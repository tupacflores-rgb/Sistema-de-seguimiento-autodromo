"""Semáforo de salida + 3 lectores RFID (ESP32 + 3× RC522 + 74HC595N) por USB Serial NDJSON v1.

No reemplaza ni duplica nada de lo que ya existe: usa el mismo `ConfigStore` (nueva sección
`config/hardware.json` + el campo `rfid_uid` que se agrega a cada vehículo en `vehicles.json`,
el "mapeo existente" UID -> auto) y, al terminar la cuenta regresiva, llama una única vez a
`TrackingEngine.reset_race()` (el único motor de carrera que ya existe en este proyecto: pone
en cero vueltas y tiempos, sin tocar la configuración de cámaras/pista/vehículos).

Dos modos:
  - "simulation": no se abre ningún puerto serial ni se necesita pyserial. Las lecturas RFID se
    generan a mano desde el panel (botón "Simular lectura" por lector).
  - "hardware": se abre el puerto serial configurado (pyserial, dependencia opcional, igual que
    OpenCV/NumPy para el tracking). Si no conecta, se guarda el error y NO se cae a simulación en
    silencio: el panel debe mostrarlo y no dejar iniciar la cuenta regresiva.

La cuenta regresiva nunca usa `time.sleep()` (bloquearía el hilo de Flask): se programa con un
`Scheduler` inyectable (de verdad usa `threading.Timer`; en los tests se usa `ManualScheduler`,
que no espera tiempo real y se "adelanta" a mano, tal como pide la consigna de pruebas).

El hilo de lectura serial es independiente (uno por conexión, daemon) y nunca toca los hilos de
cámaras/tracking ni sus locks: solo llama callbacks sobre este módulo.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable

log = logging.getLogger("mcv.admin.esp32")

NDJSON_VERSION = 1
LANES = (1, 2, 3)
_UID_RE = re.compile(r"^[0-9A-Fa-f]{4,20}$")


def missing_dependency() -> str | None:
    """Solo hace falta pyserial en modo 'hardware'; en 'simulation' no se usa."""
    try:
        import serial  # noqa: F401
    except ImportError as e:
        return (f"El modo hardware necesita pyserial ({e.name} no está instalado). "
                "Instalá con:  python -m pip install pyserial")
    return None


# =============================================================================== NDJSON v1

def parse_line(line: str) -> dict | None:
    """Una línea NDJSON -> dict, o None si está vacía/corrupta (una línea mala no debe tirar el hilo)."""
    line = line.strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        log.warning("línea NDJSON inválida descartada: %r", line[:200])
        return None
    return msg if isinstance(msg, dict) else None


def encode_line(msg: dict) -> bytes:
    return (json.dumps({"v": NDJSON_VERSION, **msg}, separators=(",", ":")) + "\n").encode("utf-8")


# =============================================================================== reloj / scheduler
# "Backend es la autoridad de la cuenta regresiva": todo el tiempo pasa por este objeto, nunca por
# time.sleep() en una ruta HTTP. Los tests inyectan ManualScheduler para no esperar de verdad.

class Scheduler:
    """Scheduler real, basado en threading.Timer (hilo propio, no bloquea Flask)."""

    def call_later(self, delay_s: float, fn: Callable[[], None]):
        t = threading.Timer(max(0.0, delay_s), fn)
        t.daemon = True
        t.start()
        return t

    def cancel(self, handle) -> None:
        if handle is not None:
            handle.cancel()

    def now(self) -> float:
        return time.monotonic()


class ManualScheduler:
    """Reloj falso para pruebas: no espera tiempo real. `advance(dt)` dispara lo que corresponda."""

    def __init__(self):
        self._now = 0.0
        self._pending: list[list] = []  # [due, fn, handle]

    def call_later(self, delay_s: float, fn: Callable[[], None]):
        handle = object()
        self._pending.append([self._now + delay_s, fn, handle])
        return handle

    def cancel(self, handle) -> None:
        self._pending = [p for p in self._pending if p[2] is not handle]

    def now(self) -> float:
        return self._now

    def advance(self, dt: float) -> None:
        self._now += dt
        while True:
            due = [p for p in self._pending if p[0] <= self._now]
            if not due:
                return
            due.sort(key=lambda p: p[0])
            nxt = due[0]
            self._pending.remove(nxt)
            nxt[1]()


# =============================================================================== enlace serial
# Independiente del resto del programa: un hilo propio por conexión, con su propio try/except por
# línea (un mensaje corrupto no corta el enlace). Nunca se instancia en modo "simulation".

class SerialLink:
    def __init__(self, port: str, baudrate: int, on_message: Callable[[dict], None],
                 on_status: Callable[..., None]):
        self.port = port
        self.baudrate = baudrate
        self.on_message = on_message
        self.on_status = on_status
        self._ser = None
        self._thread: threading.Thread | None = None
        self._stop_ev = threading.Event()
        self.connected = False
        self.last_error: str | None = None

    def open(self) -> bool:
        err = missing_dependency()
        if err:
            self.last_error = err
            return False
        if not self.port:
            self.last_error = "No se eligió el puerto COM"
            return False
        try:
            import serial
            self._ser = serial.Serial(self.port, self.baudrate, timeout=1.0)
        except Exception as e:  # noqa: BLE001 - cualquier error de apertura es "no conectó"
            self.last_error = f"{type(e).__name__}: {e}"
            self._ser = None
            return False
        self.connected = True
        self.last_error = None
        self._stop_ev.clear()
        self._thread = threading.Thread(target=self._run, name="esp32-serial", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        while not self._stop_ev.is_set():
            try:
                raw = self._ser.readline()
            except Exception as e:  # noqa: BLE001 - puerto desconectado, permisos, etc.
                self.last_error = f"{type(e).__name__}: {e}"
                self.connected = False
                self.on_status(connected=False, error=self.last_error)
                return
            if not raw:
                continue  # timeout de lectura: normal, se vuelve a intentar
            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            msg = parse_line(line)
            if msg is not None:
                try:
                    self.on_message(msg)
                except Exception:  # noqa: BLE001 - un callback que falla no debe matar el hilo
                    log.exception("error procesando mensaje NDJSON")

    def send(self, msg: dict) -> bool:
        if not self.connected or self._ser is None:
            return False
        try:
            self._ser.write(encode_line(msg))
            return True
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"
            self.connected = False
            return False

    def close(self) -> None:
        self._stop_ev.set()
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass
        self.connected = False


# =============================================================================== motor principal

class Esp32Manager:
    """Semáforo (cuenta regresiva + 6 LEDs vía 74HC595N) + 3 lectores RFID.

    `race_engine` es el `TrackingEngine` ya existente: `reset_race()` es el único método real que
    "empieza" una carrera a efectos de vueltas/tiempos (ver docstring del módulo)."""

    def __init__(self, store, race_engine, scheduler: Any | None = None,
                 link_factory: Callable[..., SerialLink] = SerialLink, debounce_s: float = 1.5):
        self.store = store
        self.race_engine = race_engine
        self.scheduler = scheduler or Scheduler()
        self.link_factory = link_factory
        self.debounce_s = debounce_s
        self._lock = threading.RLock()

        self.link: SerialLink | None = None
        self.mode = "simulation"
        self.port = ""
        self.baudrate = 115200
        self.led_count = 6
        self.countdown_seconds = 6
        self.tick_seconds = 1.0

        self.state = "idle"  # idle | countdown | running | cancelled | error
        self.remaining = 0
        self.mask = 0
        self.led_ack: int | None = None  # último mask confirmado por el firmware (led_state), si llegó
        self.error: str | None = None
        self.boot_fw: str | None = None
        self.last_comm_t: float | None = None

        self.last_by_lane: dict[int, dict] = {}
        self.last_read: dict | None = None
        self._last_raw: dict[int, dict] = {}
        self._timer_handle = None

        self.reload()

    # ---------------------------------------------------------------- configuración / conexión
    def reload(self) -> None:
        """Vuelve a leer config/hardware.json; reconecta solo si cambió modo/puerto/baudrate."""
        with self._lock:
            hw = self.store.get_hardware()
            esp, sem = hw["esp32"], hw["semaphore"]
            changed = (esp["mode"] != self.mode or esp["port"] != self.port
                       or esp["baudrate"] != self.baudrate)
            self.mode = esp["mode"]
            self.port = esp["port"]
            self.baudrate = esp["baudrate"]
            self.led_count = sem["led_count"]
            self.countdown_seconds = sem["countdown_seconds"]
            self.tick_seconds = sem["tick_seconds"]
            if changed:
                self._disconnect()
                if self.mode == "hardware":
                    self._connect_hardware()
                else:
                    self.error = None

    def _connect_hardware(self) -> None:
        self.link = self.link_factory(self.port, self.baudrate, self._on_message, self._on_status)
        if self.link.open():
            self.error = None
        else:
            self.error = self.link.last_error
            self.link = None  # no se finge conectado: hardware sin ESP32 no debe permitir iniciar

    def _disconnect(self) -> None:
        if self.link is not None:
            self.link.close()
        self.link = None

    def stop(self) -> None:
        """Cierre limpio (apagado del servidor): cancela la cuenta y cierra el enlace serial."""
        with self._lock:
            if self._timer_handle is not None:
                self.scheduler.cancel(self._timer_handle)
                self._timer_handle = None
        self._disconnect()

    @property
    def connected(self) -> bool:
        if self.mode == "simulation":
            return True
        return bool(self.link is not None and self.link.connected)

    # ---------------------------------------------------------------- mensajes entrantes (hilo serial)
    def _on_message(self, msg: dict) -> None:
        t = msg.get("type")
        with self._lock:
            self.last_comm_t = self.scheduler.now()
            if t == "boot":
                self.boot_fw = msg.get("fw")
            elif t == "rfid":
                try:
                    lane = int(msg.get("lane", 0))
                except (TypeError, ValueError):
                    return
                uid = str(msg.get("uid", "")).strip().upper()
                if lane in LANES and _UID_RE.match(uid):
                    self._record_rfid(lane, uid, msg.get("ms"), simulated=False)
            elif t == "led_state":
                try:
                    self.led_ack = int(msg.get("mask"))
                except (TypeError, ValueError):
                    pass
            # "hb" / "pong": solo actualizan last_comm_t (ya hecho arriba)

    def _on_status(self, connected: bool, error: str | None = None) -> None:
        with self._lock:
            if not connected:
                self.error = error
                # Sin fallback silencioso a simulación: el modo sigue siendo "hardware" pero
                # self.connected pasa a False, así que start_race() lo va a rechazar.

    # ---------------------------------------------------------------- RFID
    def _resolve_vehicle(self, uid: str) -> tuple[int | None, str | None]:
        if not uid:
            return None, None
        cfg = self.store.get_vehicles()
        for v in cfg["vehicles"][: cfg["count"]]:
            if (v.get("rfid_uid") or "").upper() == uid:
                return v["id"], v["name"]
        return None, None  # UID desconocido: nunca se asigna un vehículo al azar

    def _record_rfid(self, lane: int, uid: str, ms, simulated: bool) -> dict | None:
        with self._lock:
            now = self.scheduler.now()
            prev = self._last_raw.get(lane)
            if prev and prev["uid"] == uid and (now - prev["t"]) < self.debounce_s:
                return None  # antirrebote: misma tarjeta, todavía "puesta" sobre el lector
            self._last_raw[lane] = {"uid": uid, "t": now}
            vid, vname = self._resolve_vehicle(uid)
            info = {"lane": lane, "uid": uid, "vehicle_id": vid, "vehicle_name": vname,
                    "simulated": simulated, "ms": ms, "at": now}
            self.last_by_lane[lane] = info
            self.last_read = info
            return info

    def simulate_rfid(self, lane: int, uid: str | None = None) -> dict:
        """Solo en modo simulación: genera (o recibe) una lectura sin hardware real."""
        if self.mode != "simulation":
            raise RuntimeError("Simular una lectura solo está disponible en modo simulación")
        if lane not in LANES:
            raise RuntimeError("Lector inválido (tiene que ser 1, 2 o 3)")
        uid = (uid or "").strip().upper() or f"SIMUL{lane:02d}{int(self.scheduler.now() * 1000) % 10000:04d}"
        if not _UID_RE.match(uid):
            raise RuntimeError("UID inválido (hexadecimal, 4 a 20 caracteres)")
        info = self._record_rfid(lane, uid, round(self.scheduler.now() * 1000), simulated=True)
        return info or dict(self.last_by_lane[lane], debounced=True)

    # ---------------------------------------------------------------- semáforo / cuenta regresiva
    def _mask_for(self, remaining: int) -> int:
        """6 LEDs encendidos al arrancar, se apaga uno por segundo (Q0 a Q5): 63,62,60,56,48,32,0
        para el caso por defecto (6 s / 6 LEDs); se generaliza proporcionalmente si se cambia la
        duración o la cantidad de LEDs."""
        n, total = self.led_count, self.countdown_seconds
        if total <= 0 or n <= 0:
            return 0
        elapsed = total - remaining
        off = max(0, min(n, round(n * elapsed / total)))
        full = (1 << n) - 1
        return 0 if off >= n else full & ~((1 << off) - 1)

    def _send_leds(self, mask: int) -> None:
        self.mask = mask
        if self.mode == "hardware" and self.link is not None:
            self.link.send({"type": "leds", "mask": mask})
        # En simulación no hay hardware que confirmar: el panel ya refleja el estado enviado.

    def start_race(self) -> dict:
        with self._lock:
            if self.state == "countdown":
                raise RuntimeError("Ya hay una cuenta regresiva en curso")
            if self.state == "running":
                raise RuntimeError("Ya hay una carrera en curso")
            if self.mode == "hardware" and not self.connected:
                raise RuntimeError("ESP32 no conectado: no se puede iniciar en modo hardware "
                                   f"({self.error or 'sin conexión'})")
            self.state = "countdown"
            self.remaining = self.countdown_seconds
            self.error = None
            self.led_ack = None
            self._send_leds(self._mask_for(self.remaining))
            self._schedule_tick()
            return self.public_state()

    def _schedule_tick(self) -> None:
        self._timer_handle = self.scheduler.call_later(self.tick_seconds, self._on_tick)

    def _on_tick(self) -> None:
        with self._lock:
            if self.state != "countdown":
                return  # se canceló entre que se programó el tick y que se disparó
            self.remaining -= 1
            self._send_leds(self._mask_for(self.remaining))
            if self.remaining <= 0:
                self._timer_handle = None
                self.state = "running"
                self.race_engine.reset_race()  # el único motor de carrera real; se llama UNA vez
            else:
                self._schedule_tick()

    def cancel_race(self) -> dict:
        with self._lock:
            if self.state != "countdown":
                return self.public_state()
            if self._timer_handle is not None:
                self.scheduler.cancel(self._timer_handle)
                self._timer_handle = None
            self.state = "cancelled"
            self._send_leds(0)
            return self.public_state()

    def reset_semaphore(self) -> dict:
        """Vuelve el semáforo a 'idle'. A propósito NO llama a race_engine.reset_race(): no hay
        que borrar vueltas/tiempos ya corridos solo por reiniciar el semáforo."""
        with self._lock:
            if self._timer_handle is not None:
                self.scheduler.cancel(self._timer_handle)
                self._timer_handle = None
            self.state = "idle"
            self.remaining = 0
            self.error = None
            self._send_leds(0)
            return self.public_state()

    # ---------------------------------------------------------------- estado público
    def public_state(self) -> dict:
        with self._lock:
            now = self.scheduler.now()
            readers = []
            for lane in LANES:
                r = self.last_by_lane.get(lane)
                readers.append({
                    "lane": lane,
                    "uid": r["uid"] if r else None,
                    "vehicle_id": r["vehicle_id"] if r else None,
                    "vehicle_name": r["vehicle_name"] if r else None,
                    "simulated": bool(r and r["simulated"]),
                    "ms_ago": round((now - r["at"]) * 1000) if r else None,
                })
            return {
                "mode": self.mode,
                "connected": self.connected,
                "port": self.port,
                "esp32_id": "ESP32 + CH340C",
                "boot_fw": self.boot_fw,
                "last_comm_ms_ago": round((now - self.last_comm_t) * 1000) if self.last_comm_t is not None else None,
                "readers": readers,
                "led_count": self.led_count,
                "led_mask": self.mask,
                "led_ack": self.led_ack,
                "led_confirmed": self.led_ack is not None and self.led_ack == self.mask,
                "race": {"state": self.state, "remaining_s": max(0, self.remaining),
                         "countdown_seconds": self.countdown_seconds},
                "error": self.error,
            }

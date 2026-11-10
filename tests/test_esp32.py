"""Pruebas del semáforo de salida (ESP32 + 74HC595N) y los 3 lectores RFID (RC522).

    python -m pytest tests/test_esp32.py -v

No necesitan hardware real ni pyserial: usan ManualScheduler (reloj falso, sin esperar tiempo de
verdad) y un enlace serial simulado en el propio proceso de pruebas.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from admin.esp32 import Esp32Manager, ManualScheduler, encode_line, parse_line  # noqa: E402
from admin.config_store import ConfigStore  # noqa: E402
from admin.tracking import TrackingEngine  # noqa: E402


# =============================================================================== utilidades

class FakeLink:
    """Reemplaza a SerialLink en los tests: no abre ningún puerto real."""

    instances: list["FakeLink"] = []

    def __init__(self, port, baudrate, on_message, on_status, *, should_open=True):
        self.port = port
        self.baudrate = baudrate
        self.on_message = on_message
        self.on_status = on_status
        self.should_open = should_open
        self.connected = False
        self.last_error = None
        self.sent: list[dict] = []
        FakeLink.instances.append(self)

    def open(self) -> bool:
        if not self.should_open:
            self.last_error = "No se pudo abrir el puerto (simulado)"
            return False
        self.connected = True
        return True

    def send(self, msg: dict) -> bool:
        if not self.connected:
            return False
        self.sent.append(msg)
        return True

    def close(self) -> None:
        self.connected = False


class RaceEngineSpy:
    """Reemplaza a TrackingEngine para contar llamadas a reset_race() sin necesitar OpenCV."""

    def __init__(self):
        self.reset_calls = 0

    def reset_race(self):
        self.reset_calls += 1


def manager(tmp_path, *, mode="simulation", should_open=True, engine=None):
    store = ConfigStore(tmp_path)
    if mode == "hardware":
        store.save_hardware({"esp32": {"mode": "hardware", "port": "COM9"}, "semaphore": {}})
    sched = ManualScheduler()
    factory = lambda port, baud, on_msg, on_status: FakeLink(port, baud, on_msg, on_status, should_open=should_open)  # noqa: E731
    eng = engine if engine is not None else RaceEngineSpy()
    m = Esp32Manager(store, eng, scheduler=sched, link_factory=factory)
    return m, sched, eng


# =============================================================================== NDJSON

def test_ndjson_roundtrip():
    line = encode_line({"type": "rfid", "lane": 2, "uid": "A1B2C3D4"})
    msg = parse_line(line.decode())
    assert msg == {"v": 1, "type": "rfid", "lane": 2, "uid": "A1B2C3D4"}


def test_ndjson_bad_line_is_ignored():
    assert parse_line("") is None
    assert parse_line("   ") is None
    assert parse_line("{esto no es json") is None
    assert parse_line("[1,2,3]") is None  # válido como JSON pero no es un objeto


# =============================================================================== 1-2: máscara de LEDs

def test_mask_63_enciende_y_0_apaga(tmp_path):
    m, sched, eng = manager(tmp_path)
    st = m.start_race()
    assert st["led_mask"] == 0b111111 == 63
    for _ in range(6):
        sched.advance(1.0)
    assert m.public_state()["led_mask"] == 0


def test_cada_paso_apaga_exactamente_un_led(tmp_path):
    m, sched, eng = manager(tmp_path)
    m.start_race()
    masks = [m.mask]
    for _ in range(6):
        sched.advance(1.0)
        masks.append(m.mask)
    assert masks == [63, 62, 60, 56, 48, 32, 0]
    for a, b in zip(masks, masks[1:]):
        off_a = bin(a).count("1")
        off_b = bin(b).count("1")
        assert off_a - off_b == 1


# =============================================================================== 3-4: estados de carrera

def test_no_pasa_a_running_antes_de_cero(tmp_path):
    m, sched, eng = manager(tmp_path)
    m.start_race()
    for _ in range(5):
        sched.advance(1.0)
        assert m.state == "countdown"
        assert eng.reset_calls == 0
    sched.advance(1.0)
    assert m.state == "running"


def test_raceengine_recibe_exactamente_una_orden_al_cero(tmp_path):
    m, sched, eng = manager(tmp_path)
    m.start_race()
    for _ in range(6):
        sched.advance(1.0)
    assert eng.reset_calls == 1
    # ticks de más (no deberían programarse, pero por las dudas) no generan otra orden
    sched.advance(5.0)
    assert eng.reset_calls == 1


# =============================================================================== 5-6: cancelar / doble clic

def test_cancelar_apaga_leds_y_no_inicia(tmp_path):
    m, sched, eng = manager(tmp_path)
    m.start_race()
    sched.advance(2.0)
    assert m.mask != 0
    st = m.cancel_race()
    assert st["led_mask"] == 0
    assert st["race"]["state"] == "cancelled"
    sched.advance(10.0)  # aunque pase tiempo, no debe arrancar la carrera ni llamar reset_race
    assert m.state == "cancelled"
    assert eng.reset_calls == 0


def test_doble_clic_no_crea_dos_cuentas(tmp_path):
    m, sched, eng = manager(tmp_path)
    m.start_race()
    with pytest.raises(RuntimeError):
        m.start_race()
    for _ in range(6):
        sched.advance(1.0)
    assert eng.reset_calls == 1  # si se hubieran creado dos cuentas, podría haber llamado 2 veces


# =============================================================================== 7-8: simulación / hardware

def test_simulation_no_abre_puerto_serial(tmp_path):
    m, sched, eng = manager(tmp_path, mode="simulation")
    assert m.mode == "simulation"
    assert m.link is None
    assert FakeLink.instances == [] or all(not fl.connected for fl in FakeLink.instances)
    assert m.connected is True  # en simulación "conectado" es trivialmente cierto (no hay hardware)
    st = m.start_race()  # no debe tirar ni intentar abrir nada
    assert st["race"]["state"] == "countdown"


def test_hardware_sin_esp32_no_permite_iniciar_ni_cae_a_simulacion(tmp_path):
    FakeLink.instances.clear()
    m, sched, eng = manager(tmp_path, mode="hardware", should_open=False)
    assert m.mode == "hardware"  # no cambió de modo solo porque no conectó
    assert m.connected is False
    assert m.error  # se guardó el motivo
    with pytest.raises(RuntimeError):
        m.start_race()
    assert m.state == "idle"


def test_hardware_conectado_permite_iniciar_y_envia_leds(tmp_path):
    FakeLink.instances.clear()
    m, sched, eng = manager(tmp_path, mode="hardware", should_open=True)
    assert m.connected is True
    m.start_race()
    link = FakeLink.instances[-1]
    assert link.sent and link.sent[0] == {"type": "leds", "mask": 63}


# =============================================================================== 9-10: lectores RFID

def test_los_tres_lectores_emiten_lane_correcto(tmp_path):
    m, sched, eng = manager(tmp_path)
    r1 = m.simulate_rfid(1, "AA11")
    r2 = m.simulate_rfid(2, "BB22")
    r3 = m.simulate_rfid(3, "CC33")
    assert r1["lane"] == 1 and r2["lane"] == 2 and r3["lane"] == 3
    readers = m.public_state()["readers"]
    assert [r["lane"] for r in readers] == [1, 2, 3]
    assert readers[0]["uid"] == "AA11" and readers[1]["uid"] == "BB22" and readers[2]["uid"] == "CC33"


def test_uid_desconocido_no_se_asigna_al_azar(tmp_path):
    m, sched, eng = manager(tmp_path)
    r = m.simulate_rfid(1, "DEADBEEF")
    assert r["vehicle_id"] is None
    assert r["vehicle_name"] is None


def test_uid_conocido_resuelve_el_vehiculo_mapeado(tmp_path):
    store = ConfigStore(tmp_path)
    cfg = store.get_vehicles()
    cfg["vehicles"][0]["rfid_uid"] = "04A1B2C3"
    store.save_vehicles(cfg)
    sched = ManualScheduler()
    factory = lambda port, baud, on_msg, on_status: FakeLink(port, baud, on_msg, on_status)  # noqa: E731
    m = Esp32Manager(store, RaceEngineSpy(), scheduler=sched, link_factory=factory)
    r = m.simulate_rfid(1, "04a1b2c3")  # minúsculas: se normaliza a mayúsculas
    assert r["vehicle_id"] == cfg["vehicles"][0]["id"]
    assert r["vehicle_name"] == cfg["vehicles"][0]["name"]


# =============================================================================== 11: antirrebote

def test_antirrebote_evita_duplicados_y_permite_nueva_lectura_tras_retirar(tmp_path):
    m, sched, eng = manager(tmp_path)
    r1 = m.simulate_rfid(1, "11112222")
    assert r1 is not None and r1["uid"] == "11112222"
    sched.advance(0.1)
    r2 = m.simulate_rfid(1, "11112222")  # la tarjeta sigue "puesta": se ignora
    assert r2 is None or r2.get("debounced")
    before = m.last_by_lane[1]["at"]
    sched.advance(2.0)  # pasó más que el debounce: equivale a "se retiró y se volvió a presentar"
    r3 = m.simulate_rfid(1, "11112222")
    assert r3 is not None and not r3.get("debounced")
    assert m.last_by_lane[1]["at"] > before


# =============================================================================== 12: hilo serial no bloquea

def test_hilo_serial_no_bloquea_otros_hilos(tmp_path):
    """El hilo de lectura serial es daemon e independiente: llamarlo intensamente desde otro hilo
    no debe bloquear el acceso normal al resto del programa (ConfigStore, por ejemplo)."""
    store = ConfigStore(tmp_path)
    sched = ManualScheduler()
    factory = lambda port, baud, on_msg, on_status: FakeLink(port, baud, on_msg, on_status)  # noqa: E731
    m = Esp32Manager(store, RaceEngineSpy(), scheduler=sched, link_factory=factory)

    stop = threading.Event()

    def hammer():
        n = 0
        while not stop.is_set():
            m._on_message({"type": "rfid", "lane": 1, "uid": f"{n % 9999:04X}1234"})
            n += 1

    t = threading.Thread(target=hammer, daemon=True)
    t.start()
    try:
        t0 = time.monotonic()
        for _ in range(20):
            store.list_cameras()  # trabajo normal e independiente, no debería esperar al hilo serial
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0
    finally:
        stop.set()
        t.join(2)


# =============================================================================== config/hardware.json

def test_hardware_config_validation_rejects_bad_mode(tmp_path):
    store = ConfigStore(tmp_path)
    from admin.config_store import ValidationError
    with pytest.raises(ValidationError):
        store.save_hardware({"esp32": {"mode": "ouija"}})


def test_hardware_config_roundtrip(tmp_path):
    store = ConfigStore(tmp_path)
    hw = store.save_hardware({"esp32": {"mode": "hardware", "port": "COM7", "baudrate": 115200},
                              "semaphore": {"countdown_seconds": 6, "led_count": 6}})
    assert hw["esp32"]["mode"] == "hardware"
    assert hw["esp32"]["port"] == "COM7"
    again = store.get_hardware()
    assert again["esp32"]["port"] == "COM7"


# =============================================================================== rutas Flask (API completa)

@pytest.fixture()
def flask_client(tmp_path):
    pytest.importorskip("flask")
    from admin.server import create_app
    FakeLink.instances.clear()
    sched = ManualScheduler()
    factory = lambda port, baud, on_msg, on_status: FakeLink(port, baud, on_msg, on_status)  # noqa: E731
    app = create_app(tmp_path, esp32_scheduler=sched, esp32_link_factory=factory)
    c = app.test_client()
    yield c, sched, app
    app.extensions["mcv_streams"].shutdown()


def test_api_esp32_full_race_cycle(flask_client):
    c, sched, app = flask_client
    r = c.get("/api/esp32/state").get_json()
    assert r["ok"] and r["race"]["state"] == "idle"

    r = c.post("/api/esp32/race/start").get_json()
    assert r["ok"] and r["race"]["state"] == "countdown" and r["led_mask"] == 63

    # doble clic -> 409
    assert c.post("/api/esp32/race/start").status_code == 409

    eng = app.extensions["mcv_tracking"]
    for _ in range(6):
        sched.advance(1.0)
    r = c.get("/api/esp32/state").get_json()
    assert r["race"]["state"] == "running" and r["led_mask"] == 0

    r = c.post("/api/esp32/rfid/simulate", json={"lane": 1, "uid": "AABBCCDD"}).get_json()
    assert r["ok"] and r["reading"]["lane"] == 1 and r["reading"]["uid"] == "AABBCCDD"

    r = c.post("/api/esp32/race/reset").get_json()
    assert r["ok"] and r["race"]["state"] == "idle"


def test_api_esp32_rfid_simulate_rejects_in_hardware_mode(flask_client):
    c, sched, app = flask_client
    assert c.put("/api/hardware", json={"esp32": {"mode": "hardware", "port": "COM3"}}).status_code == 200
    r = c.post("/api/esp32/rfid/simulate", json={"lane": 1})
    assert r.status_code == 409


def test_api_hardware_put_rejects_invalid(flask_client):
    c, sched, app = flask_client
    r = c.put("/api/hardware", json={"semaphore": {"countdown_seconds": 999}})
    assert r.status_code == 400

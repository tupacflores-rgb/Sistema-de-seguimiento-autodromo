"""Lector MJPEG por HTTP (IP Webcam) sin OpenCV, con un hilo por cámara.

Decisiones (ver docs/informes_agentes/AGENTE_02_MULTICAMARA.md):
- Lector propio sobre socket en vez de cv2.VideoCapture: con FFmpeg, CAP_PROP_BUFFERSIZE
  no tiene efecto y un consumidor lento acumula segundos de retraso.
- Se guarda SOLO el último JPEG ("último frame"); los anteriores se descartan y se cuentan.
- Los JPEG se extraen recorriendo los segmentos del propio JPEG (SOI → … → SOS → EOI),
  así funciona con o sin la cabecera Content-Length de cada parte multipart y no se
  confunde con miniaturas EXIF.
- Timeouts de conexión y de lectura explícitos; reconexión con backoff exponencial.
- El navegador nunca se conecta directo al teléfono: el servidor re-emite el último JPEG.
  Así hay UNA sola conexión por teléfono aunque haya varias pestañas abiertas.
"""
from __future__ import annotations

import base64
import logging
import random
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

log = logging.getLogger("mcv.admin.mjpeg")

MAX_BUFFER = 16 * 1024 * 1024  # si se acumulan 16 MB sin un JPEG completo, el stream está roto


# --------------------------------------------------------------------------- JPEG

def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Ancho y alto leyendo el marcador SOF, sin decodificar la imagen."""
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:  # relleno
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 <= n:
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return (w, h)
            return None
        if marker == 0xDA:  # SOS sin SOF antes: inválido
            return None
        i += 2 + seg_len
    return None


def extract_jpeg(buf: bytearray) -> tuple[bytes | None, int]:
    """Busca un JPEG completo en buf.

    Devuelve (jpeg, consumido). Si no hay un JPEG completo, jpeg=None y `consumido`
    indica cuántos bytes iniciales se pueden descartar (basura antes de un SOI).
    """
    start = buf.find(b"\xff\xd8")
    if start < 0:
        # conservar el último byte por si es el 0xFF de un SOI partido
        return None, max(0, len(buf) - 1)
    i = start + 2
    n = len(buf)
    # Recorrer segmentos con longitud hasta SOS
    while True:
        if i + 4 > n:
            return None, start
        if buf[i] != 0xFF:
            # JPEG corrupto: saltar este SOI y seguir buscando
            return None, start + 2
        marker = buf[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        seg_len = int.from_bytes(buf[i + 2:i + 4], "big")
        if seg_len < 2:
            return None, start + 2
        if marker == 0xDA:  # Start Of Scan: luego datos entrópicos hasta EOI
            i += 2 + seg_len
            break
        i += 2 + seg_len
    # En los datos entrópicos 0xFF va seguido de 0x00 o RSTn, así que FFD9 es el EOI.
    end = buf.find(b"\xff\xd9", i)
    if end < 0:
        return None, start
    end += 2
    return bytes(buf[start:end]), end


# --------------------------------------------------------------------------- lector

@dataclass(frozen=True)
class JpegFrame:
    seq: int
    data: bytes
    t_received: float      # time.monotonic() al completar el JPEG
    t_wall: float          # time.time() (para mostrar)
    width: int | None
    height: int | None


class MjpegReader:
    """Hilo que mantiene el último JPEG de una cámara. Nunca bloquea a quien lo consulta."""

    def __init__(self, cam_id: str, url: str, username: str = "", password: str = "",
                 connect_timeout: float = 3.0, read_timeout: float = 3.0,
                 backoff_initial: float = 0.5, backoff_max: float = 5.0):
        self.cam_id = cam_id
        self.url = url
        self._auth = None
        if username or password:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            self._auth = f"Basic {token}"
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max

        self._cond = threading.Condition()
        self._latest: JpegFrame | None = None
        self._seq = 0
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread = threading.Thread(target=self._run, name=f"mjpeg:{cam_id}", daemon=True)

        # estado / métricas (solo valores medidos)
        self.state = "idle"          # idle|connecting|streaming|backoff|stopped
        self.last_error = ""
        self.reconnects = 0
        self.frames_total = 0
        self.bytes_total = 0
        self.connected_since: float | None = None
        self._fps_times: list[float] = []
        self.last_access = time.monotonic()

    # ------------------------------------------------------------------ API
    def start(self) -> "MjpegReader":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        s = self._sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        with self._cond:
            self._cond.notify_all()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def latest(self) -> JpegFrame | None:
        self.last_access = time.monotonic()
        return self._latest

    def wait_newer(self, after_seq: int, timeout: float) -> JpegFrame | None:
        """Espera un frame con seq > after_seq (o devuelve None al vencer el tiempo)."""
        self.last_access = time.monotonic()
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._stop.is_set():
                f = self._latest
                if f is not None and f.seq > after_seq:
                    return f
                rest = deadline - time.monotonic()
                if rest <= 0:
                    return None
                self._cond.wait(rest)
        return None

    def status(self) -> dict:
        now = time.monotonic()
        recent = [t for t in self._fps_times if now - t <= 2.0]
        fps = 0.0
        if len(recent) >= 2:
            span = recent[-1] - recent[0]
            fps = (len(recent) - 1) / span if span > 0 else 0.0
        f = self._latest
        return {
            "cam_id": self.cam_id,
            "state": self.state,
            "fps_received": round(fps, 1),
            "width": f.width if f else None,
            "height": f.height if f else None,
            "last_frame_age_ms": round((now - f.t_received) * 1000) if f else None,
            "last_frame_kb": round(len(f.data) / 1024, 1) if f else None,
            "frames_total": self.frames_total,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "connected_for_s": round(now - self.connected_since, 1) if self.connected_since else None,
        }

    # ------------------------------------------------------------------ hilo
    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            self.state = "connecting"
            try:
                self._stream_once()
                attempt = 0  # terminó sin excepción (fin de stream): reintentar rápido
            except Exception as e:  # noqa: BLE001 - cualquier fallo de red debe reintentarse
                if self._stop.is_set():
                    break
                self.last_error = f"{type(e).__name__}: {e}"
                log.info("cam %s: %s", self.cam_id, self.last_error)
            finally:
                self.connected_since = None
                self._close_sock()
            if self._stop.is_set():
                break
            self.reconnects += 1
            self.state = "backoff"
            delay = min(self.backoff_max, self.backoff_initial * (2 ** attempt))
            delay *= 1 + random.uniform(-0.2, 0.2)
            attempt = min(attempt + 1, 10)
            self._stop.wait(delay)
        self.state = "stopped"

    def _close_sock(self) -> None:
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def _stream_once(self) -> None:
        u = urlparse(self.url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"URL inválida: {self.url}")
        port = u.port or (443 if u.scheme == "https" else 80)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        sock = socket.create_connection((u.hostname, port), timeout=self.connect_timeout)
        if u.scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # IP Webcam usa certificado autofirmado
            sock = ctx.wrap_socket(sock, server_hostname=u.hostname)
        self._sock = sock
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        host_hdr = u.hostname if u.port is None else f"{u.hostname}:{u.port}"
        req = [f"GET {path} HTTP/1.1", f"Host: {host_hdr}", "Connection: close",
               "User-Agent: multicam-vision-admin/0.1", "Accept: */*"]
        if self._auth:
            req.append(f"Authorization: {self._auth}")
        sock.sendall(("\r\n".join(req) + "\r\n\r\n").encode())
        sock.settimeout(self.read_timeout)

        buf = bytearray()
        # cabeceras HTTP
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("conexión cerrada antes de las cabeceras")
            buf += chunk
            if len(buf) > 65536:
                raise ConnectionError("cabeceras HTTP demasiado largas")
        head, _, rest = bytes(buf).partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin1")
        parts = status_line.split()
        if len(parts) < 2 or parts[1] != "200":
            code = parts[1] if len(parts) > 1 else "?"
            hint = " (¿usuario/contraseña de IP Webcam?)" if code == "401" else ""
            raise ConnectionError(f"HTTP {code}{hint}")
        buf = bytearray(rest)

        self.state = "streaming"
        self.connected_since = time.monotonic()
        self.last_error = ""
        while not self._stop.is_set():
            while True:
                jpeg, consumed = extract_jpeg(buf)
                if consumed:
                    del buf[:consumed]
                if jpeg is None:
                    break
                self._publish(jpeg)
            if len(buf) > MAX_BUFFER:
                raise ConnectionError("stream sin JPEG válidos (buffer excedido)")
            chunk = sock.recv(262144)
            if not chunk:
                raise ConnectionError("la cámara cerró la conexión")
            self.bytes_total += len(chunk)
            buf += chunk

    def _publish(self, jpeg: bytes) -> None:
        now = time.monotonic()
        size = jpeg_size(jpeg)
        with self._cond:
            self._seq += 1
            self._latest = JpegFrame(self._seq, jpeg, now, time.time(),
                                     size[0] if size else None, size[1] if size else None)
            self.frames_total += 1
            self._fps_times.append(now)
            if len(self._fps_times) > 240:
                del self._fps_times[:120]
            self._cond.notify_all()


# --------------------------------------------------------------------------- gestor

class StreamManager:
    """Arranca lectores bajo demanda y los detiene si nadie los usa durante `idle_timeout` s.

    Así la página no consume Wi-Fi de los teléfonos cuando no se está mirando video.
    """

    def __init__(self, idle_timeout: float = 30.0):
        self.idle_timeout = idle_timeout
        self._readers: dict[str, MjpegReader] = {}
        self._sig: dict[str, tuple] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._janitor = threading.Thread(target=self._clean_loop, name="mjpeg-janitor", daemon=True)
        self._janitor.start()

    def get(self, cam_id: str, url: str, username: str = "", password: str = "") -> MjpegReader:
        sig = (url, username, password)
        with self._lock:
            r = self._readers.get(cam_id)
            if r is not None and (self._sig.get(cam_id) != sig or not r.alive):
                r.stop()
                r = None
            if r is None:
                r = MjpegReader(cam_id, url, username, password).start()
                self._readers[cam_id] = r
                self._sig[cam_id] = sig
            r.last_access = time.monotonic()
            return r

    def peek(self, cam_id: str) -> MjpegReader | None:
        with self._lock:
            return self._readers.get(cam_id)

    def drop(self, cam_id: str) -> None:
        with self._lock:
            r = self._readers.pop(cam_id, None)
            self._sig.pop(cam_id, None)
        if r is not None:
            r.stop()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            readers = list(self._readers.values())
            self._readers.clear()
        for r in readers:
            r.stop()

    def _clean_loop(self) -> None:
        while not self._stop.wait(2.0):
            now = time.monotonic()
            with self._lock:
                idle = [cid for cid, r in self._readers.items() if now - r.last_access > self.idle_timeout]
                victims = [self._readers.pop(cid) for cid in idle]
                for cid in idle:
                    self._sig.pop(cid, None)
            for r in victims:
                log.info("cam %s: sin uso, se cierra la conexión", r.cam_id)
                r.stop()

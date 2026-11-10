"""PROTOTIPO de captura multicámara (solo para experimentos del Agente 02).

No es el módulo final. Sirve para validar la propuesta de interfaces:
  Frame, ConnState, ReconnectPolicy, CameraConfig, CameraSource, FrameSynchronizer.
"""
from __future__ import annotations

import collections
import enum
import http.client
import logging
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlparse

import cv2
import numpy as np

log = logging.getLogger("capture")


def mono() -> float:
    return time.monotonic()


@dataclass(frozen=True, slots=True)
class Frame:
    cam_id: str
    seq: int                 # contador local del lector (monótono, sobrevive reconexiones)
    t_received: float        # time.monotonic() al terminar de recibir el JPEG [s]
    t_capture_est: float     # t_received - latencia estimada de la cámara [s] (reloj monotónico del PC)
    t_wall: float            # time.time() al recibir (solo para logs/BD)
    image: np.ndarray        # BGR uint8
    session: int = 0         # nº de conexión (cambia en cada reconexión)
    jpeg_bytes: int = 0


class ConnState(enum.Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    STREAMING = "streaming"
    STALLED = "stalled"          # conectado pero sin frames > stall_timeout
    BACKOFF = "backoff"          # esperando para reintentar
    STOPPED = "stopped"


@dataclass
class ReconnectPolicy:
    initial_s: float = 0.5
    max_s: float = 10.0
    factor: float = 2.0
    jitter: float = 0.2          # ±20 %

    def delay(self, attempt: int) -> float:
        d = min(self.max_s, self.initial_s * (self.factor ** attempt))
        return d * (1.0 + random.uniform(-self.jitter, self.jitter))


@dataclass
class CameraConfig:
    cam_id: str
    url: str                         # http://IP:8080/video
    backend: str = "mjpeg"           # "mjpeg" (lector multipart propio) | "ffmpeg"
    connect_timeout_s: float = 3.0
    read_timeout_s: float = 2.0      # sin bytes durante este tiempo -> reconectar
    latency_offset_s: float = 0.0    # medido con LED/cronómetro (ver informe)
    ring_size: int = 8
    decode: bool = True
    reconnect: ReconnectPolicy = field(default_factory=ReconnectPolicy)


class _MjpegHttpReader:
    """Lector multipart/x-mixed-replace manual: http.client + cv2.imdecode."""

    def __init__(self, url: str, connect_timeout: float, read_timeout: float):
        self.u = urlparse(url)
        self.ct, self.rt = connect_timeout, read_timeout
        self.conn = None
        self.fp = None
        self.boundary = None

    def open(self):
        path = self.u.path or "/"
        if self.u.query:
            path += "?" + self.u.query
        # socket crudo: control explícito de timeouts de conexión y de lectura
        self.conn = socket.create_connection((self.u.hostname, self.u.port or 80), timeout=self.ct)
        self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        req = (f"GET {path} HTTP/1.1\r\nHost: {self.u.hostname}\r\n"
               f"Connection: close\r\nUser-Agent: multicam/0.1\r\n\r\n").encode()
        self.conn.sendall(req)
        self.conn.settimeout(self.rt)
        fp = self.conn.makefile("rb", buffering=65536)
        status = fp.readline(1024).split()
        if len(status) < 2 or status[1] != b"200":
            raise ConnectionError(f"HTTP status {status!r}")
        ctype = ""
        while True:
            line = fp.readline(4096)
            if not line:
                raise ConnectionError("EOF en cabeceras")
            if not line.strip():
                break
            k, _, v = line.decode("latin1").partition(":")
            if k.strip().lower() == "content-type":
                ctype = v.strip()
        if "boundary=" not in ctype:
            raise ConnectionError(f"no multipart: {ctype}")
        self.boundary = ctype.split("boundary=")[1].split(";")[0].strip().strip('"')
        if self.boundary.startswith("--"):
            self.boundary = self.boundary[2:]
        self.fp = fp

    def read_jpeg(self) -> bytes:
        fp = self.fp
        # buscar línea de boundary
        while True:
            line = fp.readline(1024)
            if not line:
                raise ConnectionError("EOF")
            if line.strip().lstrip(b"-") == self.boundary.lstrip("-").encode() or line.startswith(b"--"):
                break
        length = None
        while True:
            line = fp.readline(1024)
            if not line:
                raise ConnectionError("EOF")
            s = line.strip()
            if not s:
                break
            k, _, v = s.partition(b":")
            if k.lower() == b"content-length":
                length = int(v.strip())
        if length is not None:
            data = fp.read(length)
            if len(data) != length:
                raise ConnectionError("short read")
            return data
        # sin Content-Length: leer líneas hasta el siguiente boundary (prototipo)
        buf = bytearray()
        bnd = b"--" + self.boundary.encode()
        while True:
            line = fp.readline(65536)
            if not line:
                raise ConnectionError("EOF")
            if line.startswith(bnd):
                # se consumió el boundary del siguiente part; el próximo read_jpeg
                # lo buscará de nuevo -> en producción usar un parser con estado
                return bytes(buf).rstrip(b"\r\n")
            buf += line

    def close(self):
        for o in (self.fp, self.conn):
            try:
                if o:
                    o.close()
            except Exception:
                pass


class _FfmpegReader:
    def __init__(self, url: str, open_timeout: float, read_timeout: float):
        self.url, self.ot, self.rt = url, open_timeout, read_timeout
        self.cap = None

    def open(self):
        params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(self.ot * 1000),
                  cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(self.rt * 1000)]
        self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG, params)
        if not self.cap.isOpened():
            raise ConnectionError("VideoCapture no abrió")
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def read_image(self):
        ok, img = self.cap.read()
        if not ok:
            raise ConnectionError("read() False")
        return img

    def close(self):
        if self.cap is not None:
            self.cap.release()


class CameraSource:
    """Un hilo por cámara. Siempre conserva el último frame (slot) + anillo corto.

    El consumidor nunca bloquea al lector: si procesa más lento que la cámara,
    los frames intermedios se descartan (contados en stats['dropped_overwrite']).
    """

    def __init__(self, cfg: CameraConfig,
                 on_state: Optional[Callable[[str, ConnState], None]] = None):
        self.cfg = cfg
        self.on_state = on_state
        self._state = ConnState.IDLE
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._latest: Optional[Frame] = None
        self._ring: collections.deque[Frame] = collections.deque(maxlen=cfg.ring_size)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seq = 0
        self._session = 0
        self._consumed_seq = -1
        self.stats = dict(frames=0, reconnects=0, dropped_overwrite=0, errors=0,
                          last_error="", connected_at=None, fps_ema=0.0)
        self.events: list[tuple[float, str]] = []   # (t_mono, evento) para experimentos

    # --- ciclo de vida ---
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"cam-{self.cfg.cam_id}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        self._set_state(ConnState.STOPPED)

    @property
    def state(self) -> ConnState:
        return self._state

    # --- consumo ---
    def latest(self) -> Optional[Frame]:
        with self._lock:
            return self._latest

    def wait_next(self, timeout: float = 1.0) -> Optional[Frame]:
        """Bloquea hasta que haya un frame más nuevo que el último entregado."""
        with self._cond:
            ok = self._cond.wait_for(
                lambda: self._latest is not None and self._latest.seq > self._consumed_seq, timeout)
            if not ok:
                return None
            f = self._latest
            self._consumed_seq = f.seq
            return f

    def recent(self) -> list[Frame]:
        with self._lock:
            return list(self._ring)

    # --- interno ---
    def _set_state(self, s: ConnState):
        if s is not self._state:
            self._state = s
            self.events.append((mono(), s.value))
            if self.on_state:
                try:
                    self.on_state(self.cfg.cam_id, s)
                except Exception:
                    log.exception("on_state")

    def _publish(self, img: np.ndarray, t_rx: float, nbytes: int):
        f = Frame(self.cfg.cam_id, self._seq, t_rx, t_rx - self.cfg.latency_offset_s,
                  time.time(), img, self._session, nbytes)
        self._seq += 1
        with self._cond:
            if self._latest is not None and self._latest.seq > self._consumed_seq:
                self.stats["dropped_overwrite"] += 1
            self._latest = f
            self._ring.append(f)
            self._cond.notify_all()
        st = self.stats
        st["frames"] += 1

    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            self._set_state(ConnState.CONNECTING)
            c = self.cfg
            rd = (_MjpegHttpReader(c.url, c.connect_timeout_s, c.read_timeout_s) if c.backend == "mjpeg"
                  else _FfmpegReader(c.url, c.connect_timeout_s, c.read_timeout_s))
            try:
                rd.open()
                self._session += 1
                self.stats["connected_at"] = mono()
                got_first = False
                while not self._stop.is_set():
                    if c.backend == "mjpeg":
                        jpg = rd.read_jpeg()
                        t_rx = mono()
                        img = (cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                               if c.decode else None)
                        if c.decode and img is None:
                            self.stats["errors"] += 1
                            continue
                        n = len(jpg)
                    else:
                        img = rd.read_image()
                        t_rx = mono()
                        n = 0
                    if not got_first:
                        got_first = True
                        attempt = 0
                        self._set_state(ConnState.STREAMING)
                    self._publish(img, t_rx, n)
            except (socket.timeout, TimeoutError) as e:
                self.stats["last_error"] = f"timeout: {e}"
                self._set_state(ConnState.STALLED)
            except Exception as e:  # noqa: BLE001
                self.stats["last_error"] = repr(e)
                self.stats["errors"] += 1
            finally:
                rd.close()
            if self._stop.is_set():
                break
            self.stats["reconnects"] += 1
            self._set_state(ConnState.BACKOFF)
            self._stop.wait(c.reconnect.delay(attempt))
            attempt += 1


@dataclass(frozen=True, slots=True)
class FrameSet:
    t_ref: float
    frames: dict[str, Optional[Frame]]   # None = cámara sin frame válido dentro de tolerancia
    max_skew_s: float


class FrameSynchronizer:
    """Empareja por t_capture_est más cercano usando el anillo de cada cámara."""

    def __init__(self, sources: dict[str, CameraSource], tolerance_s: float = 0.020,
                 max_age_s: float = 0.25):
        self.sources = sources
        self.tol = tolerance_s
        self.max_age = max_age_s

    def get(self, now: Optional[float] = None) -> FrameSet:
        now = mono() if now is None else now
        rings = {k: [f for f in s.recent() if now - f.t_capture_est <= self.max_age]
                 for k, s in self.sources.items()}
        alive = {k: r for k, r in rings.items() if r}
        if not alive:
            return FrameSet(now, {k: None for k in self.sources}, 0.0)
        # referencia: el más reciente entre los "últimos" de cada cámara que sea el más viejo
        # (min de los máximos) -> así todas las cámaras tienen datos hasta t_ref
        t_ref = min(r[-1].t_capture_est for r in alive.values())
        out, skews = {}, []
        for k in self.sources:
            r = alive.get(k)
            if not r:
                out[k] = None
                continue
            f = min(r, key=lambda fr: abs(fr.t_capture_est - t_ref))
            if abs(f.t_capture_est - t_ref) <= self.tol:
                out[k] = f
                skews.append(f.t_capture_est)
            else:
                out[k] = None
        skew = (max(skews) - min(skews)) if len(skews) > 1 else 0.0
        return FrameSet(t_ref, out, skew)

"""Servidor MJPEG sintético que imita (de forma aproximada) el endpoint /video de IP Webcam.

- Un hilo "sensor" genera frames a FPS fijos (reloj único, como una cámara real).
- Cada frame lleva codificado en píxeles: seq (32 bits) y t_gen en ms (mod 2^32),
  como bloques blanco/negro en la franja superior (robusto a JPEG).
- Opcional: parche "LED" que se enciende según el reloj absoluto (para simular el
  método de medición de desfase entre cámaras con un LED del ESP32).
- /video : multipart/x-mixed-replace; cada cliente recibe SIEMPRE el último frame
  disponible (si el cliente es lento, el servidor salta frames; la acumulación
  queda solo en los buffers TCP), salvo --mode queue (envía todos, peor caso).
- /shot.jpg : último frame.
- /ctl?stall=S : deja de enviar durante S segundos sin cerrar el socket (Wi-Fi colgado).
- /ctl?drop=1  : cierra todas las conexiones activas.
- --delay-ms / --jitter-ms : retardo artificial entre generación y envío (simula
  latencia de pipeline del teléfono, distinta por cámara).

Uso: python mjpeg_server.py --port 8081 --fps 30
"""
import argparse
import random
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

BOUNDARY = "Ba4oTvQMY8ew04N8dcnM"
NBITS = 32
BLOCK_W = 10
BLOCK_H = 16


def now_ms():
    return time.time_ns() // 1_000_000


def encode_bits(img, value, row, nbits=NBITS):
    for i in range(nbits):
        bit = (value >> i) & 1
        x0 = i * BLOCK_W
        img[row * BLOCK_H:(row + 1) * BLOCK_H, x0:x0 + BLOCK_W] = 255 if bit else 0


def decode_bits(img, row, nbits=NBITS):
    """Decodifica (gris o BGR). Devuelve int."""
    if img.ndim == 3:
        img = img[:, :, 1]
    v = 0
    yc = row * BLOCK_H + BLOCK_H // 2
    for i in range(nbits):
        xc = i * BLOCK_W + BLOCK_W // 2
        if img[yc - 3:yc + 3, xc - 2:xc + 3].mean() > 127:
            v |= 1 << i
    return v


def decode_frame_meta(img):
    """-> (seq, t_gen_ms_mod32, led_on)"""
    seq = decode_bits(img, 0)
    tg = decode_bits(img, 1)
    g = img[:, :, 1] if img.ndim == 3 else img
    led = g[60:100, 560:620].mean() > 127
    return seq, tg, led


class Sensor(threading.Thread):
    def __init__(self, fps, w, h, quality, led_period_ms, led_on_ms):
        super().__init__(daemon=True)
        self.fps, self.w, self.h, self.q = fps, w, h, quality
        self.led_period_ms, self.led_on_ms = led_period_ms, led_on_ms
        self.cond = threading.Condition()
        self.latest = None  # (seq, t_gen_ms, jpeg_bytes)
        self.history = []   # para modo queue
        self.seq = 0
        rng = np.random.default_rng(0)
        self.bg = rng.integers(40, 200, (h, w, 3), dtype=np.uint8)
        self.bg = cv2.GaussianBlur(self.bg, (0, 0), 3)

    def run(self):
        period = 1.0 / self.fps
        t_next = time.perf_counter()
        while True:
            t_next += period
            tg = now_ms()
            img = self.bg.copy()
            # objeto móvil para que el JPEG tenga contenido cambiante
            x = int((self.seq * 7) % (self.w - 80))
            cv2.rectangle(img, (x, 200), (x + 80, 240), (20, 20, 20), -1)
            encode_bits(img, self.seq & 0xFFFFFFFF, 0)
            encode_bits(img, tg & 0xFFFFFFFF, 1)
            if self.led_period_ms:
                on = (tg % self.led_period_ms) < self.led_on_ms
                img[60:100, 560:620] = 255 if on else 0
            cv2.putText(img, f"seq {self.seq}", (10, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.q])
            with self.cond:
                self.latest = (self.seq, tg, buf.tobytes())
                self.history.append(self.latest)
                if len(self.history) > 2000:
                    self.history = self.history[-1000:]
                self.cond.notify_all()
            self.seq += 1
            dt = t_next - time.perf_counter()
            if dt > 0:
                time.sleep(dt)
            else:
                t_next = time.perf_counter()


class State:
    sensor: Sensor = None
    stall_until = 0.0
    drop_gen = 0
    delay_ms = 0.0
    jitter_ms = 0.0
    mode = "latest"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/ctl":
            qs = parse_qs(u.query)
            if "stall" in qs:
                State.stall_until = time.time() + float(qs["stall"][0])
            if "drop" in qs:
                State.drop_gen += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if u.path == "/shot.jpg":
            s = State.sensor
            with s.cond:
                s.cond.wait_for(lambda: s.latest is not None)
                _, _, jpg = s.latest
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpg)))
            self.end_headers()
            self.wfile.write(jpg)
            return
        if u.path in ("/video", "/videofeed"):
            self.stream()
            return
        self.send_response(404)
        self.end_headers()

    def stream(self):
        s = State.sensor
        my_gen = State.drop_gen
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace;boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        last_seq = -1
        qidx = None
        try:
            while True:
                if State.drop_gen != my_gen:
                    return
                while time.time() < State.stall_until:
                    time.sleep(0.01)
                    if State.drop_gen != my_gen:
                        return
                with s.cond:
                    if State.mode == "queue" or State.delay_ms or State.jitter_ms:
                        # con retardo artificial se envían TODOS los frames en pipeline
                        # (cada uno a t_gen + d), si no la tasa caería a 1/d
                        if qidx is None:
                            qidx = len(s.history) - 1
                        s.cond.wait_for(lambda: len(s.history) > qidx, timeout=1.0)
                        if len(s.history) <= qidx:
                            continue
                        seq, tg, jpg = s.history[qidx]
                        qidx += 1
                    else:
                        s.cond.wait_for(lambda: s.latest is not None and s.latest[0] != last_seq, timeout=1.0)
                        if s.latest is None or s.latest[0] == last_seq:
                            continue
                        seq, tg, jpg = s.latest
                last_seq = seq
                if State.delay_ms or State.jitter_ms:
                    d = State.delay_ms + random.uniform(0, State.jitter_ms)
                    t_send = max((tg + d) / 1000.0, getattr(self, "_last_send", 0.0))
                    self._last_send = t_send
                    wait = t_send - time.time()
                    if wait > 0:
                        time.sleep(wait)
                head = (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpg)}\r\n\r\n").encode()
                self.wfile.write(head + jpg + b"\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class TServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--fps", type=float, default=30)
    ap.add_argument("--w", type=int, default=640)
    ap.add_argument("--h", type=int, default=480)
    ap.add_argument("--quality", type=int, default=80)
    ap.add_argument("--delay-ms", type=float, default=0)
    ap.add_argument("--jitter-ms", type=float, default=0)
    ap.add_argument("--mode", choices=["latest", "queue"], default="latest")
    ap.add_argument("--led-period-ms", type=int, default=0)
    ap.add_argument("--led-on-ms", type=int, default=100)
    a = ap.parse_args()
    State.delay_ms, State.jitter_ms, State.mode = a.delay_ms, a.jitter_ms, a.mode
    State.sensor = Sensor(a.fps, a.w, a.h, a.quality, a.led_period_ms, a.led_on_ms)
    State.sensor.start()
    srv = TServer(("127.0.0.1", a.port), Handler)
    print(f"serving on {a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

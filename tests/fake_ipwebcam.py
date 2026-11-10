"""Servidor que imita a la app IP Webcam, para probar sin teléfonos.

    python tests/fake_ipwebcam.py --port 8081            (luego usar 127.0.0.1:8081 como cámara)

Sirve /video (MJPEG), /shot.jpg, /status.json, /settings/<clave>?set=, /ptz?zoom=,
/enabletorch, /disabletorch, /focus, /nofocus. Dibuja una pista sintética con un auto
que da vueltas y un contador de frames. Requiere OpenCV y NumPy (solo para generar imágenes).
"""
from __future__ import annotations

import argparse
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np


class PlanScene:
    """Pista en el plano (mm) vista en perspectiva por cada cámara simulada.

    Todas las cámaras comparten el reloj (time.monotonic), así que la posición real de cada
    auto en cada instante es conocida: sirve como verdad de terreno para medir el error.
    """
    W, H = 2440, 1220
    CX, CY, RX, RY = 1220.0, 610.0, 900.0, 380.0          # línea central (elipse)
    PX_PER_MM = 0.5
    ORIGIN = (-60.0, -60.0)                               # esquina del lienzo en mm
    REF_POINTS = {"R1": (150, 150), "R2": (1250, 120), "R3": (1300, 1100), "R4": (150, 1080),
                  "R5": (2300, 140), "R6": (2290, 1090), "R7": (1150, 1110), "R8": (1180, 140)}

    def __init__(self, cars=None, distractor=True):
        self.cars = cars if cars is not None else [
            {"bgr": (0, 0, 255), "omega": 0.55, "phase": 0.0, "off": -60},
            {"bgr": (0, 255, 0), "omega": 0.45, "phase": 2.1, "off": 60},
            {"bgr": (255, 0, 0), "omega": 0.50, "phase": 4.2, "off": 0},
        ]
        self.distractor = distractor
        self.t0 = time.monotonic()

    def car_pos(self, i: int, t: float) -> tuple[float, float]:
        c = self.cars[i]
        a = c["phase"] + c["omega"] * (t - self.t0)
        return (self.CX + (self.RX + c["off"]) * math.cos(a), self.CY + (self.RY + c["off"]) * math.sin(a))

    def ellipse(self, rx, ry, n=120):
        return [(self.CX + rx * math.cos(2 * math.pi * k / n), self.CY + ry * math.sin(2 * math.pi * k / n)) for k in range(n)]

    def _to_canvas(self, pts):
        ox, oy = self.ORIGIN
        return np.array([((x - ox) * self.PX_PER_MM, (y - oy) * self.PX_PER_MM) for x, y in pts], np.int32)

    def canvas_to_plan(self) -> np.ndarray:
        ox, oy = self.ORIGIN
        k = self.PX_PER_MM
        return np.array([[1 / k, 0, ox], [0, 1 / k, oy], [0, 0, 1]], np.float64)

    def render(self, H_plan_to_img: np.ndarray, size: tuple[int, int], t: float) -> np.ndarray:
        k = self.PX_PER_MM
        cw, ch = int((self.W - 2 * self.ORIGIN[0]) * k), int((self.H - 2 * self.ORIGIN[1]) * k)
        can = np.full((ch, cw, 3), (95, 105, 110), np.uint8)          # mesa
        cv2.fillPoly(can, [self._to_canvas([(0, 0), (self.W, 0), (self.W, self.H), (0, self.H)])], (32, 32, 32))
        for rx, ry in ((self.RX + 160, self.RY + 160), (self.RX - 160, self.RY - 160)):
            cv2.polylines(can, [self._to_canvas(self.ellipse(rx, ry))], True, (235, 235, 235), 3, cv2.LINE_AA)
        for name, (x, y) in self.REF_POINTS.items():
            c = self._to_canvas([(x, y)])[0]
            cv2.rectangle(can, tuple(c - 12), tuple(c + 12), (255, 255, 255), -1)
            cv2.rectangle(can, tuple(c - 5), tuple(c + 5), (0, 0, 0), -1)
        if self.distractor:  # objeto rojo QUIETO fuera de la pista (no debe detectarse como auto)
            c = self._to_canvas([(60, 600)])[0]
            cv2.circle(can, tuple(c), 14, (0, 0, 255), -1)
        for i, car in enumerate(self.cars):
            x, y = self.car_pos(i, t)
            c = tuple(self._to_canvas([(x, y)])[0])
            cv2.circle(can, c, int(48 * k), (45, 45, 45), -1, cv2.LINE_AA)
            glow = np.zeros_like(can)
            cv2.circle(glow, c, int(22 * k), car["bgr"], -1, cv2.LINE_AA)
            glow = cv2.GaussianBlur(glow, (0, 0), 3)
            can = cv2.add(can, glow)
            cv2.circle(can, c, int(14 * k), car["bgr"], -1, cv2.LINE_AA)
            core = tuple(min(255, v + 170) for v in car["bgr"])
            cv2.circle(can, c, int(6 * k), core, -1, cv2.LINE_AA)
        M = H_plan_to_img @ self.canvas_to_plan()
        return cv2.warpPerspective(can, M, size, flags=cv2.INTER_LINEAR, borderValue=(70, 75, 80))


def camera_homography(plan_quad, img_quad) -> np.ndarray:
    return cv2.getPerspectiveTransform(np.float32(plan_quad), np.float32(img_quad)).astype(np.float64)


# Dos cámaras oblicuas con zona compartida aprox. entre x = 940 y 1500 mm
CAM_A_H = camera_homography([(-50, -50), (1500, -50), (1500, 1270), (-50, 1270)],
                            [(150, 40), (830, 60), (945, 705), (15, 690)])
CAM_B_H = camera_homography([(940, -50), (2490, -50), (2490, 1270), (940, 1270)],
                            [(120, 55), (815, 40), (945, 690), (10, 705)])


class Camera3D:
    """Cámara pinhole real (sin distorsión) ubicada en 3D, en mm del plano.

    Ejes: X a lo largo de la plancha, Y hacia abajo en el plano, Z = X × Y (hacia ADENTRO de la
    plancha). Por eso una cámara a 1,2 m de ALTURA tiene z = -1200.
    """

    def __init__(self, position, look_at, hfov_deg=62.0, size=(960, 720)):
        self.C = np.array(position, float)
        w, h = size
        self.f = (w / 2) / math.tan(math.radians(hfov_deg / 2))
        self.K = np.array([[self.f, 0, w / 2], [0, self.f, h / 2], [0, 0, 1]])
        z = np.array(look_at, float) - self.C
        z /= np.linalg.norm(z)
        x = np.cross(z, [0, 0, -1.0])
        x /= np.linalg.norm(x)
        y = np.cross(z, x)
        self.R = np.vstack([x, y, z])
        self.t = -self.R @ self.C

    @property
    def H_plan_to_img(self) -> np.ndarray:
        return self.K @ np.column_stack([self.R[:, 0], self.R[:, 1], self.t])

    def project(self, x, y, height_mm=0.0):
        p = self.K @ (self.R @ np.array([x, y, -height_mm]) + self.t)
        return p[:2] / p[2], p[2]


class Figure8Scene(PlanScene):
    """Pista en forma de 8 (cruce a nivel en el centro), autos con LED a `led_h` mm de altura."""
    A, B, HALF_W = 950.0, 800.0, 140.0           # semieje X, "alto" del 8, medio ancho de pista
    REF_POINTS = {"R1": (120, 120), "R2": (1220, 90), "R3": (2320, 120), "R4": (2330, 1100),
                  "R5": (1220, 1130), "R6": (110, 1100), "R7": (600, 610), "R8": (1840, 610)}

    def __init__(self, cars=None, distractor=True, led_h=55.0):
        super().__init__(cars=cars if cars is not None else [
            {"bgr": (0, 0, 255), "omega": 0.40, "phase": 0.3},
            {"bgr": (0, 255, 0), "omega": 0.33, "phase": 2.4},
            {"bgr": (255, 0, 0), "omega": 0.37, "phase": 4.4},
        ], distractor=distractor)
        self.led_h = led_h

    def path(self, a):
        return (self.CX + self.A * math.sin(a), self.CY + self.B / 2 * math.sin(2 * a))

    def car_pos(self, i, t):
        c = self.cars[i]
        return self.path(c["phase"] + c["omega"] * (t - self.t0))

    def centerline(self, n=64):
        return [self.path(2 * math.pi * k / n) for k in range(n)]

    def islands(self):
        # lazo derecho e izquierdo: elipses interiores que no tocan la pista
        out = []
        for sgn in (1, -1):
            cx = self.CX + sgn * 0.56 * self.A
            out.append([(cx + 0.22 * self.A * math.cos(2 * math.pi * k / 24), self.CY + 0.21 * self.B * math.sin(2 * math.pi * k / 24))
                        for k in range(24)])
        return out

    def outer(self):
        return [(60, 60), (self.W - 60, 60), (self.W - 60, self.H - 60), (60, self.H - 60)]

    def _board(self):
        k = self.PX_PER_MM
        cw, ch = int((self.W - 2 * self.ORIGIN[0]) * k), int((self.H - 2 * self.ORIGIN[1]) * k)
        can = np.full((ch, cw, 3), (95, 105, 110), np.uint8)
        cv2.fillPoly(can, [self._to_canvas([(0, 0), (self.W, 0), (self.W, self.H), (0, self.H)])], (30, 30, 30))
        cl = self._to_canvas(self.centerline(200))
        cv2.polylines(can, [cl], True, (235, 235, 235), int(2 * self.HALF_W * k) + 6, cv2.LINE_AA)
        cv2.polylines(can, [cl], True, (52, 52, 52), int(2 * self.HALF_W * k) - 2, cv2.LINE_AA)
        for name, (x, y) in self.REF_POINTS.items():
            c = self._to_canvas([(x, y)])[0]
            cv2.rectangle(can, tuple(c - 12), tuple(c + 12), (255, 255, 255), -1)
            cv2.rectangle(can, tuple(c - 5), tuple(c + 5), (0, 0, 0), -1)
        if self.distractor:
            c = self._to_canvas([(80, 610)])[0]
            cv2.circle(can, tuple(c), 14, (0, 0, 255), -1)
        return can

    def render_cam(self, cam: Camera3D, size, t):
        img = cv2.warpPerspective(self._board(), cam.H_plan_to_img @ self.canvas_to_plan(), size,
                                  flags=cv2.INTER_LINEAR, borderValue=(70, 75, 80))
        order = sorted(range(len(self.cars)), key=lambda i: -cam.project(*self.car_pos(i, t))[1])
        for i in order:                          # de lejos a cerca
            x, y = self.car_pos(i, t)
            car = self.cars[i]
            (u0, v0), d0 = cam.project(x, y, 0)
            r_body = max(3, int(cam.f * 45 / d0))
            cv2.circle(img, (int(u0), int(v0)), r_body, (40, 40, 40), -1, cv2.LINE_AA)   # carrocería/sombra
            (u, v), d = cam.project(x, y, self.led_h)
            r = max(2, int(cam.f * 11 / d))
            glow = np.zeros_like(img)
            cv2.circle(glow, (int(u), int(v)), int(r * 1.8), car["bgr"], -1, cv2.LINE_AA)
            img = cv2.add(img, cv2.GaussianBlur(glow, (0, 0), max(1.0, r * 0.6)))
            cv2.circle(img, (int(u), int(v)), r, car["bgr"], -1, cv2.LINE_AA)
            cv2.circle(img, (int(u), int(v)), max(1, r // 2), tuple(min(255, c + 170) for c in car["bgr"]), -1, cv2.LINE_AA)
        return img


# Dos cámaras EN ÁNGULO, una en cada punta, que ven TODA la pista (como en la pista real)
CAM3D_A = Camera3D(position=(-900, 610, -1400), look_at=(1150, 610, 0))
CAM3D_B = Camera3D(position=(3340, 610, -1400), look_at=(1290, 610, 0))


class FakeCamState:
    def __init__(self, name="fake", fps=20.0, with_content_length=True, color=(40, 40, 200),
                 scene: "PlanScene | None" = None, H_plan_to_img: "np.ndarray | None" = None,
                 cam3d: "Camera3D | None" = None):
        self.name = name
        self.fps = fps
        self.with_content_length = with_content_length
        self.color = color
        self.lock = threading.Lock()
        self.curvals = {
            "video_size": "960x720", "quality": "70", "zoom": "100", "orientation": "landscape",
            "focusmode": "continuous-video", "whitebalance": "auto", "night_vision": "off",
            "exposure_lock": "off", "torch": "off", "video_connections": "0",
        }
        self.avail = {
            "video_size": ["1920x1080", "1280x720", "960x720", "640x480", "320x240"],
            "zoom": ["100", "150", "200", "300"],
            "orientation": ["landscape", "portrait", "upsidedown", "upsidedown_portrait"],
            "focusmode": ["auto", "continuous-video", "continuous-picture", "infinity", "macro"],
            "whitebalance": ["auto", "daylight", "fluorescent", "incandescent"],
            "night_vision": ["on", "off"],
            "exposure_lock": ["on", "off"],
        }
        self.scene = scene
        self.H_plan_to_img = H_plan_to_img
        self.cam3d = cam3d
        self.log: list[str] = []
        self.frame_no = 0
        self.drop_next_connection = False

    def size(self):
        w, h = self.curvals["video_size"].split("x")
        return int(w), int(h)

    def render(self) -> bytes:
        w, h = self.size()
        if self.scene is not None and self.cam3d is not None:
            img = self.scene.render_cam(self.cam3d, (960, 720), time.monotonic())
            if (w, h) != (960, 720):
                img = cv2.resize(img, (w, h))
            self.frame_no += 1
            ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(self.curvals.get("quality", 70))])
            return enc.tobytes()
        if self.scene is not None:
            # la homografía está definida para 960x720; se escala si cambia la resolución
            S = np.diag([w / 960, h / 720, 1.0])
            img = self.scene.render(S @ self.H_plan_to_img, (w, h), time.monotonic())
            self.frame_no += 1
            ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(self.curvals.get("quality", 70))])
            return enc.tobytes()
        img = np.full((h, w, 3), (30, 30, 30), np.uint8)
        cx, cy, rx, ry = w // 2, h // 2, int(w * 0.38), int(h * 0.33)
        cv2.ellipse(img, (cx, cy), (rx + 40, ry + 40), 0, 0, 360, (235, 235, 235), 3)
        cv2.ellipse(img, (cx, cy), (rx - 40, ry - 40), 0, 0, 360, (235, 235, 235), 3)
        for k, (x, y) in enumerate([(0.08, 0.1), (0.92, 0.1), (0.08, 0.9), (0.92, 0.9)]):
            px, py = int(x * w), int(y * h)
            cv2.rectangle(img, (px - 14, py - 14), (px + 14, py + 14), (255, 255, 255), -1)
            cv2.putText(img, str(20 + k), (px - 9, py + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
        a = (self.frame_no / max(self.fps, 1)) * 0.8
        car = (int(cx + rx * math.cos(a)), int(cy + ry * math.sin(a)))
        cv2.circle(img, car, 16, self.color, -1)
        cv2.putText(img, f"{self.name} #{self.frame_no} {w}x{h} z{self.curvals['zoom']}",
                    (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        if self.curvals.get("torch") == "on":
            cv2.putText(img, "TORCH", (12, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)
        self.frame_no += 1
        q = int(self.curvals.get("quality", 70))
        ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
        return enc.tobytes()


def make_handler(state: FakeCamState):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            p = u.path
            if p == "/video":
                return self._video()
            if p == "/shot.jpg":
                with state.lock:
                    return self._send(200, state.render(), "image/jpeg")
            if p == "/status.json":
                with state.lock:
                    data = {"curvals": dict(state.curvals)}
                    if q.get("show_avail"):
                        data["avail"] = state.avail
                return self._send(200, json.dumps(data).encode(), "application/json")
            if p.startswith("/settings/"):
                key = p.split("/", 2)[2]
                val = q.get("set", [""])[0]
                with state.lock:
                    if key in state.avail and val not in state.avail[key]:
                        return self._send(400, b"Invalid value")
                    state.curvals[key] = val
                    state.log.append(f"{key}={val}")
                return self._send(200, b"Ok")
            if p == "/ptz":
                idx = int(q.get("zoom", ["0"])[0])
                with state.lock:
                    state.curvals["zoom"] = state.avail["zoom"][min(idx, len(state.avail["zoom"]) - 1)]
                    state.log.append(f"ptz={idx}")
                return self._send(200, b"Ok")
            if p in ("/enabletorch", "/disabletorch"):
                with state.lock:
                    state.curvals["torch"] = "on" if p == "/enabletorch" else "off"
                    state.log.append(p)
                return self._send(200, b"Ok")
            if p in ("/focus", "/nofocus"):
                with state.lock:
                    state.log.append(p)
                return self._send(200, b"Ok")
            return self._send(404, b"Not found")

        def _video(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=Ba4oTvQMY8ew04N8dcnM")
            self.end_headers()
            period = 1.0 / state.fps
            sent = 0
            try:
                while True:
                    t0 = time.monotonic()
                    with state.lock:
                        jpg = state.render()
                        drop = state.drop_next_connection and sent > 5
                    if drop:
                        state.drop_next_connection = False
                        return
                    head = b"--Ba4oTvQMY8ew04N8dcnM\r\nContent-Type: image/jpeg\r\n"
                    if state.with_content_length:
                        head += f"Content-Length: {len(jpg)}\r\n".encode()
                    self.wfile.write(head + b"\r\n" + jpg + b"\r\n")
                    self.wfile.flush()
                    sent += 1
                    time.sleep(max(0.0, period - (time.monotonic() - t0)))
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return H


def serve(port: int, state: FakeCamState | None = None, host="127.0.0.1"):
    state = state or FakeCamState()
    srv = ThreadingHTTPServer((host, port), make_handler(state))
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, state


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--name", default="fake")
    ap.add_argument("--fps", type=float, default=20)
    ap.add_argument("--escena", action="store_true",
                    help="Dos cámaras (puertos PORT y PORT+1) mirando la misma pista con autos con LED")
    a = ap.parse_args()
    if a.escena:
        sc = Figure8Scene()
        srv, _ = serve(a.port, FakeCamState("A", a.fps, scene=sc, cam3d=CAM3D_A))
        srv2, _ = serve(a.port + 1, FakeCamState("B", a.fps, scene=sc, cam3d=CAM3D_B))
        print("Pista en 8, cámaras en ángulo a 1,4 m de altura en cada punta, LED a 55 mm de altura.")
        print(f"Escena simulada: cámara A http://127.0.0.1:{a.port}/video, cámara B http://127.0.0.1:{a.port + 1}/video")
        print("Puntos de referencia (mm):", PlanScene.REF_POINTS)
    else:
        srv, _ = serve(a.port, FakeCamState(a.name, a.fps))
        print(f"IP Webcam simulada en http://127.0.0.1:{a.port}/video  (Ctrl+C para salir)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.shutdown()

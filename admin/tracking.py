"""Seguimiento en vivo de vehículos por COLOR (LED RGB de cada auto) + MOVIMIENTO.

Requiere OpenCV y NumPy (import perezoso: la página funciona sin ellos, pero sin tracking).

Por cámara (un hilo por cámara, siempre con el ÚLTIMO frame; los viejos se descartan):
  1. Decodifica el JPEG y lo reduce a `process_max_width` de ancho (rendimiento).
  2. Máscara de zona válida: interior de la pista (entre borde exterior e interior, si están
     dibujados y la cámara está calibrada) menos las zonas a ignorar (de la cámara y del plano).
  3. Máscara de movimiento: MOG2 a media resolución.
  4. Por vehículo: umbral HSV alrededor del color de su LED -> blobs -> candidatos
     (centroide, área, fracción del blob que se está moviendo).
  5. Píxel -> plano (mm) con la homografía de esa cámara.

Fusión (común a todas las cámaras), en milímetros sobre el plano:
  - La identidad la da el color: no hay que asociar detecciones entre autos.
  - Movimiento: para EMPEZAR a seguir un auto se exige que el blob se mueva (evita objetos
    quietos del mismo color). Una vez seguido, alcanza con el color (aunque frene).
  - Filtro de Kalman de velocidad constante por vehículo, actualizado con cada cámara en su
    propio instante (corrige el desfase entre cámaras). En la zona compartida, ambas cámaras
    actualizan el MISMO vehículo: no hay duplicados.
  - Progreso sobre la línea central -> vueltas y posición provisional (1°, 2°, ...).

Limitaciones conocidas (ver README): calibración preliminar por homografía sin corrección de
distorsión ni de altura del LED (paralaje); colores que dependen de la exposición del teléfono.
"""
from __future__ import annotations

import collections
import logging
import math
import random
import threading
import time
from typing import Any

from . import homography as hg
from .config_store import camera_video_url, hex_to_hsv_cv

log = logging.getLogger("mcv.admin.tracking")


def missing_dependency() -> str | None:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as e:
        return (f"El seguimiento necesita OpenCV y NumPy ({e.name} no está instalado). "
                "Instalá con:  python -m pip install opencv-contrib-python numpy")
    return None


# =============================================================================== utilidades color

def hue_ranges(h: int, tol: int) -> list[tuple[int, int]]:
    lo, hi = h - tol, h + tol
    if lo < 0:
        return [(0, hi), (180 + lo, 179)]
    if hi > 179:
        return [(lo, 179), (0, hi - 180)]
    return [(lo, hi)]


def color_mask(hsv, vehicle: dict):
    import cv2
    import numpy as np
    h, _, _ = hex_to_hsv_cv(vehicle["color"])
    mask = None
    for lo, hi in hue_ranges(h, vehicle["hue_tol"]):
        m = cv2.inRange(hsv, np.array([lo, vehicle["sat_min"], vehicle["val_min"]], np.uint8),
                        np.array([hi, 255, 255], np.uint8))
        mask = m if mask is None else cv2.bitwise_or(mask, m)
    return mask


def sample_color(jpeg: bytes, x: float, y: float, radius: int = 6) -> dict:
    """Color representativo alrededor de (x, y) en píxeles de la imagen completa.

    Un LED suele verse con el centro saturado a blanco y un halo de color: se promedian solo
    los píxeles con color (saturación y brillo suficientes)."""
    import colorsys
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("No se pudo decodificar la imagen")
    H, W = img.shape[:2]
    xi, yi = int(round(x)), int(round(y))
    if not (0 <= xi < W and 0 <= yi < H):
        raise ValueError("El punto está fuera de la imagen")
    r = max(2, int(radius))
    patch = img[max(0, yi - r):yi + r + 1, max(0, xi - r):xi + r + 1]
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(float)
    sel = hsv[(hsv[:, 1] >= 60) & (hsv[:, 2] >= 60)]
    used_colored = len(sel) >= 3
    if not used_colored:
        sel = hsv
    ang = sel[:, 0] * (2 * math.pi / 180)
    hmean = (math.degrees(math.atan2(np.sin(ang).mean(), np.cos(ang).mean())) / 2) % 180
    smed, vmed = float(np.median(sel[:, 1])), float(np.median(sel[:, 2]))
    rr, gg, bb = colorsys.hsv_to_rgb(hmean / 180, smed / 255, vmed / 255)
    hexc = "#{:02x}{:02x}{:02x}".format(round(rr * 255), round(gg * 255), round(bb * 255))
    return {"color": hexc, "h": round(hmean), "s": round(smed), "v": round(vmed),
            "pixels": int(len(sel)), "only_colored_pixels": used_colored,
            "suggested": {"sat_min": int(max(40, min(200, smed * 0.55))),
                          "val_min": int(max(40, min(200, vmed * 0.55)))}}


def mask_preview(jpeg: bytes, vehicle: dict) -> tuple[bytes, dict]:
    """Imagen con los píxeles que coinciden con el color del vehículo resaltados."""
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("No se pudo decodificar la imagen")
    hsv = cv2.cvtColor(cv2.GaussianBlur(img, (5, 5), 0), cv2.COLOR_BGR2HSV)
    m = color_mask(hsv, vehicle)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    out = (img * 0.35).astype(np.uint8)
    r, g, b = (int(vehicle["color"][i:i + 2], 16) for i in (1, 3, 5))
    out[m > 0] = (b, g, r)
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    blobs = []
    for c in cnts:
        a = cv2.contourArea(c)
        ok = vehicle["area_min_px"] <= a <= vehicle["area_max_px"]
        if a >= 3:
            blobs.append(a)
            cv2.drawContours(out, [c], -1, (255, 255, 255) if ok else (90, 90, 90), 2 if ok else 1)
    blobs.sort(reverse=True)
    valid = [a for a in blobs if vehicle["area_min_px"] <= a <= vehicle["area_max_px"]]
    cv2.putText(out, f"{vehicle['name']}: {len(valid)} blob(s) validos", (10, 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    ok, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return enc.tobytes(), {"valid_blobs": len(valid), "largest_area_px": round(blobs[0]) if blobs else 0,
                           "matched_pixels": int(cv2.countNonZero(m))}


# =============================================================================== geometría de pista

class Centerline:
    """Línea central cerrada (mm): longitud de arco y proyección."""

    def __init__(self, pts: list[list[float]]):
        self.pts = [tuple(p) for p in pts]
        n = len(self.pts)
        self.cum = [0.0]
        for i in range(n):
            a, b = self.pts[i], self.pts[(i + 1) % n]
            self.cum.append(self.cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        self.length = self.cum[-1]

    def _circ(self, a: float, b: float) -> float:
        d = abs(a - b) % self.length
        return min(d, self.length - d)

    def project(self, x: float, y: float, s_hint: float | None = None,
                window: float = 600.0) -> tuple[float, float]:
        """Proyección sobre la línea central -> (s_mm, distancia).

        Con `s_hint` (posición anterior) solo se buscan tramos cercanos en la longitud de arco.
        Es imprescindible en un circuito en 8: en el cruce, el punto está igual de cerca de los
        dos tramos y sin esta pista el auto "saltaría" al otro lazo."""
        best = (math.inf, 0.0)
        n = len(self.pts)
        for i in range(n):
            if s_hint is not None and self.length > 0:
                a, b = self.cum[i], self.cum[i + 1]
                if not (a <= s_hint % self.length <= b) and min(self._circ(s_hint, a), self._circ(s_hint, b)) > window:
                    continue
            ax, ay = self.pts[i]
            bx, by = self.pts[(i + 1) % n]
            vx, vy = bx - ax, by - ay
            L2 = vx * vx + vy * vy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy) / L2))
            px, py = ax + t * vx, ay + t * vy
            d = math.hypot(x - px, y - py)
            if d < best[0]:
                best = (d, self.cum[i] + t * math.sqrt(L2))
        return best[1], best[0]  # (s_mm, distancia a la línea)

    def point_at(self, s: float) -> tuple[float, float]:
        """Inversa de `project`: punto (x, y) a la distancia de arco `s` (mm) desde el origen.

        Se usa para el modo demo (hacer "andar" un auto sintético sobre la línea central real
        o sobre la pista de ejemplo), nunca durante el seguimiento real."""
        if self.length <= 0 or not self.pts:
            return self.pts[0] if self.pts else (0.0, 0.0)
        s = s % self.length
        n = len(self.pts)
        for i in range(n):
            a, b = self.cum[i], self.cum[i + 1]
            if a <= s <= b:
                seg = b - a
                t = 0.0 if seg <= 0 else (s - a) / seg
                ax, ay = self.pts[i]
                bx, by = self.pts[(i + 1) % n]
                return ax + t * (bx - ax), ay + t * (by - ay)
        return self.pts[0]


def _synthetic_centerline_points(width_mm: float = 1800.0, height_mm: float = 1000.0,
                                  n: int = 48) -> list[list[float]]:
    """Pista ovalada genérica (elipse) para el modo demo, solo cuando todavía no hay una línea
    central real dibujada en el plano. Se dimensiona con el ancho/alto de la pista configurada
    (con margen) para que, al menos, tenga una escala parecida a la mesa real."""
    a = max(150.0, width_mm / 2 - 100.0)
    b = max(150.0, height_mm / 2 - 100.0)
    cx, cy = width_mm / 2, height_mm / 2
    return [[cx + a * math.cos(2 * math.pi * i / n), cy + b * math.sin(2 * math.pi * i / n)]
            for i in range(n)]


# =============================================================================== filtro

class KalmanCV:
    """Kalman 2D de velocidad constante. Estado [x, y, vx, vy] en mm y mm/s."""

    def __init__(self, x: float, y: float, t: float, q_acc: float = 6000.0):
        self.s = [x, y, 0.0, 0.0]
        big = 1e6
        self.P = [[200.0, 0, 0, 0], [0, 200.0, 0, 0], [0, 0, big, 0], [0, 0, 0, big]]
        self.t = t
        self.q = q_acc

    def predict(self, t: float) -> None:
        dt = t - self.t
        if dt <= 0:
            return
        x, y, vx, vy = self.s
        self.s = [x + vx * dt, y + vy * dt, vx, vy]
        F = [[1, 0, dt, 0], [0, 1, 0, dt], [0, 0, 1, 0], [0, 0, 0, 1]]
        P = self.P
        FP = [[sum(F[i][k] * P[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
        P = [[sum(FP[i][k] * F[j][k] for k in range(4)) for j in range(4)] for i in range(4)]
        q = self.q ** 2
        dt2, dt3, dt4 = dt * dt, dt ** 3, dt ** 4
        Q = [[dt4 / 4 * q, 0, dt3 / 2 * q, 0], [0, dt4 / 4 * q, 0, dt3 / 2 * q],
             [dt3 / 2 * q, 0, dt2 * q, 0], [0, dt3 / 2 * q, 0, dt2 * q]]
        self.P = [[P[i][j] + Q[i][j] for j in range(4)] for i in range(4)]
        self.t = t

    def position_at(self, t: float) -> tuple[float, float]:
        dt = max(0.0, t - self.t)
        return self.s[0] + self.s[2] * dt, self.s[1] + self.s[3] * dt

    def update(self, zx: float, zy: float, r_mm: float) -> None:
        P = self.P
        R = r_mm * r_mm
        S = [[P[0][0] + R, P[0][1]], [P[1][0], P[1][1] + R]]
        det = S[0][0] * S[1][1] - S[0][1] * S[1][0]
        if abs(det) < 1e-12:
            return
        Si = [[S[1][1] / det, -S[0][1] / det], [-S[1][0] / det, S[0][0] / det]]
        K = [[P[i][0] * Si[0][0] + P[i][1] * Si[1][0], P[i][0] * Si[0][1] + P[i][1] * Si[1][1]] for i in range(4)]
        yx, yy = zx - self.s[0], zy - self.s[1]
        self.s = [self.s[i] + K[i][0] * yx + K[i][1] * yy for i in range(4)]
        KH = [[K[i][0], K[i][1], 0, 0] for i in range(4)]
        I_KH = [[(1.0 if i == j else 0.0) - KH[i][j] for j in range(4)] for i in range(4)]
        self.P = [[sum(I_KH[i][k] * P[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


# =============================================================================== estado por vehículo

class VehicleTrack:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.kf: KalmanCV | None = None
        self.last_update = -math.inf
        self.cams_seen: dict[str, float] = {}
        self.trail: collections.deque = collections.deque(maxlen=160)
        self.laps = 0
        self.s_prev: float | None = None
        self.s = None
        self.lap_start_t: float | None = None
        self.last_lap_s: float | None = None
        self.best_lap_s: float | None = None
        self.updates = 0

    def reset(self) -> None:
        self.kf = None
        self.s_prev = None


# =============================================================================== motor

class TrackingEngine:
    def __init__(self, store, streams):
        self.store = store
        self.streams = streams
        self._lock = threading.RLock()
        self._workers: dict[str, "_CameraWorker"] = {}
        self.running = False
        self.demo = False
        self._demo_worker: "_DemoWorker | None" = None
        self.started_at: float | None = None
        self.warnings: list[str] = []
        self.tracks: dict[int, VehicleTrack] = {}
        self.centerline: Centerline | None = None
        self.cfg: dict[str, Any] = {}

    # ---------------------------------------------------------------- control
    def start(self) -> dict:
        err = missing_dependency()
        if err:
            raise RuntimeError(err)
        with self._lock:
            if self.running and not self.demo:
                return self.state()
        if self.running or self.demo:
            self.stop()  # venía del modo demo (o de un estado inconsistente): lo corta primero
        with self._lock:
            self._load_config()
            cams = [c for c in self.store.list_cameras() if c.get("enabled") and c.get("host")]
            if not cams:
                raise RuntimeError("No hay cámaras habilitadas con IP")
            self.running = True
            self.started_at = time.monotonic()
            for cam in cams:
                w = _CameraWorker(self, cam)
                self._workers[cam["id"]] = w
                w.start()
            log.info("tracking iniciado con %d cámara(s)", len(cams))
            return self.state()

    def start_demo(self) -> dict:
        """Modo demo: simula autos y una carrera sin cámaras ni hardware, para probar la
        sección Tracking y la pantalla /display. Reutiliza TODA la lógica real (Kalman,
        vueltas por línea central, ranking) a través de `ingest()`; solo cambia de dónde
        vienen las posiciones (de un hilo que las inventa, no de las cámaras)."""
        with self._lock:
            if self.demo:
                return self.state()
        if self.running:
            self.stop()  # el modo demo y el seguimiento real no corren a la vez
        with self._lock:
            self._load_config(demo=True)
            active_ids = list(self.tracks.keys())
            if not active_ids:
                raise RuntimeError("No hay vehículos configurados")
            self.demo = True
            self.running = True
            self.started_at = time.monotonic()
            worker = _DemoWorker(self, active_ids)
            self._demo_worker = worker
            worker.start()
            log.info("modo demo iniciado con %d vehículo(s)", len(active_ids))
            return self.state()

    def stop(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
            demo_worker = self._demo_worker
            self._demo_worker = None
            self.running = False
            self.demo = False
        for w in workers:
            w.stop()
        for w in workers:
            w.join(2)
        if demo_worker:
            demo_worker.stop()
            demo_worker.join(2)

    def reload(self) -> None:
        if self.demo:
            return  # el modo demo no depende de la configuración de cámaras/calibración
        if self.running:
            self.stop()
            self.start()

    def _load_config(self, demo: bool = False) -> None:
        veh = self.store.get_vehicles()
        track = self.store.get_track()
        active = veh["vehicles"][:veh["count"]]
        self.cfg = {"vehicles": active, "detection": veh["detection"], "track": track}
        self.tracks = {v["id"]: VehicleTrack(v) for v in active}
        cl = track["plan"]["centerline"]
        self.centerline = Centerline(cl["points"]) if len(cl["points"]) >= 3 and cl["closed"] else None
        self.warnings = []
        if self.centerline is None:
            if demo:
                w = float(track.get("width_mm") or 1800)
                h = float(track.get("height_mm") or 1000)
                self.centerline = Centerline(_synthetic_centerline_points(w, h))
                self.warnings.append("Modo demo: todavía no hay una línea central dibujada en el "
                                      "plano real, así que se usa una pista ovalada de ejemplo")
            else:
                self.warnings.append("Sin línea central cerrada en el plano: no se calculan vueltas ni posiciones")
        if demo:
            return  # las cámaras y su calibración no importan en modo demo
        homs = track.get("calibration", {}).get("homographies", {})
        for cam in self.store.list_cameras():
            if cam.get("enabled") and cam.get("host") and "H_img_to_plan" not in homs.get(cam["id"], {}):
                self.warnings.append(f"{cam['name']} sin calibrar: se detecta en la imagen pero no se ubica en el plano")

    # ---------------------------------------------------------------- fusión
    def ingest(self, cam_id: str, t: float, cands: dict[int, list[dict]]) -> dict[int, dict]:
        """Recibe candidatos (ya en mm si hay calibración) y devuelve el elegido por vehículo."""
        det = self.cfg["detection"]
        chosen: dict[int, dict] = {}
        with self._lock:
            for vid, lst in cands.items():
                tr = self.tracks.get(vid)
                if tr is None:
                    continue
                lst = [c for c in lst if c.get("mm") is not None]
                if not lst:
                    continue
                age = t - tr.last_update
                if tr.kf is not None and age > det["lost_after_s"]:
                    tr.reset()
                pick = None
                if tr.kf is not None:
                    px, py = tr.kf.position_at(t)
                    speed = math.hypot(tr.kf.s[2], tr.kf.s[3])
                    gate = 250.0 + speed * max(0.0, age) * 1.5
                    best = min(lst, key=lambda c: math.hypot(c["mm"][0] - px, c["mm"][1] - py))
                    if math.hypot(best["mm"][0] - px, best["mm"][1] - py) <= gate:
                        pick = best
                    elif age > 0.3:
                        moving = [c for c in lst if c["motion"] >= 0.15]
                        if moving:
                            pick = max(moving, key=lambda c: c["area"])
                            tr.reset()
                else:
                    pool = [c for c in lst if c["motion"] >= 0.15] if det["require_motion_to_start"] else lst
                    if pool:
                        pick = max(pool, key=lambda c: c["area"])
                if pick is None:
                    continue
                x, y = pick["mm"]
                if tr.kf is None:
                    tr.kf = KalmanCV(x, y, t)
                    tr.trail.clear()
                    tr.s_prev = None
                else:
                    if t >= tr.kf.t:
                        tr.kf.predict(t)
                    tr.kf.update(x, y, r_mm=20.0)
                tr.last_update = max(tr.last_update, t)
                tr.cams_seen[cam_id] = t
                tr.updates += 1
                fx, fy = tr.kf.s[0], tr.kf.s[1]
                tr.trail.append((round(fx, 1), round(fy, 1)))
                self._progress(tr, fx, fy, t)
                chosen[vid] = pick
        return chosen

    def _progress(self, tr: VehicleTrack, x: float, y: float, t: float) -> None:
        cl = self.centerline
        if cl is None or cl.length <= 0:
            return
        if tr.s_prev is not None:
            s, d = cl.project(x, y, s_hint=tr.s_prev)
            if d > 350:                       # se perdió el hilo (salto, recuperación): búsqueda global
                s, _ = cl.project(x, y)
                tr.s_prev = None              # no contar vuelta con un salto
        else:
            s, _ = cl.project(x, y)
        if tr.s_prev is not None:
            ds = s - tr.s_prev
            if ds < -cl.length / 2:      # pasó por la largada hacia adelante
                tr.laps += 1
                if tr.lap_start_t is not None:
                    lap = t - tr.lap_start_t
                    tr.last_lap_s = lap
                    tr.best_lap_s = lap if tr.best_lap_s is None else min(tr.best_lap_s, lap)
                tr.lap_start_t = t
            elif ds > cl.length / 2:     # retrocedió sobre la largada
                tr.laps -= 1
        tr.s_prev = s
        tr.s = s

    def reset_race(self) -> None:
        with self._lock:
            for tr in self.tracks.values():
                tr.laps = 0
                tr.lap_start_t = None
                tr.last_lap_s = tr.best_lap_s = None

    # ---------------------------------------------------------------- estado
    def state(self) -> dict:
        now = time.monotonic()
        with self._lock:
            det = self.cfg.get("detection", {"lost_after_s": 1.0})
            vehicles = []
            for vid, tr in self.tracks.items():
                age = now - tr.last_update
                if tr.kf is None or age > det["lost_after_s"]:
                    st = "buscando"
                elif age > 0.3:
                    st = "perdido"
                else:
                    st = "siguiendo"
                v = {"id": vid, "name": tr.cfg["name"], "color": tr.cfg["color"], "state": st,
                     "laps": tr.laps, "last_lap_s": _r(tr.last_lap_s, 2), "best_lap_s": _r(tr.best_lap_s, 2),
                     "cams": sorted(c for c, ts in tr.cams_seen.items() if now - ts < 0.5),
                     "age_ms": None if tr.kf is None else round(age * 1000)}
                if tr.kf is not None and st != "buscando":
                    x, y = tr.kf.position_at(now) if st == "siguiendo" else (tr.kf.s[0], tr.kf.s[1])
                    vx, vy = tr.kf.s[2], tr.kf.s[3]
                    sp = math.hypot(vx, vy) if tr.updates > 3 else 0.0
                    v.update(x=round(x, 1), y=round(y, 1), vx=round(vx), vy=round(vy),
                             speed_m_s=round(sp / 1000, 2), speed_scale_kmh=round(sp / 1000 * 3.6 * 24, 1),
                             trail=list(tr.trail)[-120:])
                    if tr.s is not None and self.centerline:
                        v.update(s_mm=round(tr.s), progress_pct=round(100 * tr.s / self.centerline.length, 1))
                vehicles.append(v)
            ranked = sorted((v for v in vehicles if v["state"] != "buscando" and "s_mm" in v),
                            key=lambda v: (v["laps"], v["s_mm"]), reverse=True)
            for i, v in enumerate(ranked, 1):
                v["position"] = i
            cams = {cid: w.status() for cid, w in self._workers.items()}
            return {"running": self.running, "demo": self.demo,
                    "uptime_s": round(now - self.started_at, 1) if self.running and self.started_at else 0,
                    "warnings": self.warnings, "vehicles": vehicles, "cameras": cams,
                    "centerline_mm": round(self.centerline.length) if self.centerline else None}

    def debug_worker(self, cam_id: str) -> "_CameraWorker | None":
        return self._workers.get(cam_id)


def _r(v, n):
    return None if v is None else round(v, n)


# =============================================================================== hilo por cámara

class _CameraWorker(threading.Thread):
    def __init__(self, engine: TrackingEngine, cam: dict):
        super().__init__(name=f"track:{cam['id']}", daemon=True)
        self.engine = engine
        self.cam = cam
        self.cid = cam["id"]
        self._stop_ev = threading.Event()
        track = engine.cfg["track"]
        hom = track.get("calibration", {}).get("homographies", {}).get(self.cid, {})
        self.H = hg.unflatten(hom["H_img_to_plan"]) if "H_img_to_plan" in hom else None
        self.H_size = (hom.get("image_width"), hom.get("image_height")) if self.H else None
        self.Hinv = hg.inverse(self.H) if self.H else None
        self.rms_mm = hom.get("rms_mm")
        self.pose = hom.get("camera")
        self.led_h = float(engine.cfg["detection"].get("led_height_mm", 0) or 0)
        self.mog = None
        self._mask_cache: tuple | None = None
        # métricas
        self.frames = 0
        self.dropped = 0
        self.proc_ms = 0.0
        self._times: collections.deque = collections.deque(maxlen=60)
        self.last_error = ""
        self.last_dets: list[dict] = []
        self.size_warning = ""
        # depuración (video con anotaciones)
        self.debug_cond = threading.Condition()
        self.debug_jpeg: bytes | None = None
        self.debug_seq = 0
        self.debug_wanted_until = 0.0
        self._last_debug = 0.0

    def stop(self) -> None:
        self._stop_ev.set()

    def want_debug(self) -> None:
        self.debug_wanted_until = time.monotonic() + 3.0

    def status(self) -> dict:
        now = time.monotonic()
        recent = [t for t in self._times if now - t <= 2.0]
        fps = (len(recent) - 1) / (recent[-1] - recent[0]) if len(recent) >= 2 and recent[-1] > recent[0] else 0.0
        return {"name": self.cam["name"], "calibrated": self.H is not None, "rms_mm": self.rms_mm,
                "camera_height_mm": self.pose.get("height_mm") if self.pose else None,
                "fps_processed": round(fps, 1), "proc_ms": round(self.proc_ms, 1), "frames": self.frames,
                "frames_skipped": self.dropped, "last_error": self.last_error or self.size_warning,
                "detections": self.last_dets}

    # ---------------------------------------------------------------- máscaras
    def _masks(self, w: int, h: int, scale_x: float, scale_y: float):
        import cv2
        import numpy as np
        key = (w, h)
        if self._mask_cache and self._mask_cache[0] == key:
            return self._mask_cache[1], self._mask_cache[2]
        det = self.engine.cfg["detection"]
        track = self.engine.cfg["track"]
        valid = np.full((h, w), 255, np.uint8)
        polys_dbg = []

        def plan_to_px(points):
            out = []
            for x, y in points:
                r = hg.apply(self.Hinv, x, y)
                if r is None:
                    return None
                out.append((r[0] * scale_x, r[1] * scale_y))
            return np.array(out, np.int32) if out else None

        plan = track["plan"]
        if self.Hinv is not None and det["limit_to_track"]:
            outer = plan["border_outer"]
            if len(outer["points"]) >= 3:
                po = plan_to_px(outer["points"])
                if po is not None:
                    valid[:] = 0
                    cv2.fillPoly(valid, [po], 255)
                    polys_dbg.append(("track", po))
                    for isl in plan.get("inner_islands", []):   # en un 8 hay dos islas
                        if len(isl["points"]) >= 3:
                            pi = plan_to_px(isl["points"])
                            if pi is not None:
                                cv2.fillPoly(valid, [pi], 0)
                                polys_dbg.append(("track", pi))
        zones = []
        if self.Hinv is not None:
            for z in plan["exclusion_zones"]:
                if len(z["points"]) >= 3:
                    pz = plan_to_px(z["points"])
                    if pz is not None:
                        zones.append(pz)
        cam_entry = track.get("cameras", {}).get(self.cid)
        if cam_entry:
            sx = w / cam_entry["image_width"]
            sy = h / cam_entry["image_height"]
            for z in cam_entry["layers"]["exclusion_zones"]:
                if len(z["points"]) >= 3:
                    zones.append(np.array([(p[0] * sx, p[1] * sy) for p in z["points"]], np.int32))
        for z in zones:
            cv2.fillPoly(valid, [z], 0)
            polys_dbg.append(("zone", z))
        self._mask_cache = (key, valid, polys_dbg)
        return valid, polys_dbg

    # ---------------------------------------------------------------- bucle
    def run(self) -> None:
        import cv2
        import numpy as np
        eng = self.engine
        det = eng.cfg["detection"]
        vehicles = eng.cfg["vehicles"]
        cam = self.cam
        latency = cam.get("processing", {}).get("latency_offset_ms", 0) / 1000.0
        kernel_o = np.ones((3, 3), np.uint8)
        kernel_c = np.ones((5, 5), np.uint8)
        last_seq = 0
        while not self._stop_ev.is_set():
            try:
                reader = eng.streams.get(self.cid, camera_video_url(cam), cam.get("username", ""), cam.get("password", ""))
                f = reader.wait_newer(last_seq, timeout=0.5)
                if f is None:
                    if reader.last_error:
                        self.last_error = reader.last_error
                    continue
                if last_seq and f.seq > last_seq + 1:
                    self.dropped += f.seq - last_seq - 1
                last_seq = f.seq
                t0 = time.perf_counter()
                img = cv2.imdecode(np.frombuffer(f.data, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    self.last_error = "JPEG inválido"
                    continue
                full_h, full_w = img.shape[:2]
                if self.H is not None and self.H_size and self.H_size != (full_w, full_h):
                    self.size_warning = (f"La cámara envía {full_w}×{full_h} pero se calibró con "
                                         f"{self.H_size[0]}×{self.H_size[1]}: recalibrá")
                    Hs = None
                else:
                    self.size_warning = ""
                    Hs = self.H
                scale = min(1.0, det["process_max_width"] / full_w)
                if scale < 1.0:
                    img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                h, w = img.shape[:2]
                sx = w / (self.H_size[0] if self.H_size else full_w)
                sy = h / (self.H_size[1] if self.H_size else full_h)
                valid, polys = self._masks(w, h, sx, sy)
                # movimiento (media resolución)
                small = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (w // 2, h // 2), interpolation=cv2.INTER_AREA)
                if self.mog is None:
                    self.mog = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=False)
                motion = self.mog.apply(small)
                motion = cv2.dilate(cv2.resize(motion, (w, h), interpolation=cv2.INTER_NEAREST), kernel_c, iterations=2)
                hsv = cv2.cvtColor(cv2.GaussianBlur(img, (5, 5), 0), cv2.COLOR_BGR2HSV)
                area_k = 1.0 / (scale * scale)
                cands: dict[int, list[dict]] = {}
                for v in vehicles:
                    m = cv2.bitwise_and(color_mask(hsv, v), valid)
                    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, kernel_o)
                    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, kernel_c)
                    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    lst = []
                    for c in cnts:
                        a = cv2.contourArea(c) * area_k
                        if not (v["area_min_px"] <= a <= v["area_max_px"]):
                            continue
                        mo = cv2.moments(c)
                        if mo["m00"] <= 0:
                            continue
                        cx, cy = mo["m10"] / mo["m00"], mo["m01"] / mo["m00"]
                        x0, y0, bw, bh = cv2.boundingRect(c)
                        sub_c = m[y0:y0 + bh, x0:x0 + bw]
                        sub_m = motion[y0:y0 + bh, x0:x0 + bw]
                        n = cv2.countNonZero(sub_c)
                        mf = cv2.countNonZero(cv2.bitwise_and(sub_c, sub_m)) / n if n else 0.0
                        fx, fy = cx / scale, cy / scale            # píxeles de la imagen completa
                        mm = hg.apply(Hs, fx, fy) if Hs is not None else None
                        if mm is not None and self.led_h > 0:
                            mm = hg.correct_height(mm[0], mm[1], self.pose, self.led_h)  # LED sobre el auto
                        lst.append({"px": [round(fx, 1), round(fy, 1)], "area": round(a),
                                    "motion": round(mf, 2), "mm": [round(mm[0], 1), round(mm[1], 1)] if mm else None,
                                    "_dbg": (cx, cy)})
                    lst.sort(key=lambda c: c["area"], reverse=True)
                    cands[v["id"]] = lst[:5]
                t_cap = f.t_received - latency
                chosen = eng.ingest(self.cid, t_cap, {k: [dict(c) for c in vv] for k, vv in cands.items()})
                self.proc_ms = 0.8 * self.proc_ms + 0.2 * (time.perf_counter() - t0) * 1000 if self.frames else (time.perf_counter() - t0) * 1000
                self.frames += 1
                self._times.append(time.monotonic())
                self.last_error = ""
                self.last_dets = [{"vehicle": vid, "px": c["px"], "mm": c["mm"], "area": c["area"], "motion": c["motion"]}
                                  for vid, c in chosen.items()]
                if time.monotonic() < self.debug_wanted_until and time.monotonic() - self._last_debug > 0.1:
                    self._render_debug(img, polys, motion, vehicles, cands, chosen)
            except Exception as e:  # noqa: BLE001 - un error en un frame no debe matar el hilo
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("cam %s: error procesando frame", self.cid)
                self._stop_ev.wait(0.2)

    def _render_debug(self, img, polys, motion, vehicles, cands, chosen) -> None:
        import cv2
        import numpy as np
        self._last_debug = time.monotonic()
        out = img.copy()
        tint = np.zeros_like(out)
        tint[motion > 0] = (0, 160, 255)
        out = cv2.addWeighted(out, 1.0, tint, 0.25, 0)
        for kind, p in polys:
            cv2.polylines(out, [p], True, (80, 200, 255) if kind == "track" else (60, 60, 255), 1, cv2.LINE_AA)
        for v in vehicles:
            r, g, b = (int(v["color"][i:i + 2], 16) for i in (1, 3, 5))
            pick = chosen.get(v["id"])
            for c in cands.get(v["id"], []):
                cx, cy = c["_dbg"]
                sel = pick is not None and c["px"] == pick["px"]
                cv2.circle(out, (int(cx), int(cy)), 16 if sel else 10, (b, g, r), 3 if sel else 1, cv2.LINE_AA)
                if sel:
                    cv2.putText(out, v["name"], (int(cx) + 18, int(cy) - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 4, cv2.LINE_AA)
                    cv2.putText(out, v["name"], (int(cx) + 18, int(cy) - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
        ok, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            with self.debug_cond:
                self.debug_jpeg = enc.tobytes()
                self.debug_seq += 1
                self.debug_cond.notify_all()


# =============================================================================== modo demo

class _DemoWorker(threading.Thread):
    """Hilo del modo demo: hace "andar" cada vehículo sobre la línea central (real o la pista
    ovalada de ejemplo) a una velocidad que varía con el tiempo, y alimenta esas posiciones a
    `TrackingEngine.ingest()` como si fueran detecciones de una cámara llamada "demo".

    No usa OpenCV/NumPy ni necesita cámaras: por eso también sirve para probar la sección
    Tracking y la pantalla /display antes de tener el hardware armado. No duplica NADA de la
    lógica de seguimiento (Kalman, vueltas, velocidad, ranking): todo eso sigue calculándose en
    `ingest()`/`_progress()`/`state()`, igual que con cámaras reales."""

    TICK_S = 0.05  # 20 Hz

    def __init__(self, engine: TrackingEngine, vehicle_ids: list[int]):
        super().__init__(name="demo-driver", daemon=True)
        self.engine = engine
        self._stop_ev = threading.Event()
        rng = random.Random()
        length = engine.centerline.length if engine.centerline else 1000.0
        n = max(1, len(vehicle_ids))
        self._s: dict[int, float] = {}
        self._profile: dict[int, dict] = {}
        for i, vid in enumerate(vehicle_ids):
            self._s[vid] = length * i / n  # arrancan repartidos en la pista, no amontonados
            self._profile[vid] = {
                "base": rng.uniform(260.0, 520.0),    # mm/s -> velocidad base del auto
                "amp": rng.uniform(40.0, 150.0),       # variación (acelera/frena) en mm/s
                "period": rng.uniform(4.0, 9.0),       # s, distinto por auto para que se mezclen
                "phase": rng.uniform(0.0, 2 * math.pi),
            }
        self._last_t = time.monotonic()

    def stop(self) -> None:
        self._stop_ev.set()

    def run(self) -> None:
        rng = random.Random()
        while not self._stop_ev.is_set():
            t = time.monotonic()
            dt = min(0.25, max(0.0, t - self._last_t))
            self._last_t = t
            cl = self.engine.centerline
            length = cl.length if cl else 1000.0
            for vid, prof in self._profile.items():
                speed = prof["base"] + prof["amp"] * math.sin(2 * math.pi * t / prof["period"] + prof["phase"])
                speed = max(80.0, speed)  # nunca retrocede ni se queda del todo quieto
                self._s[vid] = (self._s.get(vid, 0.0) + speed * dt) % max(length, 1.0)
                x, y = cl.point_at(self._s[vid]) if cl else (0.0, 0.0)
                x += rng.gauss(0.0, 12.0)  # ruido chico, como el de una detección real
                y += rng.gauss(0.0, 12.0)
                try:
                    self.engine.ingest("demo", t, {vid: [{"mm": (x, y), "area": 400, "motion": 1.0}]})
                except Exception:  # noqa: BLE001 - un error simulando un auto no debe matar el hilo
                    log.exception("demo: error simulando el vehículo %s", vid)
            self._stop_ev.wait(self.TICK_S)

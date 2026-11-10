"""Simulador de 3 autos RC 1:24 en pista ovalada (mm) observados por 2 camaras con solape.

Todo en coordenadas de mundo (plano de pista, mm). NO es una simulacion de imagen:
modela la salida de detector+homografia (centroide en mm + ArUco opcional) con ruido,
perdidas, rafagas de oclusion, fusion de blobs, falsos positivos, sesgo de calibracion
entre camaras y error de timestamp (desfase) de la camara B.
"""
from __future__ import annotations
import numpy as np
from dataclasses import dataclass, field

R = 900.0      # radio de curva de la linea central (mm)
LS = 3000.0    # largo de recta (mm)
L = 2 * LS + 2 * np.pi * R   # longitud de la linea central (~11655 mm)
LANES = (-150.0, 0.0, 150.0)  # offsets laterales de carril (positivo = hacia afuera)

# s=0: centro de la recta inferior (linea de meta), sentido antihorario.
_S1 = LS / 2                 # fin recta inferior derecha
_S2 = _S1 + np.pi * R        # fin curva derecha
_S3 = _S2 + LS               # fin recta superior
_S4 = _S3 + np.pi * R        # fin curva izquierda


def centerline(s: float):
    """Devuelve (punto, tangente, normal_afuera, curvatura) de la linea central en s."""
    s = s % L
    if s < _S1:
        p = np.array([s, -R]); t = np.array([1.0, 0.0]); n = np.array([0.0, -1.0]); k = 0.0
    elif s < _S2:
        a = -np.pi / 2 + (s - _S1) / R
        p = np.array([LS / 2 + R * np.cos(a), R * np.sin(a)])
        t = np.array([-np.sin(a), np.cos(a)]); n = np.array([np.cos(a), np.sin(a)]); k = 1 / R
    elif s < _S3:
        p = np.array([LS / 2 - (s - _S2), R]); t = np.array([-1.0, 0.0]); n = np.array([0.0, 1.0]); k = 0.0
    elif s < _S4:
        a = np.pi / 2 + (s - _S3) / R
        p = np.array([-LS / 2 + R * np.cos(a), R * np.sin(a)])
        t = np.array([-np.sin(a), np.cos(a)]); n = np.array([np.cos(a), np.sin(a)]); k = 1 / R
    else:
        p = np.array([-LS / 2 + (s - _S4), -R]); t = np.array([1.0, 0.0]); n = np.array([0.0, -1.0]); k = 0.0
    return p, t, n, k


def curve_factor(s: float) -> float:
    """1 dentro de curva, 0 en recta, con rampas suaves de 400 mm (para frenar antes)."""
    s = s % L
    def ramp(x):
        return float(np.clip(x / 400.0, 0, 1))
    # distancia al inicio/fin de cada curva
    f = 0.0
    for a, b in ((_S1, _S2), (_S3, _S4)):
        if a <= s <= b:
            return 1.0
        if s < a:
            f = max(f, 1 - ramp(a - s))
        if s > b:
            f = max(f, 1 - ramp(s - b))
    f = max(f, 1 - ramp(s + L - _S3 - (np.pi * R)) if s < _S1 else 0)
    return f


@dataclass
class GroundTruth:
    t: np.ndarray            # (N,)
    pos: np.ndarray          # (N, 3, 2) mm
    vel: np.ndarray          # (N, 3, 2) mm/s
    prog: np.ndarray         # (N, 3) progreso desenrollado (mm, sobre linea central)

    def at(self, tq: float):
        i = int(np.clip(np.searchsorted(self.t, tq), 1, len(self.t) - 1))
        w = (tq - self.t[i - 1]) / (self.t[i] - self.t[i - 1])
        lerp = lambda a: a[i - 1] * (1 - w) + a[i] * w
        return lerp(self.pos), lerp(self.vel), lerp(self.prog)


def simulate_cars(T: float, rng: np.random.Generator, dt: float = 0.002, lanes=LANES) -> GroundTruth:
    LANES_ = lanes
    n = int(T / dt) + 1
    v_base = np.array([1700.0, 1850.0, 2000.0]) * rng.uniform(0.95, 1.05, 3)  # mm/s
    s = np.array([0.0, -500.0, -1000.0]) % L
    sp = np.array([0.0, -500.0, -1000.0])   # progreso desenrollado
    lane = np.array([0, 1, 2])
    d = np.array([LANES_[i] for i in lane]); dd = np.zeros(3)
    v = v_base * 0.5
    ou = np.zeros(3)
    incident_until = np.zeros(3)
    drift_kick = np.zeros(3)
    T_ = np.zeros(n); P = np.zeros((n, 3, 2)); V = np.zeros((n, 3, 2)); S = np.zeros((n, 3))
    prev = None
    for k in range(n):
        t = k * dt
        for c in range(3):
            cf = curve_factor(s[c])
            ou[c] += -ou[c] / 2.0 * dt + 150.0 * np.sqrt(2 * dt / 2.0) * rng.standard_normal()
            vt = v_base[c] * (1 - 0.35 * cf) + ou[c]
            if t < incident_until[c]:
                vt = 0.2 * v_base[c]          # trompo / choque: frenada brusca
            elif rng.random() < dt / 20.0:
                incident_until[c] = t + 0.6
            # no atravesar al auto de adelante en el mismo carril
            for o in range(3):
                if o == c:
                    continue
                gap = (sp[o] - sp[c])
                if 0 < gap < 350 and abs(d[o] - d[c]) < 110:
                    vt = min(vt, v[o] * 0.98)
                    # intentar cambio de carril si esta libre
                    if rng.random() < dt / 0.5:
                        cand = [l for l in range(3) if l != lane[c]]
                        rng.shuffle(cand)
                        for l in cand:
                            free = all(not (abs(sp[q] - sp[c]) < 450 and abs(d[q] - LANES_[l]) < 110)
                                       for q in range(3) if q != c)
                            if free:
                                lane[c] = l
                                break
            tau = 0.25 if vt < v[c] else 0.5
            v[c] += (vt - v[c]) * dt / tau
            # cambio de carril espontaneo
            if rng.random() < dt / 12.0:
                l = rng.integers(0, 3)
                free = all(not (abs(sp[q] - sp[c]) < 450 and abs(d[q] - LANES_[l]) < 110)
                           for q in range(3) if q != c)
                if free:
                    lane[c] = l
            # dinamica lateral: 2o orden hacia carril + derrape centrifugo + patadas
            if cf > 0.9 and rng.random() < dt / 3.0:
                drift_kick[c] = rng.uniform(30, 80)
            drift_kick[c] *= np.exp(-dt / 0.6)
            _, _, _, kap = centerline(s[c])
            d_target = LANES_[lane[c]] + 9e-6 * v[c] ** 2 * kap * R + drift_kick[c]
            wn = 2 * np.pi * 0.8
            dd[c] += (wn ** 2 * (d_target - d[c]) - 2 * 0.8 * wn * dd[c]) * dt
            d[c] += dd[c] * dt
            ds = v[c] / (1 + d[c] * kap) * dt
            s[c] = (s[c] + ds) % L
            sp[c] += ds
            p, tg, nn, _ = centerline(s[c])
            P[k, c] = p + d[c] * nn
            S[k, c] = sp[c]
        T_[k] = t
        if prev is not None:
            V[k] = (P[k] - prev) / dt
        prev = P[k].copy()
    V[0] = V[1]
    # suavizar velocidad (derivada numerica de paso 2 ms)
    from scipy.ndimage import uniform_filter1d
    V = uniform_filter1d(V, size=5, axis=0)
    return GroundTruth(T_, P, V, S)


@dataclass
class CamSpec:
    name: str
    x_min: float
    x_max: float
    x_center: float
    bias: tuple = (0.0, 0.0)  # sesgo de calibracion residual (mm)


CAM_A = CamSpec("A", -2600, 400, -900, (0.0, 0.0))
CAM_B = CamSpec("B", -400, 2600, 900, (12.0, -9.0))   # ~15 mm de sesgo residual
OVERLAP = (-400.0, 400.0)


@dataclass
class SimDet:
    xy: np.ndarray
    sigma: float
    aruco: int | None
    gt: int            # indice de auto real (-1 = falso positivo)


@dataclass
class SimFrame:
    cam: str
    t_true: float
    t_reported: float
    dets: list


def simulate_cameras(gt: GroundTruth, rng: np.random.Generator, T: float, p_miss: float,
                     delta_b: float, fps: float = 30.0, merge_dist: float = 110.0,
                     p_fp: float = 0.05, burst_rate: float = 0.1, global_occ_rate: float = 0.03,
                     p_id_max: float = 0.75, p_wrong_id: float = 0.01):
    """Genera frames de camaras A y B. delta_b = error de timestamp de B (s): t_rep = t_true + delta_b."""
    # rafagas de oclusion por (camara, auto) y globales por auto
    def bursts(rate):
        out = []
        t = 0.0
        while True:
            t += rng.exponential(1 / rate)
            if t > T:
                return out
            out.append((t, t + rng.uniform(0.2, 0.8) if rate > 0.05 else t + rng.uniform(0.3, 1.0)))
    occ = {(cam, c): bursts(burst_rate) for cam in "AB" for c in range(3)}
    gocc = {c: bursts(global_occ_rate) for c in range(3)}
    occluded = lambda ivs, t: any(a <= t <= b for a, b in ivs)

    frames = []
    phase_b = rng.uniform(0, 1 / fps)
    for cam, phase in ((CAM_A, 0.0), (CAM_B, phase_b)):
        k = 0
        while True:
            t = 0.2 + k / fps + phase + rng.normal(0, 0.002)
            k += 1
            if t > T - 0.05:
                break
            pos, _, _ = gt.at(t)
            raw = []
            for c in range(3):
                x, y = pos[c]
                if not (cam.x_min <= x <= cam.x_max):
                    continue
                if occluded(occ[(cam.name, c)], t) or occluded(gocc[c], t):
                    continue
                if rng.random() < p_miss:
                    continue
                dn = min(1.0, abs(x - cam.x_center) / 1700.0)
                sigma = 6.0 + 6.0 * dn
                z = pos[c] + rng.normal(0, sigma, 2) + np.array(cam.bias)
                p_id = p_id_max - 0.3 * dn
                aid = None
                if rng.random() < p_id:
                    aid = c + 1
                    if rng.random() < p_wrong_id:
                        aid = int(rng.choice([i for i in (1, 2, 3) if i != c + 1]))
                raw.append(SimDet(z, sigma, aid, c))
            # fusion de blobs: dos autos muy cerca -> una deteccion en el punto medio, sin ID
            merged = []
            used = set()
            for i in range(len(raw)):
                if i in used:
                    continue
                for j in range(i + 1, len(raw)):
                    if j in used:
                        continue
                    if np.linalg.norm(raw[i].xy - raw[j].xy) < merge_dist:
                        merged.append(SimDet((raw[i].xy + raw[j].xy) / 2, 25.0, None, raw[i].gt))
                        used |= {i, j}
                        break
                if i not in used:
                    merged.append(raw[i])
            for _ in range(rng.poisson(p_fp)):
                z = np.array([rng.uniform(cam.x_min, cam.x_max), rng.uniform(-1300, 1300)])
                merged.append(SimDet(z, 12.0, None, -1))
            t_rep = t + (delta_b if cam.name == "B" else 0.0)
            frames.append(SimFrame(cam.name, t, t_rep, merged))
    frames.sort(key=lambda f: f.t_true)
    return frames

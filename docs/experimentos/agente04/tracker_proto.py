"""Prototipo PEQUENO de tracking en plano de pista (mm). Solo para el experimento de la propuesta.

- Filtros: CV, CA, IMM(CV bajo ruido + CV alto ruido).
- Asociacion: Hungaro (scipy.optimize.linear_sum_assignment) + gating Mahalanobis (chi2).
- Identidad fuerte: votos ArUco -> car_id; restricciones duras en la matriz de costos.
- Estados: TENTATIVE / CONFIRMED / LOST / DELETED con coasting maximo.
- Cada lote de detecciones se procesa en SU timestamp (prediccion al instante de la observacion).
"""
from __future__ import annotations
import enum
import math
from collections import deque
from dataclasses import dataclass, field
import numpy as np
from scipy.optimize import linear_sum_assignment

CHI2_2DOF_999 = 13.82
BIG = 1e6


class TrackState(enum.Enum):
    TENTATIVE = 0
    CONFIRMED = 1
    LOST = 2
    DELETED = 3


@dataclass
class WorldDetection:
    xy: np.ndarray            # (2,) mm en plano de pista
    cov: np.ndarray           # (2,2) mm^2
    t: float                  # timestamp de captura (s, reloj comun)
    camera_id: str
    marker_id: int | None = None
    score: float = 1.0


# ------------------------------------------------------------------ filtros
class CVFilter:
    dim = 4

    def __init__(self, z, R, q, v0_sigma=1500.0):
        self.q = q
        self.x = np.array([z[0], z[1], 0.0, 0.0])
        self.P = np.diag([R[0, 0], R[1, 1], v0_sigma ** 2, v0_sigma ** 2])

    @staticmethod
    def FQ(dt, q):
        F = np.eye(4); F[0, 2] = F[1, 3] = dt
        a, b, c = dt ** 3 / 3, dt ** 2 / 2, dt
        Q = q * np.array([[a, 0, b, 0], [0, a, 0, b], [b, 0, c, 0], [0, b, 0, c]])
        if dt < 0:
            Q = np.zeros((4, 4))
        return F, Q

    H = np.array([[1.0, 0, 0, 0], [0, 1.0, 0, 0]])

    def predict(self, dt):
        F, Q = self.FQ(dt, self.q)
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q

    def innov(self, z, R):
        y = z - self.x[:2]
        S = self.P[:2, :2] + R
        return y, S

    def update(self, z, R):
        y, S = self.innov(z, R)
        K = self.P[:, :2] @ np.linalg.inv(S)
        self.x = self.x + K @ y
        IKH = np.eye(self.dim) - K @ self._H()
        self.P = IKH @ self.P @ IKH.T + K @ R @ K.T      # forma de Joseph (estable)

    def _H(self):
        H = np.zeros((2, self.dim)); H[0, 0] = H[1, 1] = 1
        return H

    @property
    def pos(self):
        return self.x[:2]

    @property
    def vel(self):
        return self.x[2:4]

    def peek(self, dt):
        F, Q = self.FQ(dt, self.q)
        x = F @ self.x
        return x[:2], x[2:4], (F @ self.P @ F.T + Q)[:2, :2]


class CAFilter(CVFilter):
    dim = 6

    def __init__(self, z, R, q, v0_sigma=1500.0):
        self.q = q
        self.x = np.array([z[0], z[1], 0, 0, 0, 0.0])
        self.P = np.diag([R[0, 0], R[1, 1], v0_sigma ** 2, v0_sigma ** 2, 4000.0 ** 2, 4000.0 ** 2])

    @staticmethod
    def FQ(dt, q):
        F = np.eye(6)
        F[0, 2] = F[1, 3] = F[2, 4] = F[3, 5] = dt
        F[0, 4] = F[1, 5] = dt ** 2 / 2
        # ruido blanco de jerk (continuo)
        q1 = np.array([[dt ** 5 / 20, dt ** 4 / 8, dt ** 3 / 6],
                       [dt ** 4 / 8, dt ** 3 / 3, dt ** 2 / 2],
                       [dt ** 3 / 6, dt ** 2 / 2, dt]]) * q
        Q = np.zeros((6, 6))
        idx = [(0, 2, 4), (1, 3, 5)]
        for ax in idx:
            for i in range(3):
                for j in range(3):
                    Q[ax[i], ax[j]] = q1[i, j]
        if dt < 0:
            Q = np.zeros((6, 6))
        return F, Q

    def peek(self, dt):
        F, Q = self.FQ(dt, self.q)
        x = F @ self.x
        return x[:2], x[2:4], (F @ self.P @ F.T + Q)[:2, :2]


class IMMFilter:
    """IMM de 2 modelos CV (q bajo = crucero, q alto = maniobra/derrape)."""

    def __init__(self, z, R, q_low, q_high, tau_switch=0.5):
        self.m = [CVFilter(z, R, q_low), CVFilter(z, R, q_high)]
        self.mu = np.array([0.8, 0.2])
        self.tau = tau_switch
        self._c = self.mu.copy()

    def _mix(self, dt):
        p = 1 - math.exp(-max(dt, 0) / self.tau)
        PI = np.array([[1 - p, p], [p, 1 - p]])
        c = PI.T @ self.mu
        w = (PI * self.mu[:, None]) / np.maximum(c[None, :], 1e-12)  # w[i,j] = mu_{i|j}
        xs = [f.x.copy() for f in self.m]; Ps = [f.P.copy() for f in self.m]
        for j, f in enumerate(self.m):
            x0 = sum(w[i, j] * xs[i] for i in range(2))
            P0 = sum(w[i, j] * (Ps[i] + np.outer(xs[i] - x0, xs[i] - x0)) for i in range(2))
            f.x, f.P = x0, P0
        return c

    def predict(self, dt):
        self._c = self._mix(dt)
        for f in self.m:
            f.predict(dt)
        self.mu = self._c.copy()

    def _comb(self):
        x = sum(self.mu[j] * f.x for j, f in enumerate(self.m))
        P = sum(self.mu[j] * (f.P + np.outer(f.x - x, f.x - x)) for j, f in enumerate(self.m))
        return x, P

    def innov(self, z, R):
        x, P = self._comb()
        return z - x[:2], P[:2, :2] + R

    def update(self, z, R):
        lik = np.zeros(2)
        for j, f in enumerate(self.m):
            y, S = f.innov(z, R)
            lik[j] = math.exp(-0.5 * y @ np.linalg.solve(S, y)) / (2 * math.pi * math.sqrt(np.linalg.det(S)))
            f.update(z, R)
        mu = self._c * lik + 1e-300
        self.mu = mu / mu.sum()

    @property
    def x(self):
        return self._comb()[0]

    @property
    def P(self):
        return self._comb()[1]

    @property
    def pos(self):
        return self.x[:2]

    @property
    def vel(self):
        return self.x[2:4]

    def peek(self, dt):
        x, P = self._comb()
        F, Q = CVFilter.FQ(dt, self.m[0].q * self.mu[0] + self.m[1].q * self.mu[1])
        xx = F @ x
        return xx[:2], xx[2:4], (F @ P @ F.T + Q)[:2, :2]


# ------------------------------------------------------------------ track
@dataclass
class TrajectoryPoint:
    t: float
    xy: tuple
    v: tuple
    source: str           # camara(s) que actualizaron, o 'pred' si fue coasting


@dataclass
class Track:
    track_id: int
    filt: object
    t_last_pred: float
    t_last_update: float
    t_created: float
    state: TrackState = TrackState.TENTATIVE
    hits: int = 1
    car_id: int | None = None
    id_votes: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    cam_hist: dict = field(default_factory=dict)   # camara -> deque[(t, xy)] para estimar desfase


@dataclass
class TrackerConfig:
    model: str = "cv"                 # cv | ca | imm
    q_cv: float = 1e6                 # mm^2/s^3
    q_ca: float = 1e8                 # mm^2/s^5
    q_imm: tuple = (5e5, 3e7)
    gate_chi2: float = CHI2_2DOF_999
    gate_max_mm: float = 450.0
    confirm_hits: int = 3
    tentative_timeout: float = 0.15   # s sin update -> borrar tentativo
    lost_after: float = 0.10          # s sin update -> LOST
    max_coast: float = 1.0            # s en LOST -> DELETED
    use_aruco: bool = True
    id_min_votes: int = 2
    spawn_suppress_mm: float = 90.0
    merge_mm: float = 60.0
    r_floor_mm: float = 10.0          # piso de sigma para absorber sesgo residual de calibracion


class Tracker:
    def __init__(self, cfg: TrackerConfig):
        self.cfg = cfg
        self.tracks: list[Track] = []
        self._next = 1
        self.last_matches: list = []   # [(det_idx, track)] del ultimo update
        self.t_now = -1e9

    def _new_filter(self, z, R):
        c = self.cfg
        if c.model == "cv":
            return CVFilter(z, R, c.q_cv)
        if c.model == "ca":
            return CAFilter(z, R, c.q_ca)
        return IMMFilter(z, R, *c.q_imm)

    def alive(self):
        return [t for t in self.tracks if t.state != TrackState.DELETED]

    def owner_of(self, car_id):
        for tr in self.alive():
            if tr.car_id == car_id:
                return tr
        return None

    def update(self, dets: list[WorldDetection], t: float) -> list[Track]:
        c = self.cfg
        tracks = self.alive()
        for tr in tracks:
            dt = t - tr.t_last_pred
            tr.filt.predict(dt)
            tr.t_last_pred = t
        self.t_now = max(self.t_now, t)
        Rs = [d.cov + np.eye(2) * c.r_floor_mm ** 2 for d in dets]
        nT, nD = len(tracks), len(dets)
        self.last_matches = []
        matched_t, matched_d = set(), set()
        if nT and nD:
            C = np.full((nT, nD), BIG)
            for i, tr in enumerate(tracks):
                for j, d in enumerate(dets):
                    y, S = tr.filt.innov(d.xy, Rs[j])
                    m2 = float(y @ np.linalg.solve(S, y))
                    if m2 > c.gate_chi2 or np.hypot(*y) > c.gate_max_mm:
                        continue
                    cost = m2 + math.log(np.linalg.det(S))
                    if c.use_aruco and d.marker_id is not None:
                        if tr.car_id is not None:
                            if tr.car_id != d.marker_id:
                                continue          # restriccion dura: identidad incompatible
                            cost -= 6.0
                        else:
                            own = self.owner_of(d.marker_id)
                            if own is not None and own is not tr:
                                cost += 8.0
                    if tr.state == TrackState.TENTATIVE:
                        cost += 2.0               # preferir tracks confirmados
                    C[i, j] = cost
            ri, ci = linear_sum_assignment(C)
            for i, j in zip(ri, ci):
                if C[i, j] >= BIG:
                    continue
                tr, d = tracks[i], dets[j]
                tr.filt.update(d.xy, Rs[j])
                tr.t_last_update = t
                tr.hits += 1
                if tr.state == TrackState.LOST:
                    tr.state = TrackState.CONFIRMED
                elif tr.state == TrackState.TENTATIVE and tr.hits >= c.confirm_hits:
                    tr.state = TrackState.CONFIRMED
                if c.use_aruco and d.marker_id is not None:
                    tr.id_votes[d.marker_id] = tr.id_votes.get(d.marker_id, 0) + 1
                tr.cam_hist.setdefault(d.camera_id, deque(maxlen=12)).append((t, d.xy.copy()))
                tr.history.append(TrajectoryPoint(t, tuple(tr.filt.pos), tuple(tr.filt.vel), d.camera_id))
                matched_t.add(i); matched_d.add(j)
                self.last_matches.append((j, tr))
        # nacimientos
        for j, d in enumerate(dets):
            if j in matched_d:
                continue
            near = [tr for tr in tracks if tr.state in (TrackState.CONFIRMED, TrackState.LOST)
                    and np.hypot(*(tr.filt.pos - d.xy)) < c.spawn_suppress_mm]
            if near:
                continue
            R = Rs[j]
            tr = Track(self._next, self._new_filter(d.xy, R), t, t, t)
            self._next += 1
            if c.use_aruco and d.marker_id is not None:
                tr.id_votes[d.marker_id] = 1
            tr.history.append(TrajectoryPoint(t, tuple(d.xy), (0.0, 0.0), d.camera_id))
            self.tracks.append(tr)
        # ciclo de vida
        for tr in self.alive():
            age = t - tr.t_last_update
            if tr.state == TrackState.TENTATIVE and age > c.tentative_timeout:
                tr.state = TrackState.DELETED
            elif tr.state == TrackState.CONFIRMED and age > c.lost_after:
                tr.state = TrackState.LOST
            elif tr.state == TrackState.LOST and age > c.max_coast:
                tr.state = TrackState.DELETED
        if c.use_aruco:
            self._resolve_identities()
        self._merge_duplicates()
        self.tracks = [tr for tr in self.tracks if tr.state != TrackState.DELETED]
        return [tr for tr in self.tracks if tr.state in (TrackState.CONFIRMED, TrackState.LOST)]

    def _resolve_identities(self):
        c = self.cfg
        for tr in self.alive():
            if not tr.id_votes:
                continue
            best = max(tr.id_votes, key=tr.id_votes.get)
            nb, tot = tr.id_votes[best], sum(tr.id_votes.values())
            if tr.car_id is None and nb >= c.id_min_votes and nb >= 0.7 * tot:
                own = self.owner_of(best)
                if own is None:
                    tr.car_id = best
                elif own.state == TrackState.LOST or own.id_votes.get(best, 0) < nb:
                    # identidad persistente: el track viejo (perdido o mas debil) cede el ID
                    own.car_id = None
                    if own.state == TrackState.LOST:
                        own.state = TrackState.DELETED
                    tr.car_id = best
            elif tr.car_id is not None and best != tr.car_id and nb >= 2 * tr.id_votes.get(tr.car_id, 0) + 3:
                tr.car_id = None   # evidencia acumulada contradice la etiqueta

    def _merge_duplicates(self):
        al = [t for t in self.alive() if t.state in (TrackState.CONFIRMED, TrackState.LOST)]
        for i in range(len(al)):
            for j in range(i + 1, len(al)):
                a, b = al[i], al[j]
                if a.state == TrackState.DELETED or b.state == TrackState.DELETED:
                    continue
                if a.car_id is not None and b.car_id is not None and a.car_id != b.car_id:
                    continue
                pa, va, _ = a.filt.peek(self.t_now - a.t_last_pred)
                pb, vb, _ = b.filt.peek(self.t_now - b.t_last_pred)
                if np.hypot(*(pa - pb)) < self.cfg.merge_mm and np.hypot(*(va - vb)) < 600:
                    keep, drop = (a, b) if (a.car_id is not None or a.t_created <= b.t_created) and not (
                        b.car_id is not None and a.car_id is None) else (b, a)
                    for k, v in drop.id_votes.items():
                        keep.id_votes[k] = keep.id_votes.get(k, 0) + v
                    drop.state = TrackState.DELETED

    def outputs_at(self, t: float):
        """Estado de tracks reportables predicho al instante t (sin modificar el filtro)."""
        out = []
        for tr in self.tracks:
            if tr.state not in (TrackState.CONFIRMED, TrackState.LOST):
                continue
            p, v, P = tr.filt.peek(t - tr.t_last_pred)
            out.append(dict(track_id=tr.track_id, car_id=tr.car_id, xy=p, v=v, state=tr.state))
        return out


# ------------------------------------------------------------------ fusion por pares de frames (variante)
def fuse_frame_pair(da: list[WorldDetection], db: list[WorldDetection], max_mm=120.0):
    """Fusiona detecciones de A y B como si fueran simultaneas (variante 'pre-fusion por pares')."""
    if not da or not db:
        return list(da) + list(db)
    C = np.full((len(da), len(db)), BIG)
    for i, a in enumerate(da):
        for j, b in enumerate(db):
            if a.marker_id is not None and b.marker_id is not None and a.marker_id != b.marker_id:
                continue
            dd = np.hypot(*(a.xy - b.xy))
            if dd < max_mm:
                C[i, j] = dd
    ri, ci = linear_sum_assignment(C)
    out, ua, ub = [], set(), set()
    for i, j in zip(ri, ci):
        if C[i, j] >= BIG:
            continue
        a, b = da[i], db[j]
        Wa, Wb = np.linalg.inv(a.cov), np.linalg.inv(b.cov)
        cov = np.linalg.inv(Wa + Wb)
        xy = cov @ (Wa @ a.xy + Wb @ b.xy)
        out.append(WorldDetection(xy, cov, 0.5 * (a.t + b.t), "AB",
                                  a.marker_id if a.marker_id is not None else b.marker_id))
        ua.add(i); ub.add(j)
    out += [a for i, a in enumerate(da) if i not in ua] + [b for j, b in enumerate(db) if j not in ub]
    return out


# ------------------------------------------------------------------ estimador de desfase entre camaras
class ClockOffsetEstimator:
    """Estima delta = (t_reportado_B - t_real_B) con residuos en la zona de solape.

    Para cada deteccion de B asociada a un track que tiene historial reciente de A,
    ajusta una recta a las ultimas posiciones de A (<=150 ms), la evalua en t_B y usa
    r = z_B - p_A(t_B) ~= -v * (delta - delta_hat). Actualizacion por gradiente con paso eta.
    """

    def __init__(self, eta=0.03, v_min=500.0):
        self.delta = 0.0
        self.eta = eta
        self.v_min = v_min
        self.n = 0

    def observe(self, tr: Track, z_b: np.ndarray, t_b: float, ref_cam="A"):
        h = tr.cam_hist.get(ref_cam)
        if not h:
            return
        pts = [(t, xy) for t, xy in h if t_b - 0.15 <= t <= t_b]
        if len(pts) < 4:
            return
        ts = np.array([p[0] for p in pts]); xs = np.array([p[1] for p in pts])
        A = np.vstack([ts - t_b, np.ones_like(ts)]).T
        coef, *_ = np.linalg.lstsq(A, xs, rcond=None)
        v, p = coef[0], coef[1]
        sp = np.hypot(*v)
        if sp < self.v_min:
            return
        r = z_b - p
        err = -float(r @ v) / sp ** 2      # estimacion instantanea de (delta - delta_hat)
        err = float(np.clip(err, -0.05, 0.05))
        self.delta += self.eta * err
        self.n += 1

"""Progreso sobre la pista: polilinea central cerrada parametrizada por longitud de arco."""
from __future__ import annotations
import numpy as np


class CenterlinePolyline:
    def __init__(self, pts: np.ndarray):
        """pts: (N,2) mm, polilinea cerrada (el ultimo punto conecta con el primero).
        El punto pts[0] define s=0 (linea de meta)."""
        self.p = np.asarray(pts, float)
        self.q = np.roll(self.p, -1, axis=0)
        self.seg = self.q - self.p
        self.len = np.hypot(self.seg[:, 0], self.seg[:, 1])
        self.s0 = np.concatenate([[0], np.cumsum(self.len)[:-1]])
        self.L = float(self.len.sum())

    def project(self, xy, s_hint: float | None = None, window: float = 800.0):
        """Devuelve (s, d): longitud de arco de la proyeccion y distancia lateral con signo."""
        xy = np.asarray(xy, float)
        idx = np.arange(len(self.p))
        if s_hint is not None:
            ds = (self.s0 - s_hint + self.L / 2) % self.L - self.L / 2
            idx = idx[np.abs(ds) < window]
            if len(idx) == 0:
                idx = np.arange(len(self.p))
        a, v, l = self.p[idx], self.seg[idx], self.len[idx]
        u = np.clip(np.einsum("ij,ij->i", xy - a, v) / l ** 2, 0, 1)
        proj = a + u[:, None] * v
        dist = np.hypot(*(xy - proj).T)
        k = int(np.argmin(dist))
        i = idx[k]
        cross = v[k, 0] * (xy[1] - a[k, 1]) - v[k, 1] * (xy[0] - a[k, 0])
        return float((self.s0[i] + u[k] * l[k]) % self.L), float(np.sign(cross) * dist[k])


class ProgressTracker:
    """Progreso desenrollado por auto (mm acumulados) y deteccion de cruces de meta."""

    def __init__(self, line: CenterlinePolyline):
        self.line = line
        self.s = {}
        self.prog = {}
        self.crossings = {}   # car_id -> [t]

    def update(self, car_id, xy, t):
        L = self.line.L
        if car_id not in self.s:
            s, _ = self.line.project(xy)
            self.s[car_id] = s
            self.prog[car_id] = s if s <= L / 2 else s - L
            self.crossings[car_id] = []
            return self.prog[car_id]
        s, _ = self.line.project(xy, self.s[car_id])
        d = (s - self.s[car_id] + L / 2) % L - L / 2
        old = self.prog[car_id]
        new = old + d
        if np.floor(new / L) > np.floor(old / L):
            self.crossings[car_id].append(t)
        self.prog[car_id] = new
        self.s[car_id] = s
        return new

    def laps(self, car_id):
        return int(np.floor(self.prog[car_id] / self.line.L))

    def ranking(self):
        return sorted(self.prog, key=lambda c: -self.prog[c])

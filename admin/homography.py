"""Homografía plano↔imagen en Python puro (sin NumPy/OpenCV), para la calibración preliminar.

Método: DLT normalizado (Hartley) con h33 = 1 y mínimos cuadrados sobre todos los puntos
(n >= 4), resuelto con eliminación gaussiana con pivoteo parcial (sistema 8x8).

Limitación: una homografía solo es exacta para puntos SOBRE el plano del piso y sin distorsión
de lente. Un LED sobre el techo del auto (a unos 40-60 mm de altura) se proyecta desplazado (paralaje).
Ver docs/informes_agentes/AGENTE_05_CALIBRACION.md.
"""
from __future__ import annotations

import math
from typing import Sequence

Point = Sequence[float]
Matrix = list[list[float]]


class HomographyError(ValueError):
    pass


def _normalize(pts: Sequence[Point]) -> tuple[list[tuple[float, float]], Matrix]:
    n = len(pts)
    cx = sum(p[0] for p in pts) / n
    cy = sum(p[1] for p in pts) / n
    d = sum(math.hypot(p[0] - cx, p[1] - cy) for p in pts) / n
    if d < 1e-9:
        raise HomographyError("Todos los puntos coinciden")
    s = math.sqrt(2) / d
    T = [[s, 0.0, -s * cx], [0.0, s, -s * cy], [0.0, 0.0, 1.0]]
    return [((p[0] - cx) * s, (p[1] - cy) * s) for p in pts], T


def _solve(A: Matrix, b: list[float]) -> list[float]:
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-12:
            raise HomographyError("Puntos degenerados (¿tres o más alineados?)")
        M[c], M[piv] = M[piv], M[c]
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            if f:
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (M[r][n] - sum(M[r][k] * x[k] for k in range(r + 1, n))) / M[r][r]
    return x


def matmul(A: Matrix, B: Matrix) -> Matrix:
    return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def inverse(H: Matrix) -> Matrix:
    a, b, c = H[0]
    d, e, f = H[1]
    g, h, i = H[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-15:
        raise HomographyError("Homografía no invertible")
    inv = [[(e * i - f * h), -(b * i - c * h), (b * f - c * e)],
           [-(d * i - f * g), (a * i - c * g), -(a * f - c * d)],
           [(d * h - e * g), -(a * h - b * g), (a * e - b * d)]]
    return [[v / det for v in row] for row in inv]


def apply(H: Matrix, x: float, y: float) -> tuple[float, float] | None:
    w = H[2][0] * x + H[2][1] * y + H[2][2]
    if abs(w) < 1e-12:
        return None
    return ((H[0][0] * x + H[0][1] * y + H[0][2]) / w, (H[1][0] * x + H[1][1] * y + H[1][2]) / w)


def _collinear(pts: Sequence[Point]) -> bool:
    """True si no hay al menos un trío de puntos con área apreciable (todos casi alineados)."""
    n = len(pts)
    span = max(math.hypot(p[0] - q[0], p[1] - q[1]) for p in pts for q in pts) or 1.0
    for i in range(n):
        for j in range(i + 1, n):
            for k in range(j + 1, n):
                a, b, c = pts[i], pts[j], pts[k]
                area2 = abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
                if area2 > 0.02 * span * span:
                    return False
    return True


def fit(src: Sequence[Point], dst: Sequence[Point]) -> Matrix:
    """H tal que dst ~ H·src. Requiere >= 4 pares no alineados."""
    if len(src) != len(dst) or len(src) < 4:
        raise HomographyError("Se necesitan al menos 4 puntos con coordenadas en ambos sistemas")
    if _collinear(src) or _collinear(dst):
        raise HomographyError("Los puntos están casi alineados: repartilos en las cuatro esquinas de la zona")
    ns, Ts = _normalize(src)
    nd, Td = _normalize(dst)
    # Ecuaciones normales A^T A h = A^T b
    ata = [[0.0] * 8 for _ in range(8)]
    atb = [0.0] * 8
    for (x, y), (u, v) in zip(ns, nd):
        for row, rhs in (([x, y, 1, 0, 0, 0, -u * x, -u * y], u),
                         ([0, 0, 0, x, y, 1, -v * x, -v * y], v)):
            for i in range(8):
                ri = row[i]
                if ri:
                    atb[i] += ri * rhs
                    for j in range(8):
                        ata[i][j] += ri * row[j]
    h = _solve(ata, atb)
    Hn = [[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1.0]]
    H = matmul(inverse(Td), matmul(Hn, Ts))
    s = H[2][2] if abs(H[2][2]) > 1e-12 else 1.0
    return [[v / s for v in row] for row in H]


def residuals(H: Matrix, src: Sequence[Point], dst: Sequence[Point]) -> list[float]:
    out = []
    for p, q in zip(src, dst):
        r = apply(H, p[0], p[1])
        out.append(math.inf if r is None else math.hypot(r[0] - q[0], r[1] - q[1]))
    return out


def flatten(H: Matrix) -> list[float]:
    return [round(v, 12) for row in H for v in row]


def unflatten(h: Sequence[float]) -> Matrix:
    return [list(map(float, h[0:3])), list(map(float, h[3:6])), list(map(float, h[6:9]))]


# --------------------------------------------------------------------------- pose de la cámara
DEFAULT_HFOV_DEG = 65.0  # campo de visión horizontal típico de un teléfono en 4:3 (respaldo)


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def camera_pose(H_img_to_plan: Matrix, image_w: int, image_h: int) -> dict | None:
    """Estima focal y posición de la cámara (mm) a partir de la homografía del piso.

    Supone píxeles cuadrados y punto principal en el centro de la imagen (razonable en un
    teléfono). La focal se despeja de las condiciones de ortonormalidad de la rotación
    (método de Zhang con un solo plano); si la vista es casi cenital y no se puede despejar,
    se usa un campo de visión típico. Devuelve None si la geometría no es coherente.
    """
    try:
        Hp = inverse(H_img_to_plan)          # plano -> imagen
    except HomographyError:
        return None
    cx, cy = image_w / 2.0, image_h / 2.0
    col = [[Hp[r][c] for r in range(3)] for c in range(3)]     # columnas h1, h2, h3
    a = [(h[0] - cx * h[2], h[1] - cy * h[2]) for h in col]
    z = [h[2] for h in col]
    cands = []
    den1 = z[0] * z[1]
    if abs(den1) > 1e-15:
        f2 = -(a[0][0] * a[1][0] + a[0][1] * a[1][1]) / den1
        if f2 > 0:
            cands.append(f2)
    den2 = z[0] ** 2 - z[1] ** 2
    if abs(den2) > 1e-15:
        f2 = -((a[0][0] ** 2 + a[0][1] ** 2) - (a[1][0] ** 2 + a[1][1] ** 2)) / den2
        if f2 > 0:
            cands.append(f2)
    f_default = (image_w / 2.0) / math.tan(math.radians(DEFAULT_HFOV_DEG / 2))
    plausible = [math.sqrt(v) for v in cands if 0.3 * image_w < math.sqrt(v) < 3.0 * image_w]
    if plausible:
        f = math.exp(sum(math.log(v) for v in plausible) / len(plausible))
        method = "estimada"
    else:
        f, method = f_default, "supuesta (campo de visión típico)"
    m = [[(h[0] - cx * h[2]) / f, (h[1] - cy * h[2]) / f, h[2]] for h in col]
    n1 = math.sqrt(sum(v * v for v in m[0]))
    n2 = math.sqrt(sum(v * v for v in m[1]))
    if n1 < 1e-15 or n2 < 1e-15:
        return None
    lam = 2.0 / (n1 + n2)
    r1 = [lam * v for v in m[0]]
    r2 = [lam * v for v in m[1]]
    t = [lam * v for v in m[2]]
    if t[2] < 0:   # el plano tiene que estar delante de la cámara
        r1, r2, t = [-v for v in r1], [-v for v in r2], [-v for v in t]
    r3 = _cross(r1, r2)
    # centro de la cámara C = -R^T t, con R = [r1 r2 r3] (r_k son columnas): C_k = -(r_k · t)
    C = [-(r1[0] * t[0] + r1[1] * t[1] + r1[2] * t[2]),
         -(r2[0] * t[0] + r2[1] * t[1] + r2[2] * t[2]),
         -(r3[0] * t[0] + r3[1] * t[1] + r3[2] * t[2])]
    height = abs(C[2])
    ortho = abs(sum(x * y for x, y in zip(r1, r2)))
    return {"f_px": round(f, 1), "hfov_deg": round(math.degrees(2 * math.atan(image_w / (2 * f))), 1),
            "focal_method": method, "x_mm": round(C[0], 1), "y_mm": round(C[1], 1),
            "height_mm": round(height, 1), "ortho_error": round(ortho, 4)}


def correct_height(x: float, y: float, pose: dict | None, h_mm: float) -> tuple[float, float]:
    """Punto del piso donde 'cae' un objeto a altura h visto desde la cámara -> su posición real.

    La homografía ubica el LED donde su rayo de visión corta el PISO (G). El LED real está
    sobre la recta G -> cámara, a altura h: P = G + (C - G) * h / H_cámara (triángulos semejantes).
    """
    if not pose or h_mm <= 0 or pose["height_mm"] <= h_mm:
        return x, y
    k = h_mm / pose["height_mm"]
    return x + (pose["x_mm"] - x) * k, y + (pose["y_mm"] - y) * k

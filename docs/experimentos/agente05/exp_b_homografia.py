"""Experimento B (sintético): homografía piso->mundo por cámara, consistencia en la zona
compartida y error de paralaje para un marcador sobre el techo del auto.

Pista 2400 x 1200 mm (plano Z=0, X a lo largo, Y a lo ancho, Z arriba).
Dos cámaras oblicuas tipo teléfono (960x720, fx~780) a un costado de la pista.
Marcadores de referencia ArUco fijos (IDs 20-49, 50 mm) en posiciones medidas.
Monte Carlo con ruido de esquinas 0.5 y 1.0 px (+ error de colocación de marcadores).
Uso: python3 exp_b_homografia.py
"""
import numpy as np
import cv2

rng = np.random.default_rng(42)
W, H = 960, 720
K = np.array([[780.0, 0, 482.0], [0, 781.0, 357.0], [0, 0, 1]])
D = np.array([-0.085, 0.060, 0.0006, -0.0004, 0.0])
TRACK_L, TRACK_W = 2400.0, 1200.0
MARKER = 50.0
N_TRIALS = 300


def look_at(C, target):
    C = np.asarray(C, float); z = np.asarray(target, float) - C; z /= np.linalg.norm(z)
    x = np.cross(z, [0, 0, 1.0]); x /= np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.vstack([x, y, z])
    return R, -R @ C


def project(R, t, Xw, dist=None):
    rv, _ = cv2.Rodrigues(R)
    p, _ = cv2.projectPoints(np.asarray(Xw, float).reshape(-1, 1, 3), rv, t, K, D if dist is None else dist)
    return p.reshape(-1, 2)


def in_view(R, t, Xw, margin=10):
    Xc = (R @ np.asarray(Xw).T).T + t
    p = project(R, t, Xw)
    return (Xc[:, 2] > 0) & (p[:, 0] > margin) & (p[:, 0] < W - margin) & (p[:, 1] > margin) & (p[:, 1] < H - margin)


def undist(p):
    return cv2.undistortPoints(p.reshape(-1, 1, 2), K, D, P=K).reshape(-1, 2)


# --- marcadores de referencia: grilla 7 x 3 en bordes y centro (21 marcadores, IDs 20..40) ---
ref_centers = [(x, y) for y in (60.0, 600.0, 1140.0) for x in np.linspace(60, 2340, 7)]
s = MARKER / 2
ref_corners = np.array([[(cx - s, cy - s, 0), (cx + s, cy - s, 0), (cx + s, cy + s, 0), (cx - s, cy + s, 0)]
                        for cx, cy in ref_centers])  # (21,4,3)

# grilla densa de evaluación sobre la pista
gx, gy = np.meshgrid(np.linspace(0, TRACK_L, 49), np.linspace(0, TRACK_W, 25))
grid = np.stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)], 1)


def make_cams(height):
    # cámaras a 700 mm del borde largo, mirando hacia el centro de cada mitad; zona compartida ~ X 1000..1400
    A = look_at((550, -700, height), (850, 650, 0))
    B = look_at((1850, -700, height), (1550, 650, 0))
    return {"A": A, "B": B}


FAILS = [0]
OUTLIERS = 0  # nº de marcadores con posición declarada errónea (+40 mm en X) por cámara


def estimate_H(R, t, sigma_px, sigma_mm, method, use_undist=True, direction="img2world", thr=3.0):
    vis = np.array([in_view(R, t, c).all() for c in ref_corners])
    Xw = ref_corners[vis].reshape(-1, 3)
    px = project(R, t, Xw) + rng.normal(0, sigma_px, (len(Xw), 2))
    Xmeas = Xw[:, :2] + rng.normal(0, sigma_mm, (len(Xw), 2))  # error de medición de la posición del marcador
    if OUTLIERS:
        bad = rng.choice(int(vis.sum()), OUTLIERS, replace=False)
        for b in bad:
            Xmeas[4 * b:4 * b + 4, 0] += 40.0  # marcador movido / mal medido
    if use_undist:
        px = undist(px)
    if direction == "img2world":
        Hm, inl = cv2.findHomography(px, Xmeas, method, thr)  # imagen -> mundo (umbral en mm)
    else:
        two_stage = method == "2stage"
        Hw, inl = cv2.findHomography(Xmeas, px, cv2.RANSAC if two_stage else method, thr)  # mundo -> imagen (umbral px)
        if Hw is None:
            FAILS[0] += 1
            Hw, inl = cv2.findHomography(Xmeas, px, 0)
        if two_stage:  # reajuste por mínimos cuadrados sobre los inliers de RANSAC
            m = inl.ravel().astype(bool)
            Hw, _ = cv2.findHomography(Xmeas[m], px[m], 0)
        Hm = np.linalg.inv(Hw); Hm /= Hm[2, 2]
    rep = np.linalg.norm(cv2.perspectiveTransform(px.reshape(-1, 1, 2).astype(np.float64), Hm.astype(np.float64)).reshape(-1, 2) - Xmeas, axis=1)
    return Hm, int(vis.sum()), rep


def img_to_world(Hm, px, use_undist=True):
    if use_undist:
        px = undist(px)
    return cv2.perspectiveTransform(px.reshape(-1, 1, 2).astype(np.float64), Hm).reshape(-1, 2)


def run_plane(height, sigma_px, sigma_mm, method="2stage", use_undist=True, direction="world2img", thr=4.0):
    cams = make_cams(height)
    out = {}
    errs = {k: [] for k in cams}
    cross = []
    vis_grid = {k: in_view(*cams[k], grid) for k in cams}
    overlap = vis_grid["A"] & vis_grid["B"]
    nmark = {}
    rms_list = {k: [] for k in cams}
    for _ in range(N_TRIALS):
        Hs = {}
        for k, (R, t) in cams.items():
            Hs[k], nmark[k], rep = estimate_H(R, t, sigma_px, sigma_mm, method, use_undist, direction, thr)
            rms_list[k].append(np.sqrt(np.mean(rep ** 2)))
            g = grid[vis_grid[k]]
            w = img_to_world(Hs[k], project(R, t, g), use_undist)
            errs[k].append(np.linalg.norm(w - g[:, :2], axis=1))
        go = grid[overlap]
        wa = img_to_world(Hs["A"], project(*cams["A"], go), use_undist)
        wb = img_to_world(Hs["B"], project(*cams["B"], go), use_undist)
        cross.append(np.linalg.norm(wa - wb, axis=1))
    for k in cams:
        e = np.concatenate(errs[k])
        out[k] = dict(mean=e.mean(), p95=np.percentile(e, 95), max=e.max(), nmark=nmark[k],
                      cover=vis_grid[k].mean() * 100, rms_ref=np.mean(rms_list[k]))
    c = np.concatenate(cross)
    out["cross"] = dict(mean=c.mean(), p95=np.percentile(c, 95), n=int(overlap.sum()))
    return out


def pix_per_mm(height):
    """resolución en el piso: px por mm en el centro de la zona de cada cámara (cercano y lejano)."""
    cams = make_cams(height)
    R, t = cams["A"]
    res = []
    for y in (0.0, 600.0, 1200.0):
        p = project(R, t, np.array([[850, y, 0], [860, y, 0]]))
        res.append(np.linalg.norm(p[1] - p[0]) / 10)
    return res


print("=" * 78)
print("B1. Error de la homografía piso->mundo (mm) sobre la grilla de la pista")
print("    21 marcadores ref (50 mm), 300 ensayos Monte Carlo, método: RANSAC mundo->img 4 px + reajuste LSQ sobre inliers")
print("=" * 78)
for height in (600.0, 1000.0, 1600.0):
    ppm = pix_per_mm(height)
    print(f"\n-- cámaras a altura {height:.0f} mm, a 700 mm del borde; px/mm en piso (cerca/medio/lejos, cam A): "
          f"{ppm[0]:.2f}/{ppm[1]:.2f}/{ppm[2]:.2f}")
    for sp, smm in ((0.5, 0.0), (1.0, 0.0), (0.5, 1.0), (1.0, 1.0)):
        r = run_plane(height, sp, smm)
        a, b, c = r["A"], r["B"], r["cross"]
        print(f"  ruido {sp:.1f}px, colocación {smm:.1f}mm | A: media {a['mean']:.2f} p95 {a['p95']:.2f} max {a['max']:.2f} "
              f"(marc {a['nmark']}, cobertura {a['cover']:.0f}%) | B: media {b['mean']:.2f} p95 {b['p95']:.2f} | "
              f"cruzado A-B zona compartida: media {c['mean']:.2f} p95 {c['p95']:.2f} mm (n={c['n']})")

print("\n-- Sin corregir distorsión (usar píxeles crudos), altura 1000, ruido 0.5 px:")
r = run_plane(1000.0, 0.5, 0.0, use_undist=False)
print(f"  A: media {r['A']['mean']:.2f} p95 {r['A']['p95']:.2f} max {r['A']['max']:.2f} mm | B: media {r['B']['mean']:.2f} "
      f"max {r['B']['max']:.2f} | cruzado media {r['cross']['mean']:.2f} p95 {r['cross']['p95']:.2f} mm | "
      f"RMS en referencias {r['A']['rms_ref']:.2f} mm")
r2 = run_plane(1000.0, 0.5, 0.0, use_undist=True)
print(f"  (con corrección: RMS en referencias {r2['A']['rms_ref']:.2f} mm)")

print("\n-- Método/dirección de estimación (altura 1000, ruido 1.0 px + 1 mm):")
variants = (("LSQ img->mundo", 0, "img2world", 3.0), ("RANSAC img->mundo thr 3 mm", cv2.RANSAC, "img2world", 3.0),
            ("RANSAC img->mundo thr 10 mm", cv2.RANSAC, "img2world", 10.0), ("LSQ mundo->img (inv)", 0, "world2img", 2.0),
            ("RANSAC mundo->img thr 2 px", cv2.RANSAC, "world2img", 2.0), ("RANSAC mundo->img thr 4 px", cv2.RANSAC, "world2img", 4.0),
            ("RANSAC 4 px + reajuste LSQ inliers", "2stage", "world2img", 4.0),
            ("MAGSAC mundo->img thr 2 px", cv2.USAC_MAGSAC, "world2img", 2.0))
for nout in (0, 2):
    OUTLIERS = nout
    print(f"  [marcadores atípicos (+40 mm) por cámara: {nout}]")
    for name, m, d, thr in variants:
        FAILS[0] = 0
        r = run_plane(1000.0, 1.0, 1.0, method=m, direction=d, thr=thr)
        print(f"    fallos={FAILS[0]:3d} {name:34s} A media {r['A']['mean']:6.2f} p95 {r['A']['p95']:6.2f} | cruzado media {r['cross']['mean']:6.2f} mm")
OUTLIERS = 0

# ---------------------------------------------------------------------------------------
print("\n" + "=" * 78)
print("B2. Error de paralaje: punto a h = 45 mm (marcador sobre el techo) mapeado con H del piso")
print("=" * 78)
h = 45.0
for height in (600.0, 1000.0, 1600.0):
    cams = make_cams(height)
    R, t = cams["A"]
    C = -R.T @ t
    Hs, _, _ = estimate_H(R, t, 0.0, 0.0, 0)  # H exacta (sin ruido) para aislar el paralaje
    pts = grid[in_view(R, t, grid + [0, 0, h]) & in_view(R, t, grid)]
    roof = pts + [0, 0, h]
    w = img_to_world(Hs, project(R, t, roof))
    e = np.linalg.norm(w - pts[:, :2], axis=1)
    dh = np.linalg.norm(pts[:, :2] - C[:2], axis=1)
    pred = h * dh / (C[2] - h)
    elev = np.degrees(np.arctan2(C[2], dh))
    print(f"  cámara h_c={height:.0f} mm: error paralaje media {e.mean():.1f} mm, min {e.min():.1f}, max {e.max():.1f} mm "
          f"(ángulo de elevación {elev.min():.0f}-{elev.max():.0f}°); fórmula h*d/(Hc-h): max |dif| {np.abs(pred - e).max():.3f} mm")
    # desacuerdo entre cámaras para el MISMO techo en la zona compartida (afecta la deduplicación)
    RB, tB = cams["B"]
    HsB, _, _ = estimate_H(RB, tB, 0.0, 0.0, 0)
    ov = grid[in_view(R, t, grid + [0, 0, h]) & in_view(RB, tB, grid + [0, 0, h])]
    if len(ov):
        wa = img_to_world(Hs, project(R, t, ov + [0, 0, h]))
        wb = img_to_world(HsB, project(RB, tB, ov + [0, 0, h]))
        d = np.linalg.norm(wa - wb, axis=1)
        print(f"     mismo techo visto por A y B (zona compartida, sin corrección): desacuerdo media {d.mean():.1f} mm, max {d.max():.1f} mm")

print("\n-- Corrección del paralaje (altura cámara 1000 mm, Monte Carlo 300, ruido 1.0 px + 1 mm):")
cams = make_cams(1000.0)
R, t = cams["A"]
RB, tB = cams["B"]
pts = grid[in_view(R, t, grid + [0, 0, h]) & in_view(R, t, grid)]
ov = grid[in_view(R, t, grid + [0, 0, h]) & in_view(RB, tB, grid + [0, 0, h])]
res = {"sin corrección": [], "H del plano z=h (pose por solvePnP)": [], "corrección radial al pie de cámara": [],
       "idem con h mal estimada (+5 mm)": []}
cross_corr = []
cam_center_err = []


def calib_cam(R, t, sp, smm):
    vis = np.array([in_view(R, t, c).all() for c in ref_corners])
    Xw = ref_corners[vis].reshape(-1, 3)
    px = project(R, t, Xw) + rng.normal(0, sp, (len(Xw), 2))
    Xm = Xw.copy(); Xm[:, :2] += rng.normal(0, smm, (len(Xw), 2))
    pu = undist(px)
    Hw, _ = cv2.findHomography(Xm[:, :2], pu, cv2.RANSAC, 2.0)
    Hm = np.linalg.inv(Hw)
    ok, rv, tv = cv2.solvePnP(Xm, px, K, D, flags=cv2.SOLVEPNP_ITERATIVE)
    Re, _ = cv2.Rodrigues(rv)
    Ce = (-Re.T @ tv).ravel()
    # homografía del plano Z=h: X=(x,y,h) -> K[r1 r2 (h*r3+t)]
    Hh = K @ np.column_stack([Re[:, 0], Re[:, 1], h * Re[:, 2] + tv.ravel()])
    return Hm, np.linalg.inv(Hh), Ce


def radial(w0, Ce, hh):
    return Ce[:2] + (w0 - Ce[:2]) * (Ce[2] - hh) / Ce[2]


for _ in range(N_TRIALS):
    Hm, Hh_inv, Ce = calib_cam(R, t, 1.0, 1.0)
    cam_center_err.append(np.linalg.norm(Ce - (-R.T @ t)))
    p = project(R, t, pts + [0, 0, h])
    w0 = img_to_world(Hm, p)
    res["sin corrección"].append(np.linalg.norm(w0 - pts[:, :2], axis=1))
    wh = cv2.perspectiveTransform(undist(p).reshape(-1, 1, 2), Hh_inv).reshape(-1, 2)
    res["H del plano z=h (pose por solvePnP)"].append(np.linalg.norm(wh - pts[:, :2], axis=1))
    res["corrección radial al pie de cámara"].append(np.linalg.norm(radial(w0, Ce, h) - pts[:, :2], axis=1))
    res["idem con h mal estimada (+5 mm)"].append(np.linalg.norm(radial(w0, Ce, h + 5) - pts[:, :2], axis=1))
    HmB, _, CeB = calib_cam(RB, tB, 1.0, 1.0)
    wa = radial(img_to_world(Hm, project(R, t, ov + [0, 0, h])), Ce, h)
    wb = radial(img_to_world(HmB, project(RB, tB, ov + [0, 0, h])), CeB, h)
    cross_corr.append(np.linalg.norm(wa - wb, axis=1))
for k, v in res.items():
    v = np.concatenate(v)
    print(f"  {k:38s}: media {v.mean():6.2f} mm, p95 {np.percentile(v, 95):6.2f}, max {v.max():6.2f}")
c = np.concatenate(cross_corr)
print(f"  desacuerdo A-B del mismo techo tras corrección radial: media {c.mean():.2f} mm, p95 {np.percentile(c, 95):.2f} mm")
print(f"  error de posición del centro óptico estimado por solvePnP: media {np.mean(cam_center_err):.1f} mm, "
      f"p95 {np.percentile(cam_center_err, 95):.1f} mm")

# ---------------------------------------------------------------------------------------
print("\n" + "=" * 78)
print("B3. Detección de descalibración: la cámara A gira (golpe/estabilización) y la H vieja se mantiene")
print("=" * 78)
Hm, _, _ = estimate_H(R, t, 0.0, 0.0, 0)
vis = np.array([in_view(R, t, c).all() for c in ref_corners])
Xw = ref_corners[vis].reshape(-1, 3)
g = grid[in_view(R, t, grid)]
for deg in (0.1, 0.25, 0.5, 1.0):
    dR, _ = cv2.Rodrigues(np.deg2rad([deg, 0, 0]))  # pitch
    R2 = dR @ R; t2 = dR @ t
    shift_px = np.linalg.norm(project(R2, t2, Xw) - project(R, t, Xw), axis=1)
    m = in_view(R2, t2, g)
    err_mm = np.linalg.norm(img_to_world(Hm, project(R2, t2, g[m])) - g[m][:, :2], axis=1)
    ref_mm = np.linalg.norm(img_to_world(Hm, project(R2, t2, Xw)) - Xw[:, :2], axis=1)
    print(f"  giro {deg:4.2f}°: desplazamiento en imagen de esquinas ref media {shift_px.mean():.2f} px; "
          f"error en pista media {err_mm.mean():.1f} mm, max {err_mm.max():.1f} mm; residuo en referencias media {ref_mm.mean():.1f} mm")

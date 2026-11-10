"""Experimento SINTÉTICO de segmentación de autos sobre pista negra con líneas blancas.

Escena 960x720 (~2.5 mm/px, vista cenital simplificada), 3 autos 80x32 px (200x80 mm)
a ~2 m/s (27 px/frame a 30 FPS), exposición = 1/2 frame (blur por promedio de 4 subframes).
Auto 1 carrocería NEGRA (peor caso), auto 2 roja oscura, auto 3 azul. Cada auto lleva una
placa de techo blanca con borde de color (rojo/verde/azul) -> identidad por color.
Perturbaciones: sombra proyectada (x0.55), deriva global de ganancia ±8 %, salto de
iluminación -25 % en el frame 150 (luz que se apaga), ruido sigma 3, JPEG q70,
y una "mano/persona" (objeto oscuro grande) que entra entre los frames 220-260.

Métricas por método: recall (auto con blob cuyo centroide está a <20 px del centro real),
FP/frame (blobs sin auto asociado), error de centroide mediano (px).
"""
import cv2, numpy as np, json

W, H, N = 960, 720, 300
rng = np.random.default_rng(3)
COL_BODY = [(35, 35, 35), (30, 30, 120), (120, 40, 30)]  # BGR
COL_TAG = [(0, 0, 230), (0, 200, 0), (230, 60, 0)]       # borde de color en placa
HUE_RANGES = [((0, 120, 80), (8, 255, 255), (172, 120, 80), (180, 255, 255)),
              ((50, 120, 80), (70, 255, 255), None, None),
              ((100, 120, 80), (125, 255, 255), None, None)]


def background():
    bg = np.full((H, W, 3), 28, np.float32)
    cx, cy = W / 2, H / 2
    for a, b in ((380, 280), (250, 160)):  # bordes de pista blancos
        cv2.ellipse(bg, (int(cx), int(cy)), (a, b), 0, 0, 360, (215, 215, 215), 4, cv2.LINE_AA)
    cv2.line(bg, (int(cx), 40), (int(cx), 140), (215, 215, 215), 6)  # línea de meta
    # textura leve del chapadur
    bg += cv2.GaussianBlur(rng.normal(0, 4, (H, W, 1)).astype(np.float32), (0, 0), 3)[..., None]
    return bg


def car_pose(i, t):
    a, b = 315, 220
    th = 0.085 * t + i * 2.1  # rad/frame ~ 27 px/frame
    x, y = W / 2 + a * np.cos(th), H / 2 + b * np.sin(th)
    dx, dy = -a * np.sin(th), b * np.cos(th)
    return x, y, np.degrees(np.arctan2(dy, dx))


def draw_car(img, i, x, y, ang):
    # sombra (luz lateral): desplazada, multiplicativa
    sh = cv2.boxPoints(((x + 14, y + 10), (86, 36), ang)).astype(np.int32)
    m = np.zeros(img.shape[:2], np.uint8); cv2.fillConvexPoly(m, sh, 1)
    img[m == 1] *= 0.55
    body = cv2.boxPoints(((x, y), (80, 32), ang)).astype(np.int32)
    cv2.fillConvexPoly(img, body, COL_BODY[i], cv2.LINE_AA)
    tag = cv2.boxPoints(((x, y), (30, 24), ang)).astype(np.int32)
    cv2.fillConvexPoly(img, tag, COL_TAG[i], cv2.LINE_AA)
    inner = cv2.boxPoints(((x, y), (20, 16), ang)).astype(np.int32)
    cv2.fillConvexPoly(img, inner, (230, 230, 230), cv2.LINE_AA)


def frame(bg, t):
    acc = np.zeros_like(bg)
    for s in range(4):  # exposición = 0.5 frame
        img = bg.copy()
        for i in range(3):
            draw_car(img, i, *car_pose(i, t + s * 0.125))
        acc += img
    img = acc / 4
    gain = 1 + 0.08 * np.sin(t / 40) - (0.25 if t >= 150 else 0)
    img *= gain
    if 220 <= t < 260:  # intruso oscuro (mano/persona) entrando desde el borde
        cv2.circle(img, (40 + (t - 220) * 3, 650), 90, (20, 25, 30), -1)
    img += rng.normal(0, 3, img.shape)
    img = np.clip(img, 0, 255).astype(np.uint8)
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return cv2.imdecode(enc, cv2.IMREAD_COLOR)


K = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))


def blobs(mask, min_area=600, max_area=6000):
    m = cv2.morphologyEx(mask, cv2.MORPH_OPEN, K)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, K, iterations=2)
    cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cs:
        a = cv2.contourArea(c)
        if min_area <= a <= max_area:
            (cx, cy), (w, h), _ = cv2.minAreaRect(c)
            el = max(w, h) / max(1, min(w, h))
            if el < 5:  # descarta formas tipo línea
                out.append((cx, cy))
    return out


def evaluate(name, det_fn, frames, gts, start=30):
    rec, fp, errs = 0, 0, []
    tot = 0; per = [0, 0, 0]; post = [0, 0]
    for t in range(start, N):
        d = det_fn(frames[t], t)
        used = set()
        for i in range(3):
            tot += 1
            gx, gy = gts[t][i]
            best, bj = 1e9, -1
            for j, p in enumerate(d):
                if isinstance(p, tuple) and len(p) == 3 and p[2] != i:
                    continue  # métodos con identidad: solo vale su propio ID
                e = np.hypot(p[0] - gx, p[1] - gy)
                if e < best and j not in used:
                    best, bj = e, j
            if best < 20:
                rec += 1; errs.append(best); used.add(bj); per[i] += 1
                if t >= 150: post[0] += 1
        fp += len(d) - len(used)
    n = N - start
    return dict(method=name, recall=rec / tot, recall_per_car=[p / n for p in per],
                recall_after_light_drop=post[0] / (3 * (N - 150)), fp_per_frame=fp / (N - start),
                centroid_err_median_px=float(np.median(errs)) if errs else None,
                centroid_err_p95_px=float(np.percentile(errs, 95)) if errs else None)


def main():
    bg = background()
    frames = [frame(bg, t) for t in range(N)]
    # GT: centro del auto al centro de la exposición
    gts = [[car_pose(i, t + 0.1875)[:2] for i in range(3)] for t in range(N)]
    empty = cv2.imdecode(cv2.imencode(".jpg", np.clip(bg + rng.normal(0, 3, bg.shape), 0, 255).astype(np.uint8),
                                      [cv2.IMWRITE_JPEG_QUALITY, 70])[1], cv2.IMREAD_COLOR)
    res = []

    mog = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=16, detectShadows=True)
    mog.setShadowThreshold(0.5)
    mmog = {}
    for t in range(N): mmog[t] = mog.apply(frames[t], learningRate=-1)
    res.append(evaluate("MOG2 (shadows on, fg=255)", lambda f, t: blobs((mmog[t] == 255).astype(np.uint8) * 255), frames, gts))
    knn = cv2.createBackgroundSubtractorKNN(history=300, dist2Threshold=400, detectShadows=True)
    mknn = {t: knn.apply(frames[t]) for t in range(N)}
    res.append(evaluate("KNN (shadows on, fg=255)", lambda f, t: blobs((mknn[t] == 255).astype(np.uint8) * 255), frames, gts))

    eg = cv2.cvtColor(empty, cv2.COLOR_BGR2GRAY).astype(np.float32)
    def static(f, t, norm=True, shadow=True, thr=25):
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        b = eg.copy()
        if norm:  # compensación de ganancia global con estadística robusta (mediana del ratio)
            ratio = np.median((g[::8, ::8] + 1) / (b[::8, ::8] + 1))
            b *= ratio
        d = np.abs(g - b)
        m = (d > thr)
        if shadow:  # sombra: más oscuro que el fondo pero ratio en [0.4, 0.85] y fondo no-blanco
            r = (g + 1) / (b + 1)
            m &= ~((r > 0.4) & (r < 0.85) & (b < 120))
        return blobs(m.astype(np.uint8) * 255)
    res.append(evaluate("Static diff, sin normalizar, sin sombras", lambda f, t: static(f, t, False, False), frames, gts))
    res.append(evaluate("Static diff + norm. ganancia", lambda f, t: static(f, t, True, False), frames, gts))
    res.append(evaluate("Static diff + norm. + test sombra", lambda f, t: static(f, t, True, True), frames, gts))

    ec = empty.astype(np.float32)
    def static_color(f, t, thr=25, min_area=600):
        g = f.astype(np.float32)
        ratio = np.median((g[::8, ::8] + 1) / (ec[::8, ::8] + 1))
        b = ec * ratio
        d = np.abs(g - b).max(axis=2)  # máx. diferencia por canal (capta carrocerías de color)
        r = (g.mean(2) + 1) / (b.mean(2) + 1)
        m = (d > thr) & ~((r > 0.4) & (r < 0.85) & (b.mean(2) < 120) & (d < 60))
        return blobs(m.astype(np.uint8) * 255, min_area=min_area)
    res.append(evaluate("Static diff COLOR (max canal) + norm + sombra", static_color, frames, gts))
    res.append(evaluate("Static diff COLOR, min_area=300", lambda f, t: static_color(f, t, min_area=300), frames, gts))

    def color(f, t):
        hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
        out = []
        for i, (lo1, hi1, lo2, hi2) in enumerate(HUE_RANGES):
            m = cv2.inRange(hsv, lo1, hi1)
            if lo2: m |= cv2.inRange(hsv, lo2, hi2)
            m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, K)
            n, lab, st, cen = cv2.connectedComponentsWithStats(m)
            cand = [(st[k, cv2.CC_STAT_AREA], cen[k]) for k in range(1, n) if 60 <= st[k, cv2.CC_STAT_AREA] <= 1500]
            if cand:
                a, c = max(cand, key=lambda z: z[0])
                out.append((c[0], c[1], i))
        return out
    res.append(evaluate("HSV placa de color (con identidad)", color, frames, gts))
    # identidad: confusión
    json.dump(res, open("segment_results.json", "w"), indent=1)
    for r in res:
        print(f"{r['method']:42s} recall {r['recall']*100:5.1f}%  FP/frame {r['fp_per_frame']:.3f}  "
              f"err med {r['centroid_err_median_px']:.2f}  p95 {r['centroid_err_p95_px']:.2f}  por auto {[round(x*100) for x in r['recall_per_car']]}  post-apagón {r['recall_after_light_drop']*100:.0f}%")
    cv2.imwrite("seg_sample_f240.jpg", frames[240])
    cv2.imwrite("seg_mog2_f240.png", mmog[240])


if __name__ == "__main__":
    main()

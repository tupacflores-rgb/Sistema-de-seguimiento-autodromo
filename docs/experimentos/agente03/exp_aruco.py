"""Experimento SINTÉTICO: tasa de detección ArUco DICT_4X4_50 vs tamaño en px,
inclinación (vista oblicua), motion blur y compresión JPEG.

Render: marcador + zona blanca (quiet zone) sobre techo oscuro, supermuestreado 4x
y reducido con INTER_AREA (imita integración del sensor). Luego blur lineal de
longitud L px (en resolución final), ruido gaussiano sigma=3 y JPEG calidad q.

Éxito = ID correcto detectado y error medio de esquinas < 0.5*lado_px (o 2 px).
Se registran también IDs falsos (detecciones con ID != verdadero).
"""
import cv2, numpy as np, itertools, json, sys, time

rng = np.random.default_rng(1234)
DICT = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
SS = 4  # supersampling


def make_params(kind):
    p = cv2.aruco.DetectorParameters()
    if kind == "default":
        return p
    if kind == "tuned":  # para marcadores chicos (estilo app2.py + foro OpenCV)
        p.minMarkerPerimeterRate = 0.005
        p.adaptiveThreshWinSizeMin = 3
        p.adaptiveThreshWinSizeMax = 23
        p.adaptiveThreshWinSizeStep = 4
        p.minCornerDistanceRate = 0.01
        p.minDistanceToBorder = 1
        p.polygonalApproxAccuracyRate = 0.05
        p.perspectiveRemovePixelPerCell = 8
        p.perspectiveRemoveIgnoredMarginPerCell = 0.2
        p.maxErroneousBitsInBorderRate = 0.5
        p.errorCorrectionRate = 0.6
        p.useAruco3Detection = False
        p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        p.cornerRefinementWinSize = 2
        p.cornerRefinementMinAccuracy = 0.05
        return p
    if kind == "minimal":  # solo lo imprescindible para marcadores chicos + subpíxel
        p.minMarkerPerimeterRate = 0.005
        p.minCornerDistanceRate = 0.01
        p.minDistanceToBorder = 1
        p.useAruco3Detection = False
        p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        p.cornerRefinementWinSize = 2
        return p
    if kind == "recommended":  # minimal + barrido fino de umbral adaptativo
        p = make_params("minimal")
        p.adaptiveThreshWinSizeMin = 3
        p.adaptiveThreshWinSizeMax = 23
        p.adaptiveThreshWinSizeStep = 4
        return p
    if kind == "safe":  # tuned pero con verificación de bits por defecto (menos IDs falsos)
        p = make_params("tuned")
        p.maxErroneousBitsInBorderRate = 0.35
        p.errorCorrectionRate = 0.0
        return p
    raise ValueError(kind)


def render(side_px, tilt_deg, blur_px, jpeg_q, marker_id, canvas=(240, 240)):
    H, W = canvas
    # marcador base 6x6 celdas, con quiet zone blanca de 1 celda -> 8x8 celdas
    m = cv2.aruco.generateImageMarker(DICT, marker_id, 600)  # 6 celdas*100
    cell = 100
    qz = np.full((800, 800), 255, np.uint8)
    qz[100:700, 100:700] = m
    # escena supermuestreada: fondo gris pista + techo oscuro alrededor
    Hs, Ws = H * SS, W * SS
    scene = np.full((Hs, Ws), 30, np.uint8)  # pista negra
    # coordenadas del marcador (sin quiet zone) en destino
    s = side_px * SS
    c = np.array([Ws / 2 + rng.uniform(-10, 10) * SS, Hs / 2 + rng.uniform(-10, 10) * SS])
    rot = rng.uniform(0, 2 * np.pi)
    sq = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]], float) * (s / 2) * (8 / 6)  # incl qz
    # foreshortening por inclinación respecto de la normal: eje y * cos(tilt)
    sq[:, 1] *= np.cos(np.radians(tilt_deg))
    # leve perspectiva: borde lejano un poco más chico
    persp = 1 - 0.08 * np.sin(np.radians(tilt_deg))
    sq[0:2, 0] *= persp
    R = np.array([[np.cos(rot), -np.sin(rot)], [np.sin(rot), np.cos(rot)]])
    dst = (sq @ R.T) + c
    # techo oscuro (auto) más grande que el marcador
    roof = (sq * np.array([1.6, 1.6])) @ R.T + c
    cv2.fillConvexPoly(scene, roof.astype(np.int32), 15)
    src = np.array([[0, 0], [800, 0], [800, 800], [0, 800]], np.float32)
    Hm = cv2.getPerspectiveTransform(src, dst.astype(np.float32))
    warped = cv2.warpPerspective(qz, Hm, (Ws, Hs), flags=cv2.INTER_LINEAR, borderValue=0)
    mask = cv2.warpPerspective(np.full_like(qz, 255), Hm, (Ws, Hs), flags=cv2.INTER_LINEAR, borderValue=0)
    a = mask.astype(np.float32) / 255
    scene = (scene * (1 - a) + warped * a)
    # contraste realista (blanco no saturado)
    scene = 20 + scene * (200 / 255)
    img = cv2.resize(scene.astype(np.float32), (W, H), interpolation=cv2.INTER_AREA)
    # esquinas verdaderas del marcador (sin quiet zone) en resolución final
    inner = np.array([[100, 100], [700, 100], [700, 700], [100, 700]], np.float32).reshape(-1, 1, 2)
    gt = (cv2.perspectiveTransform(inner, Hm).reshape(-1, 2) - (SS - 1) / 2) / SS  # convención centro de píxel
    if blur_px > 0:
        L = int(round(blur_px))
        k = np.zeros((L * 2 + 1, L * 2 + 1), np.float32)
        ang = rng.uniform(0, np.pi)
        p0 = np.array([L, L]) - np.array([np.cos(ang), np.sin(ang)]) * blur_px / 2
        p1 = np.array([L, L]) + np.array([np.cos(ang), np.sin(ang)]) * blur_px / 2
        cv2.line(k, tuple(np.round(p0).astype(int)), tuple(np.round(p1).astype(int)), 1.0, 1, cv2.LINE_AA)
        k /= k.sum()
        img = cv2.filter2D(img, -1, k)
    img = img + rng.normal(0, 3, img.shape)
    img = np.clip(img, 0, 255).astype(np.uint8)
    if jpeg_q < 100:
        ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, jpeg_q])
        img = cv2.imdecode(enc, cv2.IMREAD_GRAYSCALE)
    return img, gt


def corner_err(det, gt):
    # mínimo sobre rotaciones cíclicas (el orden de esquinas depende de la rotación)
    best = 1e9
    for r in range(4):
        e = np.linalg.norm(np.roll(det, r, axis=0) - gt, axis=1).mean()
        best = min(best, e)
    return best


def run(N=40):
    sizes = [8, 10, 12, 14, 16, 20, 24, 28, 32, 40]
    tilts = [0, 45, 60, 70]
    blurs = [0, 2, 4, 8, 16]
    jpegs = [90, 60]
    dets = {k: cv2.aruco.ArucoDetector(DICT, make_params(k)) for k in ("default", "tuned")}
    res = []
    for size, tilt, blur, q in itertools.product(sizes, tilts, blurs, jpegs):
        stats = {k: [0, 0, []] for k in dets}  # ok, false, errs
        for i in range(N):
            mid = int(rng.integers(0, 50))
            img, gt = render(size, tilt, blur, q, mid)
            for k, d in dets.items():
                corners, ids, _ = d.detectMarkers(img)
                if ids is None:
                    continue
                for cc, ii in zip(corners, ids.ravel()):
                    if ii == mid:
                        e = corner_err(cc.reshape(-1, 2), gt)
                        if e < max(2.0, 0.5 * size):
                            stats[k][0] += 1
                            stats[k][2].append(e)
                            break
                for ii in ids.ravel():
                    if ii != mid:
                        stats[k][1] += 1
        row = dict(size=size, tilt=tilt, blur=blur, jpeg=q)
        for k, (ok, fp, errs) in stats.items():
            row[k + "_rate"] = ok / N
            row[k + "_falseIDs"] = fp
            row[k + "_cornerErr"] = float(np.median(errs)) if errs else None
        res.append(row)
    return res


if __name__ == "__main__":
    t0 = time.time()
    res = run(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
    json.dump(res, open("aruco_results.json", "w"), indent=1)
    print("elapsed", time.time() - t0)

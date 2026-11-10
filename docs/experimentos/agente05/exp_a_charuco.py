"""Experimento A (sintético): calibración intrínseca ChArUco vs tablero de ajedrez.

Cámara virtual tipo teléfono a 960x720 con distorsión radial/tangencial conocida.
Se renderizan 15 poses (algunas con el tablero parcialmente fuera de cuadro) mediante
remap exacto (pixel distorsionado -> rayo -> plano del tablero -> textura), se agrega
desenfoque y ruido, se detecta y se calibra. Se reporta RMS y error de recuperación.
Uso: python3 exp_a_charuco.py [seed]
"""
import sys
import numpy as np
import cv2

SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 0
rng = np.random.default_rng(SEED)
W, H = 960, 720
K_true = np.array([[780.0, 0, 482.0], [0, 781.0, 357.0], [0, 0, 1]])
D_true = np.array([-0.085, 0.060, 0.0006, -0.0004, 0.0])  # k1 k2 p1 p2 k3

# --- tableros: ambos 7x5 cuadros de 40 mm (ChArUco: marcadores 30 mm, DICT_5X5_100) ---
SQ = 40
DICT = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_100)
board = cv2.aruco.CharucoBoard((7, 5), SQ, 30.0, DICT)
PX_PER_MM = 6
tex_ch = board.generateImage((int(7 * SQ * PX_PER_MM), int(5 * SQ * PX_PER_MM)), marginSize=0)
# ajedrez del mismo tamaño (6x4 esquinas interiores)
tex_cb = np.zeros_like(tex_ch)
for r in range(5):
    for c in range(7):
        if (r + c) % 2 == 0:
            tex_cb[r * SQ * PX_PER_MM:(r + 1) * SQ * PX_PER_MM, c * SQ * PX_PER_MM:(c + 1) * SQ * PX_PER_MM] = 255
tex_cb = tex_cb.astype(np.uint8)
# ChArUco de OpenCV: la celda (0,0) es negra; invertimos ajedrez para mismo patrón
tex_cb = 255 - tex_cb


def pad(tex, m=60):
    return cv2.copyMakeBorder(tex, m, m, m, m, cv2.BORDER_CONSTANT, value=255), m


# rayos normalizados para cada pixel distorsionado (válido para todas las poses)
SS = 3  # supersampling 3x3 por pixel (simula integración del sensor, evita aliasing)
uu, vv = np.meshgrid((np.arange(W * SS, dtype=np.float32) + 0.5) / SS - 0.5,
                     (np.arange(H * SS, dtype=np.float32) + 0.5) / SS - 0.5)
pts = np.stack([uu.ravel(), vv.ravel()], 1).reshape(-1, 1, 2)
norm = cv2.undistortPoints(pts, K_true, D_true,
                           criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 40, 1e-9)).reshape(-1, 2)
rays = np.hstack([norm, np.ones((norm.shape[0], 1))])


def render(tex, rvec, tvec):
    texp, m = pad(tex)
    R, _ = cv2.Rodrigues(rvec)
    n = R[:, 2]
    # punto del plano X = R*[x,y,0]+t ; intersección rayo s*d con plano n·(X-t)=0
    s = (n @ tvec) / (rays @ n)
    Xc = rays * s[:, None]
    Xb = (Xc - tvec[None, :]) @ R  # coords en el tablero (mm)
    mapx = (Xb[:, 0] * PX_PER_MM + m).reshape(H * SS, W * SS).astype(np.float32)
    mapy = (Xb[:, 1] * PX_PER_MM + m).reshape(H * SS, W * SS).astype(np.float32)
    bad = (s <= 0).reshape(H * SS, W * SS)
    img = cv2.remap(texp, mapx, mapy, cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=170)
    img[bad] = 170
    img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
    img = cv2.GaussianBlur(img, (0, 0), 0.8)
    img = img.astype(np.float32) * rng.uniform(0.7, 1.0) + rng.normal(0, 3.0, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


# 15 poses: distancias 350-650 mm, inclinaciones hasta ~40°, desplazamientos laterales
poses = []
cx_b, cy_b = 7 * SQ / 2, 5 * SQ / 2
for i in range(15):
    ang = np.deg2rad(rng.uniform(-38, 38, 3) * np.array([1, 1, 0.5]))
    R, _ = cv2.Rodrigues(ang)
    dist = rng.uniform(350, 650)
    off = rng.uniform(-1, 1, 2) * np.array([170, 120]) * (1.6 if i >= 11 else 1.0)  # últimas: parcialmente fuera
    center_cam = np.array([off[0], off[1], dist])
    t = center_cam - R @ np.array([cx_b, cy_b, 0])
    poses.append((ang, t))

obj_ch, img_ch, obj_cb, img_cb = [], [], [], []
n_ch_views = n_cb_views = 0
chdet = cv2.aruco.CharucoDetector(board)
pattern = (6, 4)
objp_cb = np.zeros((24, 3), np.float32)
objp_cb[:, :2] = np.mgrid[1:7, 1:5].T.reshape(-1, 2) * SQ
for i, (rv, tv) in enumerate(poses):
    im = render(tex_ch, rv, tv)
    cc, ci, mc, mi = chdet.detectBoard(im)
    if ci is not None and len(ci) >= 6:
        op, ip = board.matchImagePoints(cc, ci)
        obj_ch.append(op.astype(np.float32)); img_ch.append(ip.astype(np.float32)); n_ch_views += 1
    if i == 0:
        cv2.imwrite(sys.path[0] + "/exp_a_view0.png", im)
    im2 = render(tex_cb, rv, tv)
    ok, corners = cv2.findChessboardCornersSB(im2, pattern, flags=cv2.CALIB_CB_ACCURACY)
    if ok:
        obj_cb.append(objp_cb.copy()); img_cb.append(corners.astype(np.float32)); n_cb_views += 1
    print(f"pose {i:2d}: charuco corners={0 if ci is None else len(ci):2d}/24  chessboard={'OK' if ok else 'FALLA'}")


def calib(objs, imgs, label):
    flags = cv2.CALIB_FIX_K3
    rms, K, D, rv, tv = cv2.calibrateCamera(objs, imgs, (W, H), None, None, flags=flags)
    D = D.ravel()
    # error de recuperación: diferencia de reproyección entre modelo verdadero y estimado sobre grilla de imagen
    g = np.stack(np.meshgrid(np.linspace(0, W - 1, 33), np.linspace(0, H - 1, 25)), -1).reshape(-1, 1, 2).astype(np.float64)
    n_true = cv2.undistortPoints(g, K_true, D_true).reshape(-1, 2)
    p_est = cv2.projectPoints(np.hstack([n_true, np.ones((len(n_true), 1))]), np.zeros(3), np.zeros(3), K, D)[0].reshape(-1, 2)
    map_err = np.linalg.norm(p_est - g.reshape(-1, 2), axis=1)
    print(f"\n[{label}] vistas usadas={len(objs)}  puntos={sum(len(o) for o in objs)}  RMS reproy={rms:.3f} px")
    globals().setdefault('RESULTS', []).append((label, rms, res.mean() if False else None))
    print(f"  fx {K[0,0]:.2f} (verd {K_true[0,0]})  fy {K[1,1]:.2f} ({K_true[1,1]})  cx {K[0,2]:.2f} ({K_true[0,2]})  cy {K[1,2]:.2f} ({K_true[1,2]})")
    print(f"  err rel fx={abs(K[0,0]-K_true[0,0])/K_true[0,0]*100:.3f}%  fy={abs(K[1,1]-K_true[1,1])/K_true[1,1]*100:.3f}%  |dc|={np.hypot(K[0,2]-K_true[0,2],K[1,2]-K_true[1,2]):.2f} px")
    print(f"  dist est = {np.round(D[:5],4)}  verd = {D_true}")
    # pixel ideal (sin distorsión) según modelo verdadero vs estimado; la parte lineal (K) la absorbe la homografía del piso
    u_true = n_true * [K_true[0,0], K_true[1,1]] + [K_true[0,2], K_true[1,2]]
    n_est = cv2.undistortPoints(g, K, D).reshape(-1, 2)
    u_est = n_est * [K_true[0,0], K_true[1,1]] + [K_true[0,2], K_true[1,2]]
    Hfit, _ = cv2.findHomography(u_est, u_true, 0)
    res = np.linalg.norm(cv2.perspectiveTransform(u_est.reshape(-1,1,2), Hfit).reshape(-1,2) - u_true, axis=1)
    print(f"  residuo de distorsión NO absorbible por homografía: media {res.mean():.3f} px, p95 {np.percentile(res,95):.3f} px, max {res.max():.3f} px")
    print(f"  error del modelo completo sobre la imagen (mapa de distorsión): media {map_err.mean():.3f} px, max {map_err.max():.3f} px")
    return rms


print(f"\nSEED={SEED}")
if obj_ch:
    calib(obj_ch, img_ch, "ChArUco")
if len(obj_cb) >= 3:
    calib(obj_cb, img_cb, "Ajedrez SB")
# cuánta distorsión hay: desplazamiento máximo distorsionado vs ideal
g = np.stack(np.meshgrid(np.linspace(0, W - 1, 33), np.linspace(0, H - 1, 25)), -1).reshape(-1, 1, 2).astype(np.float64)
n = cv2.undistortPoints(g, K_true, D_true).reshape(-1, 2)
ideal = n * [K_true[0, 0], K_true[1, 1]] + [K_true[0, 2], K_true[1, 2]]
d = np.linalg.norm(ideal - g.reshape(-1, 2), axis=1)
print(f"\nMagnitud de la distorsión verdadera: media {d.mean():.2f} px, max (esquina) {d.max():.2f} px")

"""Prueba sobre la captura REAL del usuario (haruko_captura.jpg)."""
import cv2, numpy as np, sys
from exp_aruco import make_params, DICT
im = cv2.imread(sys.argv[1]); g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
# nitidez: varianza del Laplaciano (global y en el auto)
print("lap var global", cv2.Laplacian(g, cv2.CV_64F).var(), "auto", cv2.Laplacian(g[600:830, 1720:1920], cv2.CV_64F).var())
# ¿es 1080p real o reescalado? energía de alta frecuencia por espectro
F = np.abs(np.fft.fftshift(np.fft.fft2(g.astype(float))))
h, w = g.shape; cy, cx = h // 2, w // 2
yy, xx = np.ogrid[:h, :w]; r = np.hypot((yy - cy) / h, (xx - cx) / w)
for lo, hi in ((0, .125), (.125, .25), (.25, .5)):
    print(f"energía espectral banda {lo}-{hi}: {F[(r>=lo)&(r<hi)].sum()/F.sum():.4f}")
for kind in ("default", "tuned"):
    d = cv2.aruco.ArucoDetector(DICT, make_params(kind))
    for sc in (1, 2, 3):
        roi = cv2.resize(g[600:830, 1720:1920], None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)
        c, ids, rej = d.detectMarkers(roi)
        print(kind, "escala", sc, "ids", None if ids is None else ids.ravel(), "candidatos rechazados", len(rej))
    c, ids, rej = d.detectMarkers(g); print(kind, "frame completo ids", ids, "rechazados", len(rej))

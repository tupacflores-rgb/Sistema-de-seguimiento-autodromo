"""SINTÉTICO: costo de diferencia de fondo estático en COLOR (absdiff BGR + máx canal + umbral + test sombra)."""
import cv2, numpy as np
from exp_timing import scene, bench
for th in (2, 1):
    cv2.setNumThreads(th)
    for W, H in ((960, 720), (1920, 1080)):
        fr = [scene(W, H, 0.1 * k) for k in range(30)]
        bg = np.full_like(fr[0], 28); bgs = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
        def f(x):
            # normalización de ganancia por mediana submuestreada
            g = cv2.cvtColor(x, cv2.COLOR_BGR2GRAY)
            ratio = float(np.median(g[::16, ::16])) / max(1.0, float(np.median(bgs[::16, ::16])))
            b = cv2.convertScaleAbs(bg, alpha=ratio)
            d = cv2.absdiff(x, b)
            dm = cv2.max(cv2.max(d[..., 0], d[..., 1]), d[..., 2])
            _, m = cv2.threshold(dm, 25, 255, cv2.THRESH_BINARY)
            r = cv2.divide(g, cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), scale=100)  # ratio*100
            sh = cv2.inRange(r, 40, 85) & cv2.inRange(dm, 0, 59)
            return cv2.bitwise_and(m, cv2.bitwise_not(sh))
        print(f"threads={th} {W}x{H} static_color_diff+shadow  mediana/p95 ms", [round(v, 2) for v in bench(f, fr)])

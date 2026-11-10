"""SINTÉTICO: tiempo de detectMarkers con params 'recommended' y 'minimal' (2 hilos y 1 hilo)."""
import cv2, numpy as np
from exp_timing import scene, bench
from exp_aruco import make_params, DICT
for th in (2, 1):
    cv2.setNumThreads(th)
    for W, H in ((960, 720), (1920, 1080)):
        gs = [cv2.cvtColor(scene(W, H, 0.1 * k), cv2.COLOR_BGR2GRAY) for k in range(30)]
        for k in ("default", "minimal", "recommended", "tuned"):
            d = cv2.aruco.ArucoDetector(DICT, make_params(k))
            m, p = bench(lambda g: d.detectMarkers(g), gs)
            print(f"threads={th} {W}x{H} {k:12s} mediana {m:6.2f} ms p95 {p:6.2f}")

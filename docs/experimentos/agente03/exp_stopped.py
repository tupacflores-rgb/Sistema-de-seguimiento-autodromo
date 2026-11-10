"""SINTÉTICO: ¿cuántos frames tarda MOG2/KNN en 'absorber' un auto detenido? (30 FPS)"""
import cv2, numpy as np
from exp_segment import background, draw_car, rng
bg = background()
def mk(stop):
    img = bg.copy()
    if stop: draw_car(img, 1, 480, 360, 0)
    img = np.clip(img + rng.normal(0, 3, img.shape), 0, 255).astype(np.uint8)
    return img
body = np.zeros((720, 960), np.uint8); cv2.fillConvexPoly(body, cv2.boxPoints(((480, 360), (80, 32), 0)).astype(np.int32), 1)
for name, mk_sub in [("MOG2 h=300 lr=auto", lambda: cv2.createBackgroundSubtractorMOG2(300, 16, True)),
                     ("MOG2 h=500 lr=auto", lambda: cv2.createBackgroundSubtractorMOG2(500, 16, True)),
                     ("KNN h=300", lambda: cv2.createBackgroundSubtractorKNN(300, 400, True))]:
    for lr in ([-1] if "KNN" in name else [-1, 0.001, 0.0]):
        s = mk_sub()
        for _ in range(300): s.apply(mk(False), learningRate=lr if lr != 0.0 else -1)
        lost = None
        for k in range(1800):
            m = s.apply(mk(True), learningRate=lr)
            cov = ((m == 255) & (body == 1)).sum() / body.sum()
            if cov < 0.5 and lost is None: lost = k
        print(f"{name:20s} lr={lr:<6} frames hasta cobertura<50%: {lost}  ({(lost or 0)/30:.1f} s a 30 FPS)")

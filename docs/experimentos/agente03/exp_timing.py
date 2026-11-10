"""Experimento SINTÉTICO: costo CPU por frame de detectMarkers, MOG2, KNN,
diferencia contra fondo estático, morfología+contornos y HSV inRange,
en 960x720 y 1920x1080. Workspace: 2 vCPU (NO es el Ryzen 5700X)."""
import cv2, numpy as np, time, json, os
from exp_aruco import make_params, DICT

rng = np.random.default_rng(7)


def scene(W, H, t=0.0, markers=True, side=None):
    img = np.full((H, W, 3), 28, np.uint8)  # pista negra
    for k in range(4):  # líneas blancas
        y = int(H * (0.2 + 0.2 * k))
        cv2.line(img, (0, y), (W, y + int(0.05 * H)), (220, 220, 220), max(2, W // 300))
    s = side or int(W / 960 * 24)  # lado del marcador ~24 px en 960
    for i, mid in enumerate((1, 2, 3)):
        cx = int((0.15 + 0.3 * i + 0.05 * np.sin(t + i)) * W) % W
        cy = int(H * (0.3 + 0.2 * i))
        L, Wc = int(s * 4.5), int(s * 2.2)
        cv2.rectangle(img, (cx - L // 2, cy - Wc // 2), (cx + L // 2, cy + Wc // 2), (40, 40, 160 + 30 * i), -1)
        if markers:
            m = cv2.aruco.generateImageMarker(DICT, mid, s)
            q = cv2.copyMakeBorder(m, s // 6, s // 6, s // 6, s // 6, cv2.BORDER_CONSTANT, value=255)
            h, w = q.shape
            y0, x0 = cy - h // 2, cx - w // 2
            img[y0:y0 + h, x0:x0 + w] = cv2.cvtColor(q, cv2.COLOR_GRAY2BGR)
    img = np.clip(img.astype(np.int16) + rng.normal(0, 3, img.shape).astype(np.int16), 0, 255).astype(np.uint8)
    return img


def bench(fn, frames, reps=1):
    fn(frames[0])  # warmup
    ts = []
    for _ in range(reps):
        for f in frames:
            t0 = time.perf_counter(); fn(f); ts.append((time.perf_counter() - t0) * 1000)
    return float(np.median(ts)), float(np.percentile(ts, 95))


def main():
    out = {}
    for threads in (os.cpu_count(), 1):
        cv2.setNumThreads(threads)
        for (W, H) in ((960, 720), (1920, 1080)):
            frames = [scene(W, H, t=0.1 * k) for k in range(30)]
            grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
            r = {}
            for kind in ("default", "tuned"):
                d = cv2.aruco.ArucoDetector(DICT, make_params(kind))
                ok = sum(1 for g in grays if d.detectMarkers(g)[1] is not None and len(d.detectMarkers(g)[1]) == 3)
                r[f"aruco_{kind}_fullframe"] = bench(lambda g: d.detectMarkers(g), grays) + (f"{ok}/30 frames con 3 IDs",)
            # ArUco solo en ROIs (3 recortes de 3x tamaño del auto alrededor de cada blob)
            d = cv2.aruco.ArucoDetector(DICT, make_params("tuned"))
            s = int(W / 960 * 24); R = int(s * 3.5)
            def roi_detect(g, d=d, R=R, W=W, H=H):
                for i in range(3):
                    cx, cy = int((0.15 + 0.3 * i) * W), int(H * (0.3 + 0.2 * i))
                    d.detectMarkers(g[max(0, cy - R):cy + R, max(0, cx - R):cx + R])
            r["aruco_tuned_3ROIs"] = bench(roi_detect, grays)
            for shadows in (True, False):
                mog = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=16, detectShadows=shadows)
                for f in frames[:5]: mog.apply(f)
                r[f"MOG2_bgr_shadows={shadows}"] = bench(lambda f: mog.apply(f, learningRate=0.002), frames)
            mog = cv2.createBackgroundSubtractorMOG2(detectShadows=False)
            r["MOG2_gray_noshadow"] = bench(lambda g: mog.apply(g, learningRate=0.002), grays)
            small = [cv2.resize(f, (W // 2, H // 2), interpolation=cv2.INTER_AREA) for f in frames]
            mog = cv2.createBackgroundSubtractorMOG2(detectShadows=True)
            r["MOG2_bgr_shadows_half_res"] = bench(lambda f: mog.apply(f, learningRate=0.002), small)
            knn = cv2.createBackgroundSubtractorKNN(detectShadows=True)
            r["KNN_bgr_shadows"] = bench(lambda f: knn.apply(f), frames)
            bg = scene(W, H, markers=False)
            bg[:] = 28
            bgg = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
            def static_diff(g):
                d_ = cv2.absdiff(g, bgg)
                _, m = cv2.threshold(d_, 25, 255, cv2.THRESH_BINARY)
                return m
            r["static_absdiff_thresh_gray"] = bench(static_diff, grays)
            k5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
            masks = [static_diff(g) for g in grays]
            def morph_cont(m):
                m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k5)
                m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k5, iterations=2)
                cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                return [cv2.minAreaRect(c) for c in cs if cv2.contourArea(c) > 200]
            r["morph_open_close_contours"] = bench(morph_cont, masks)
            def hsv(f):
                h = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
                return [cv2.inRange(h, (lo, 80, 60), (lo + 10, 255, 255)) for lo in (0, 50, 110)]
            r["HSV_convert+3_inRange"] = bench(hsv, frames)
            Hm = np.array([[1, 0.1, 0], [0, 1.2, 0], [0, 0.0001, 1]], np.float64)
            r["warpPerspective_to_same_size"] = bench(lambda f: cv2.warpPerspective(f, Hm, (W, H)), frames)
            r["cvtColor_BGR2GRAY"] = bench(lambda f: cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), frames)
            ok, enc = cv2.imencode(".jpg", frames[0], [cv2.IMWRITE_JPEG_QUALITY, 80])
            r["imdecode_jpeg_q80 (costo MJPEG)"] = bench(lambda e: cv2.imdecode(e, cv2.IMREAD_COLOR), [enc] * 30)
            out[f"threads={threads} {W}x{H}"] = r
    json.dump(out, open("timing_results.json", "w"), indent=1)
    for k, r in out.items():
        print("\n==", k)
        for n, v in r.items():
            print(f"  {n:40s} mediana {v[0]:7.2f} ms  p95 {v[1]:7.2f} ms  {v[2] if len(v) > 2 else ''}")


if __name__ == "__main__":
    print("cpu_count", os.cpu_count(), "cv2", cv2.__version__)
    main()

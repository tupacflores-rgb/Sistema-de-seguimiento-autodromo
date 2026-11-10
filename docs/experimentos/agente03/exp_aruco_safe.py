"""SINTÉTICO: compara default / tuned / safe en una grilla reducida (IDs falsos vs recall)."""
import itertools, numpy as np, cv2, json
import exp_aruco as E
dets = {k: cv2.aruco.ArucoDetector(E.DICT, E.make_params(k)) for k in ("default", "tuned", "safe", "minimal", "recommended")}
agg = {k: [0, 0] for k in dets}; n = 0; rows = []
for size, tilt, blur in itertools.product([12, 16, 20, 24, 32, 40], [0, 45, 60], [0, 2, 4, 8]):
    ok = {k: 0 for k in dets}
    for i in range(40):
        mid = int(E.rng.integers(0, 50)); img, gt = E.render(size, tilt, blur, 60, mid); n += 1
        for k, d in dets.items():
            c, ids, _ = d.detectMarkers(img)
            if ids is None: continue
            ids = ids.ravel()
            if mid in ids: agg[k][0] += 1; ok[k] += 1
            agg[k][1] += int((ids != mid).sum())
    rows.append((size, tilt, blur, ok))
for k, (o, f) in agg.items(): print(f"{k:8s} recall {o/n*100:5.1f}%  IDs falsos {f} en {n} imágenes ({f/n*100:.2f}%)")
json.dump({"agg": agg, "n": n, "rows": rows}, open("aruco_safe_results.json", "w"))

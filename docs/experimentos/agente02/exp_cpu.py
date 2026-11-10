"""Experimento 4: costo de decodificación JPEG (cv2.imdecode) y CPU por backend (localhost)."""
import sys, os, time, json, resource
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2, numpy as np
from capture_proto import CameraConfig, CameraSource
res = {}
rng = np.random.default_rng(1)
for (w, h) in [(640, 480), (960, 720), (1920, 1080)]:
    img = cv2.GaussianBlur(rng.integers(0, 255, (h, w, 3), dtype=np.uint8), (0, 0), 2)
    jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])[1]
    for flag, name in [(cv2.IMREAD_COLOR, "color"), (cv2.IMREAD_REDUCED_COLOR_2, "reduced2")]:
        cv2.imdecode(jpg, flag)
        t = time.perf_counter(); n = 100
        for _ in range(n): cv2.imdecode(jpg, flag)
        res[f"imdecode_{w}x{h}_{name}_ms"] = round((time.perf_counter() - t) / n * 1000, 2)
    res[f"jpeg_kB_{w}x{h}_q80"] = round(len(jpg) / 1024, 1)
url = sys.argv[1]
for backend in ("mjpeg", "ffmpeg"):
    src = CameraSource(CameraConfig("A", url, backend=backend)); src.start(); time.sleep(1)
    r0 = resource.getrusage(resource.RUSAGE_SELF); f0 = src.stats["frames"]; t0 = time.perf_counter()
    time.sleep(10)
    r1 = resource.getrusage(resource.RUSAGE_SELF); f1 = src.stats["frames"]; dt = time.perf_counter() - t0
    cpu = (r1.ru_utime - r0.ru_utime + r1.ru_stime - r0.ru_stime)
    res[f"cpu_{backend}_640x480_30fps_pct_one_core"] = round(cpu / dt * 100, 1)
    res[f"fps_{backend}"] = round((f1 - f0) / dt, 1)
    src.stop()
print(json.dumps(res, indent=1))
json.dump(res, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "res_cpu.json"), "w"), indent=1)

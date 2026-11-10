"""Experimento 2b: qué hace cv2.VideoCapture(CAP_FFMPEG) cuando el servidor cierra el socket (FIN)."""
import sys, time, urllib.request, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
from mjpeg_server import decode_frame_meta
URL = sys.argv[1]
cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG, [cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2000])
for _ in range(30): cap.read()
base = URL.rsplit('/', 1)[0]
urllib.request.urlopen(base + "/ctl?drop=1").read()
t0 = time.monotonic(); last = None; log = []
while time.monotonic() - t0 < 4:
    ts = time.monotonic(); ok, img = cap.read(); dt = time.monotonic() - ts
    if not ok:
        log.append(("FAIL", round(ts - t0, 3), round(dt, 3))); break
    seq = decode_frame_meta(img)[0]
    if last is not None and seq != last + 1:
        log.append(("gap", round(ts - t0, 3), last, seq, round(dt, 3)))
    last = seq
print("log:", log[:10], "final_seq", last)

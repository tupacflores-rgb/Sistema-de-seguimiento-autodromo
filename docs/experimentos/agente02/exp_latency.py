"""Experimento 1: latencia y acumulación de buffer.

Compara, contra el servidor sintético (localhost):
  A) cv2.VideoCapture(url, CAP_FFMPEG) leído en el bucle del consumidor
  B) lector multipart manual (http.client + imdecode) leído en el bucle del consumidor
  C) CameraSource (hilo lector + "último frame") con backend mjpeg
  D) CameraSource con backend ffmpeg
con distintos tiempos de procesamiento simulados por frame (P).

Latencia = time.time() al obtener el frame - t_gen codificado en el frame (ms).
Uso: python exp_latency.py URL DURACION_S
"""
import json
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from capture_proto import CameraConfig, CameraSource, _MjpegHttpReader  # noqa: E402
from mjpeg_server import decode_frame_meta  # noqa: E402


def lat_ms(img):
    seq, tg, _ = decode_frame_meta(img)
    now = time.time_ns() // 1_000_000
    return seq, ((now - tg) & 0xFFFFFFFF)


def summarize(samples, dur):
    # samples: (t_rel, seq, lat_ms)
    a = np.array([s[2] for s in samples], float)
    t = np.array([s[0] for s in samples], float)
    seqs = np.array([s[1] for s in samples])
    first = a[t < 3.0]
    last = a[t > dur - 3.0]
    gaps = np.diff(seqs)
    return dict(
        n=len(a), fps_consumed=round(len(a) / dur, 1),
        lat_med=round(float(np.median(a)), 1), lat_p95=round(float(np.percentile(a, 95)), 1),
        lat_max=round(float(a.max()), 1),
        lat_first3s_med=round(float(np.median(first)), 1) if len(first) else None,
        lat_last3s_med=round(float(np.median(last)), 1) if len(last) else None,
        seq_skipped=int((gaps - 1).clip(min=0).sum()),
    )


def run_direct_ffmpeg(url, dur, proc_s, opts_env=None):
    if opts_env is not None:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = opts_env
    else:
        os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
    t0 = time.perf_counter()
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    t_open = time.perf_counter() - t0
    set_ok = cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    got = cap.get(cv2.CAP_PROP_BUFFERSIZE)
    ok, img = cap.read()
    t_first = time.perf_counter() - t0
    samples = []
    ts = time.perf_counter()
    while time.perf_counter() - ts < dur:
        ok, img = cap.read()
        if not ok:
            break
        seq, l = lat_ms(img)
        samples.append((time.perf_counter() - ts, seq, l))
        if proc_s:
            time.sleep(proc_s)
    cap.release()
    r = summarize(samples, dur)
    r.update(open_s=round(t_open, 3), first_frame_s=round(t_first, 3),
             bufsize_set_returned=bool(set_ok), bufsize_get=got)
    return r


def run_direct_mjpeg(url, dur, proc_s):
    t0 = time.perf_counter()
    rd = _MjpegHttpReader(url, 3, 3)
    rd.open()
    t_open = time.perf_counter() - t0
    samples = []
    ts = time.perf_counter()
    first = None
    while time.perf_counter() - ts < dur:
        jpg = rd.read_jpeg()
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if first is None:
            first = time.perf_counter() - t0
        seq, l = lat_ms(img)
        samples.append((time.perf_counter() - ts, seq, l))
        if proc_s:
            time.sleep(proc_s)
    rd.close()
    r = summarize(samples, dur)
    r.update(open_s=round(t_open, 3), first_frame_s=round(first, 3))
    return r


def run_threaded(url, dur, proc_s, backend):
    src = CameraSource(CameraConfig("A", url, backend=backend))
    src.start()
    t0 = time.perf_counter()
    while src.latest() is None and time.perf_counter() - t0 < 5:
        time.sleep(0.005)
    first = time.perf_counter() - t0
    samples = []
    ts = time.perf_counter()
    while time.perf_counter() - ts < dur:
        f = src.wait_next(1.0)
        if f is None:
            continue
        seq, l = lat_ms(f.image)
        samples.append((time.perf_counter() - ts, seq, l))
        if proc_s:
            time.sleep(proc_s)
    src.stop()
    r = summarize(samples, dur)
    r.update(first_frame_s=round(first, 3), dropped_overwrite=src.stats["dropped_overwrite"])
    return r


if __name__ == "__main__":
    url = sys.argv[1]
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else 15
    which = sys.argv[3].split(",") if len(sys.argv) > 3 else ["A", "B", "C", "D", "E"]
    procs = [float(x) for x in sys.argv[4].split(",")] if len(sys.argv) > 4 else [0.0, 0.050, 0.100]
    res = {}
    for P in procs:
        if "A" in which:
            res[f"A_ffmpeg_direct_P{int(P*1000)}"] = run_direct_ffmpeg(url, dur, P)
        if "E" in which:
            res[f"E_ffmpeg_direct_nobuffer_P{int(P*1000)}"] = run_direct_ffmpeg(
                url, dur, P, "fflags;nobuffer|flags;low_delay|probesize;32|analyzeduration;0")
        if "B" in which:
            res[f"B_mjpeg_direct_P{int(P*1000)}"] = run_direct_mjpeg(url, dur, P)
        if "C" in which:
            res[f"C_thread_mjpeg_P{int(P*1000)}"] = run_threaded(url, dur, P, "mjpeg")
        if "D" in which:
            res[f"D_thread_ffmpeg_P{int(P*1000)}"] = run_threaded(url, dur, P, "ffmpeg")
        for k, v in res.items():
            if k.endswith(f"_P{int(P*1000)}"):
                print(k, json.dumps(v), flush=True)
    out = os.path.join(os.path.dirname(__file__), f"res_latency_{os.environ.get('TAG','run')}.json")
    with open(out, "w") as fh:
        json.dump(res, fh, indent=1)

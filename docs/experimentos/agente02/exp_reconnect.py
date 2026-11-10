"""Experimento 2: corte / cuelgue / reconexión (localhost).

Escenarios (para backend 'mjpeg' y 'ffmpeg' de CameraSource):
  kill  : se mata el proceso servidor (RST/conn refused) y se relanza a los 5 s
  drop  : el servidor cierra el socket (FIN) y sigue vivo
  stall : el servidor deja de enviar 8 s sin cerrar el socket (Wi-Fi colgado)
Además: cv2.VideoCapture "crudo" sin timeouts ante stall (cuánto bloquea read()).

Métricas: detección = desde el evento hasta que el estado deja STREAMING;
recuperación = desde que el servidor vuelve a estar disponible hasta el 1er frame nuevo.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from capture_proto import CameraConfig, CameraSource, ConnState, ReconnectPolicy  # noqa: E402

PORT = 8082
URL = f"http://127.0.0.1:{PORT}/video"


def start_server():
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "mjpeg_server.py"), "--port", str(PORT)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    p.stdout.readline()  # "serving on"
    return p


def ctl(q):
    urllib.request.urlopen(f"http://127.0.0.1:{PORT}/ctl?{q}", timeout=2).read()


def wait_frames(src, n=10, timeout=10):
    t0 = time.monotonic()
    start = src.stats["frames"]
    while src.stats["frames"] - start < n and time.monotonic() - t0 < timeout:
        time.sleep(0.01)


def first_frame_after(src, t, timeout=30):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        f = src.latest()
        if f is not None and f.t_received > t:
            return f.t_received - t
        time.sleep(0.005)
    return None


def left_streaming_after(src, t):
    for te, ev in src.events:
        if te >= t and ev != "streaming":
            return round(te - t, 3), ev
    return None, None


def scenario(backend, kind, srv):
    pol = ReconnectPolicy(initial_s=0.25, max_s=2.0, factor=2.0, jitter=0.0)
    src = CameraSource(CameraConfig("A", URL, backend=backend, connect_timeout_s=1.0,
                                    read_timeout_s=2.0, reconnect=pol))
    src.start()
    wait_frames(src, 30)
    res = dict(backend=backend, kind=kind)
    t_ev = time.monotonic()
    if kind == "kill":
        srv.kill()
        srv.wait()
        time.sleep(5.0)
        t_back = time.monotonic()
        srv = start_server()
    elif kind == "drop":
        ctl("drop=1")
        t_back = time.monotonic()
    elif kind == "stall":
        ctl("stall=8")
        t_back = t_ev + 8.0
    if kind == "stall":
        # ignorar frames "en vuelo" (recibidos < 0.3 s después de pedir el stall)
        rec = first_frame_after(src, t_ev + 0.3, timeout=30)
        res["gap_until_next_frame_s"] = None if rec is None else round(rec + 0.3, 3)
        res["stall_s"] = 8.0
        f = src.latest()
        res["session_of_first_frame_after"] = f.session if f else None
    else:
        rec = first_frame_after(src, t_back, timeout=30)
        res["recovery_s"] = None if rec is None else round(rec, 3)
    d, ev = left_streaming_after(src, t_ev)
    res["detect_s"], res["detect_state"] = d, ev
    res["reconnects"] = src.stats["reconnects"]
    res["sessions"] = src._session
    res["last_error"] = src.stats["last_error"][:80]
    res["events"] = [(round(te - t_ev, 3), e) for te, e in src.events if te >= t_ev - 0.01][:12]
    wait_frames(src, 10)
    src.stop()
    return res, srv


def raw_ffmpeg_stall(srv, stall_s=40, with_timeout_ms=None):
    params = []
    if with_timeout_ms:
        params = [cv2.CAP_PROP_READ_TIMEOUT_MSEC, with_timeout_ms]
    cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG, params)
    for _ in range(10):
        cap.read()
    ctl(f"stall={stall_s}")
    t0 = time.monotonic()
    reads = []
    while time.monotonic() - t0 < stall_s + 5:
        ts = time.monotonic()
        ok, _ = cap.read()
        reads.append((round(ts - t0, 3), round(time.monotonic() - ts, 3), ok))
        if not ok:
            break
    cap.release()
    n_ok = sum(1 for r in reads if r[2])
    fail = [r for r in reads if not r[2]]
    time.sleep(max(0, stall_s - (time.monotonic() - t0)) + 0.5)
    return dict(read_timeout_ms=with_timeout_ms, stall_s=stall_s, buffered_ok_reads=n_ok,
                failing_read=fail[0] if fail else None,
                longest_block_s=max(r[1] for r in reads))


def raw_ffmpeg_kill(srv):
    cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG)
    for _ in range(10):
        cap.read()
    srv.kill()
    srv.wait()
    t0 = time.monotonic()
    ok, _ = cap.read()
    t1 = time.monotonic() - t0
    srv = start_server()
    time.sleep(0.5)
    oks = [cap.read()[0] for _ in range(5)]
    cap.release()
    return dict(read_after_kill_ok=ok, read_returned_after_s=round(t1, 3),
                same_object_reads_after_restart=oks), srv


if __name__ == "__main__":
    srv = start_server()
    out = {}
    for backend in ("mjpeg", "ffmpeg"):
        for kind in ("drop", "kill", "stall"):
            r, srv = scenario(backend, kind, srv)
            print(json.dumps(r), flush=True)
            out[f"{backend}_{kind}"] = r
            time.sleep(1)
    r, srv = raw_ffmpeg_kill(srv)
    print("raw_kill", json.dumps(r), flush=True)
    out["raw_ffmpeg_kill"] = r
    r = raw_ffmpeg_stall(srv, stall_s=6, with_timeout_ms=2000)
    print("raw_stall_timeout2s", json.dumps(r), flush=True)
    out["raw_ffmpeg_stall_timeout2000"] = r
    r = raw_ffmpeg_stall(srv, stall_s=40, with_timeout_ms=None)
    print("raw_stall_default", json.dumps(r), flush=True)
    out["raw_ffmpeg_stall_default"] = r
    srv.kill()
    with open(os.path.join(HERE, "res_reconnect.json"), "w") as fh:
        json.dump(out, fh, indent=1)

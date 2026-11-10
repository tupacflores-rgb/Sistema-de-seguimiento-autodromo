"""Experimento 3: dos cámaras con latencias distintas, estimación de latencia con "LED"
y sincronización por timestamp más cercano.

Servidores: A (delay 30 ms + jitter U[0,10]), B (delay 120 ms + jitter U[0,30]).
Ambos dibujan un "LED" que se enciende cuando (t_wall_ms % 1000) < 100, es decir, el
mismo evento físico visto por las dos cámaras (como un LED del ESP32).

Fase 1 (calibración, 20 s): se detectan flancos de subida del LED en cada cámara y se
estima la latencia = t_rx(1er frame ON) - t_flanco - T/2 (corrección del muestreo).
Ground truth: t_rx - t_gen codificado en el frame.
Fase 2 (20 s): FrameSynchronizer con y sin offsets; se mide el desfase REAL de los pares
(|t_gen_A - t_gen_B|) usando los bits codificados.
"""
import json
import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from capture_proto import CameraConfig, CameraSource, FrameSynchronizer  # noqa: E402
from mjpeg_server import decode_frame_meta  # noqa: E402

FPS = 30
CAL_S = float(os.environ.get("CAL_S", 40))
LED_PERIOD, LED_ON = int(os.environ.get("LED_PERIOD", 1037)), 100


def start(port, delay, jitter):
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "mjpeg_server.py"), "--port", str(port),
                          "--fps", str(FPS), "--delay-ms", str(delay), "--jitter-ms", str(jitter),
                          "--led-period-ms", str(LED_PERIOD), "--led-on-ms", str(LED_ON)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    p.stdout.readline()
    return p


def wall_of_mono(t_mono, anchor):
    # convierte monotonic->wall con un ancla tomada al inicio (mismo PC)
    return t_mono + anchor


def collect(src, dur):
    """Recolecta TODOS los frames (usa wait_next; el consumidor aquí es rápido)."""
    out = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < dur:
        f = src.wait_next(0.5)
        if f is None:
            continue
        seq, tg, led = decode_frame_meta(f.image)
        out.append((f.t_received, f.t_wall, tg, bool(led)))
    return out


def estimate_latency(rows, anchor):
    lat_est, lat_true = [], []
    prev = None
    for t_rx, t_wall, tg, led in rows:
        now_ms = int(round(wall_of_mono(t_rx, anchor) * 1000))
        lat_true.append(((now_ms - tg) & 0xFFFFFFFF))
        if prev is not None and (not prev) and led:
            # flanco: el LED se encendió en el múltiplo de LED_PERIOD anterior al t_gen real,
            # pero el PC no conoce t_gen: usa su propio reloj -> flanco = múltiplo de 1000 ms
            # más cercano por debajo de (t_rx - lat_aprox). Como lat < 500 ms, tomamos el
            # múltiplo inmediatamente anterior a t_rx.
            edge_ms = (now_ms // LED_PERIOD) * LED_PERIOD
            if now_ms - edge_ms > LED_PERIOD / 2:
                edge_ms += 0  # latencia > 0.5 s: no esperado aquí
            lat_est.append(now_ms - edge_ms - 1000.0 / FPS / 2)
        prev = led
    return np.array(lat_est), np.array(lat_true)


def main():
    pa, pb = start(8091, 30, 10), start(8092, 120, 30)
    try:
        anchor = time.time() - time.monotonic()
        A = CameraSource(CameraConfig("A", "http://127.0.0.1:8091/video", ring_size=16))
        B = CameraSource(CameraConfig("B", "http://127.0.0.1:8092/video", ring_size=16))
        A.start(); B.start()
        time.sleep(1.0)
        import threading
        res = {}
        rows = {}

        def run(name, s):
            rows[name] = collect(s, CAL_S)
        th = [threading.Thread(target=run, args=(n, s)) for n, s in (("A", A), ("B", B))]
        [t.start() for t in th]; [t.join() for t in th]
        offs = {}
        for n in ("A", "B"):
            est, true = estimate_latency(rows[n], anchor)
            offs[n] = float(np.median(est)) / 1000.0
            res[f"lat_{n}"] = dict(n_edges=len(est), est_median_ms=round(float(np.median(est)), 1),
                                   est_iqr_ms=round(float(np.subtract(*np.percentile(est, [75, 25]))), 1),
                                   true_median_ms=round(float(np.median(true)), 1),
                                   true_p5_p95_ms=[round(float(x), 1) for x in np.percentile(true, [5, 95])],
                                   fps=round(len(rows[n]) / CAL_S, 1))
        res["rel_offset_est_ms"] = round((offs["B"] - offs["A"]) * 1000, 1)

        def sync_trial(use_offsets, dur=15, tol=0.020):
            A.cfg.latency_offset_s = offs["A"] if use_offsets else 0.0
            B.cfg.latency_offset_s = offs["B"] if use_offsets else 0.0
            time.sleep(1.0)  # que el anillo se renueve con los nuevos offsets
            sy = FrameSynchronizer({"A": A, "B": B}, tolerance_s=tol, max_age_s=0.5)
            skews_true, complete, total, used = [], 0, 0, set()
            t0 = time.monotonic()
            while time.monotonic() - t0 < dur:
                fs = sy.get()
                total += 1
                fa, fb = fs.frames["A"], fs.frames["B"]
                if fa is not None and fb is not None:
                    key = (fa.seq, fb.seq)
                    if key not in used:
                        used.add(key)
                        complete += 1
                        ga = decode_frame_meta(fa.image)[1]
                        gb = decode_frame_meta(fb.image)[1]
                        skews_true.append(abs(((ga - gb + 2**31) & 0xFFFFFFFF) - 2**31))
                time.sleep(1 / 30)
            a = np.array(skews_true, float)
            return dict(offsets_ms=dict(A=round(A.cfg.latency_offset_s * 1000, 1),
                                        B=round(B.cfg.latency_offset_s * 1000, 1)),
                        tolerance_ms=tol * 1000, polls=total, unique_pairs=complete,
                        true_skew_med_ms=round(float(np.median(a)), 1) if len(a) else None,
                        true_skew_p95_ms=round(float(np.percentile(a, 95)), 1) if len(a) else None,
                        true_skew_max_ms=round(float(a.max()), 1) if len(a) else None)
        res["sync_without_offsets"] = sync_trial(False)
        res["sync_without_offsets_tol60"] = sync_trial(False, tol=0.060)
        res["sync_with_offsets"] = sync_trial(True)
        res["sync_with_offsets_tol60"] = sync_trial(True, tol=0.060)
        # sin sincronizador: tomar "el último de cada uno"
        A.cfg.latency_offset_s = B.cfg.latency_offset_s = 0
        sk = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < 10:
            fa, fb = A.latest(), B.latest()
            sk.append(abs(decode_frame_meta(fa.image)[1] - decode_frame_meta(fb.image)[1]))
            time.sleep(1 / 30)
        res["naive_latest_pair"] = dict(true_skew_med_ms=float(np.median(sk)),
                                        true_skew_p95_ms=float(np.percentile(sk, 95)))
        A.stop(); B.stop()
        print(json.dumps(res, indent=1))
        with open(os.path.join(HERE, "res_sync.json"), "w") as fh:
            json.dump(res, fh, indent=1)
    finally:
        pa.kill(); pb.kill()


if __name__ == "__main__":
    main()

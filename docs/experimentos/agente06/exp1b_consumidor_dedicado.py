"""Experimento 1b: igual que escenario B, pero los eventos RFID se consumen en un hilo dedicado
(bloqueante en queue.get) en vez de drenarse una vez por frame en el bucle de video.
Mide latencia publicación->consumo y jitter del bucle de video en paralelo."""
import json
import queue
import sys
import threading
import time

import cv2

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from exp1_bus_jitter import (DUR, EventBus, producer, slow_rfid_read_sleep, summarize,  # noqa: E402
                             video_loop, pct)

if __name__ == "__main__":
    cv2.setNumThreads(1)
    bus = EventBus()
    stop = threading.Event()
    q_fusion = bus.subscribe("fusion")
    lat = []

    def consumer():
        while not stop.is_set():
            try:
                ev = q_fusion.get(timeout=0.1)
            except queue.Empty:
                continue
            lat.append(time.perf_counter() - ev["t_pub"])

    tc = threading.Thread(target=consumer, daemon=True)
    tp = threading.Thread(target=producer, args=(bus, stop, slow_rfid_read_sleep), daemon=True)
    tc.start(); tp.start()
    t0 = time.perf_counter()
    res = video_loop(DUR, None, None)
    el = time.perf_counter() - t0
    stop.set(); tp.join(1); tc.join(1)
    s = summarize("E_bus_consumidor_dedicado", res, el)
    s["eventos_rfid_consumidos"] = len(lat)
    s["lat_evento_p50_ms"] = round(pct(lat, 50) * 1000, 3)
    s["lat_evento_p99_ms"] = round(pct(lat, 99) * 1000, 3)
    s["lat_evento_max_ms"] = round(max(lat) * 1000, 3)
    s["bus_descartados"] = bus.dropped
    print(json.dumps(s, ensure_ascii=False))

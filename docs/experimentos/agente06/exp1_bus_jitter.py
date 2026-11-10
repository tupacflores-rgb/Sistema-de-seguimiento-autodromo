"""Experimento 1 (Agente 06): ¿el bucle de video a 30 Hz se demora por un productor RFID lento?

Escenarios:
  A) baseline: solo bucle de video (sin productor)
  B) bus desacoplado: productor RFID en hilo propio que BLOQUEA aleatoriamente (time.sleep, libera GIL,
     equivale a esperar I/O serial) y publica en EventBus (queue.Queue); el video drena con get_nowait.
  C) bus desacoplado + productor que hace trabajo CPU en Python puro (retiene el GIL, peor caso).
  D) anti-patrón: la lectura RFID bloqueante se hace DENTRO del bucle de video.

El bucle de video hace trabajo real por frame (cv2.GaussianBlur + absdiff sobre 960x720 gris) y se
programa con deadlines absolutos (period = 1/30 s). Se mide: período real entre frames, retraso respecto
al deadline (lateness), frames que exceden el presupuesto, latencia evento RFID (publicación -> consumo).
Uso: python3 exp1_bus_jitter.py [segundos_por_escenario]
"""
import json
import queue
import random
import statistics as st
import sys
import threading
import time

import cv2
import numpy as np

FPS = 30.0
PERIOD = 1.0 / FPS
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
random.seed(1234)


class EventBus:
    """Bus mínimo: una cola por suscriptor, publish nunca bloquea (descarta y cuenta si está llena)."""

    def __init__(self, maxsize=1024):
        self._subs = {}
        self._lock = threading.Lock()
        self.dropped = 0
        self.maxsize = maxsize

    def subscribe(self, name):
        q = queue.Queue(maxsize=self.maxsize)
        with self._lock:
            self._subs[name] = q
        return q

    def publish(self, event):
        with self._lock:
            subs = list(self._subs.values())
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                self.dropped += 1


def slow_rfid_read_sleep():
    """Simula lectura serial/RC522: 70% rápido (1-5 ms), 25% lento (20-80 ms), 5% muy lento (200-500 ms)."""
    r = random.random()
    if r < 0.70:
        d = random.uniform(0.001, 0.005)
    elif r < 0.95:
        d = random.uniform(0.020, 0.080)
    else:
        d = random.uniform(0.200, 0.500)
    time.sleep(d)
    return d


def slow_rfid_read_cpu():
    """Igual distribución pero quemando CPU en Python puro (retiene el GIL en tramos de 5 ms)."""
    r = random.random()
    if r < 0.70:
        d = random.uniform(0.001, 0.005)
    elif r < 0.95:
        d = random.uniform(0.020, 0.080)
    else:
        d = random.uniform(0.200, 0.500)
    t_end = time.perf_counter() + d
    x = 0
    while time.perf_counter() < t_end:
        x += 1  # bytecode puro: el intérprete cede el GIL cada sys.getswitchinterval() (5 ms)
    return d


def producer(bus, stop, read_fn):
    seq = 0
    while not stop.is_set():
        read_fn()
        seq += 1
        # simula parseo de línea NDJSON real
        line = json.dumps({"v": 1, "type": "rfid_read", "seq": seq, "t_esp": seq * 10,
                           "reader": seq % 3 + 1, "uid": "04A1B2C3D4E5F6"})
        bus.publish({"msg": json.loads(line), "t_pub": time.perf_counter()})


def video_loop(duration, bus_q=None, inline_read=None):
    rng = np.random.default_rng(0)
    frames = [rng.integers(0, 255, (720, 960), dtype=np.uint8) for _ in range(4)]
    prev = cv2.GaussianBlur(frames[0], (5, 5), 0)
    periods, lateness, work, ev_lat = [], [], [], []
    n_events = 0
    t0 = time.perf_counter()
    next_deadline = t0 + PERIOD
    last = t0
    i = 0
    while True:
        now = time.perf_counter()
        if now - t0 > duration:
            break
        # --- "procesamiento de frame" ---
        w0 = time.perf_counter()
        f = cv2.GaussianBlur(frames[i % 4], (5, 5), 0)
        d = cv2.absdiff(f, prev)
        _ = cv2.threshold(d, 25, 255, cv2.THRESH_BINARY)
        prev = f
        if inline_read is not None:
            inline_read()  # ANTI-PATRÓN
            n_events += 1
        if bus_q is not None:
            while True:
                try:
                    ev = bus_q.get_nowait()
                except queue.Empty:
                    break
                ev_lat.append(time.perf_counter() - ev["t_pub"])
                n_events += 1
        work.append(time.perf_counter() - w0)
        # --- espera hasta el siguiente deadline absoluto ---
        sl = next_deadline - time.perf_counter()
        if sl > 0:
            time.sleep(sl)
        t = time.perf_counter()
        lateness.append(t - next_deadline)
        periods.append(t - last)
        last = t
        next_deadline += PERIOD
        if t - next_deadline > PERIOD:  # si se atrasó más de un frame, re-sincroniza (descarta)
            next_deadline = t + PERIOD
        i += 1
    return periods, lateness, work, ev_lat, n_events, i


def pct(a, p):
    a = sorted(a)
    if not a:
        return float("nan")
    k = min(len(a) - 1, int(round(p / 100.0 * (len(a) - 1))))
    return a[k]


def summarize(name, res, elapsed):
    periods, lateness, work, ev_lat, n_events, n = res
    ms = lambda x: round(x * 1000, 3)
    over = sum(1 for p in periods if p > PERIOD * 1.5)
    out = {
        "escenario": name,
        "frames": n,
        "fps_efectivo": round(n / elapsed, 2),
        "periodo_media_ms": ms(st.mean(periods)),
        "periodo_std_ms": ms(st.pstdev(periods)),
        "periodo_p99_ms": ms(pct(periods, 99)),
        "periodo_max_ms": ms(max(periods)),
        "lateness_p50_ms": ms(pct(lateness, 50)),
        "lateness_p99_ms": ms(pct(lateness, 99)),
        "lateness_max_ms": ms(max(lateness)),
        "trabajo_frame_p50_ms": ms(pct(work, 50)),
        "frames_>1.5xperiodo": over,
        "eventos_rfid_consumidos": n_events,
    }
    if ev_lat:
        out.update({
            "lat_evento_p50_ms": ms(pct(ev_lat, 50)),
            "lat_evento_p99_ms": ms(pct(ev_lat, 99)),
            "lat_evento_max_ms": ms(max(ev_lat)),
        })
    return out


def run(name, read_fn=None, decoupled=True):
    bus = EventBus()
    stop = threading.Event()
    th = None
    q = None
    inline = None
    if read_fn is not None and decoupled:
        q = bus.subscribe("video")
        th = threading.Thread(target=producer, args=(bus, stop, read_fn), daemon=True)
        th.start()
    elif read_fn is not None:
        inline = read_fn
    t0 = time.perf_counter()
    res = video_loop(DUR, q, inline)
    el = time.perf_counter() - t0
    stop.set()
    if th:
        th.join(timeout=1)
    s = summarize(name, res, el)
    s["bus_descartados"] = bus.dropped
    return s


if __name__ == "__main__":
    cv2.setNumThreads(1)
    print(f"# python {sys.version.split()[0]} opencv {cv2.__version__} numpy {np.__version__} "
          f"switchinterval={sys.getswitchinterval()} dur={DUR}s")
    results = [
        run("A_baseline_sin_rfid"),
        run("B_bus_productor_bloqueante_IO", slow_rfid_read_sleep, True),
        run("C_bus_productor_CPU_GIL", slow_rfid_read_cpu, True),
        run("D_antipatron_lectura_inline", slow_rfid_read_sleep, False),
    ]
    for r in results:
        print(json.dumps(r, ensure_ascii=False))

"""Experimento 2 (Agente 06): simulador de ESP32 sobre pseudo-terminal (pty) + parser NDJSON + sincronización
de reloj ping/pong (estilo NTP simplificado, filtro de mínimo RTT).

- No usa pyserial (no instalado): os.openpty() da un par maestro/esclavo que se comporta como un puerto serial
  en Linux (en la PC real se reemplaza por serial.Serial o por com0com en Windows).
- El "ESP32" simulado tiene un reloj millis() con offset desconocido y deriva (+40 ppm) y responde pings
  con retardo asimétrico aleatorio. Se verifica que el estimador recupera el offset con error medido.
- Mide: throughput del parser (líneas/s), latencia escritura->parseo por el pty, error de sincronización.
"""
import json
import os
import random
import statistics as st
import threading
import time

random.seed(7)
OFFSET_S = 12345.678          # el ESP32 arrancó "en otro momento": t_esp = (t_pc - T0)*(1+drift) + ...
DRIFT = 40e-6                  # 40 ppm, típico de cristal + RC sin calibrar (supuesto de simulación)
T0 = time.monotonic() - OFFSET_S


def esp_millis():
    return int(((time.monotonic() - T0) * (1 + DRIFT)) * 1000)


def esp_sim(fd_slave, stop, n_rfid):
    """Firmware simulado: emite rfid_read y heartbeat; responde ping con pong {t1 eco, t2, t3}."""
    seq = 0
    buf = b""
    sent = 0
    os.set_blocking(fd_slave, False)
    last_hb = 0
    while not stop.is_set():
        # leer comandos de la PC
        try:
            chunk = os.read(fd_slave, 4096)
            buf += chunk
        except BlockingIOError:
            pass
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            msg = json.loads(line)
            if msg["type"] == "ping":
                time.sleep(random.uniform(0.0002, 0.003))   # retardo de "ida" asimétrico (simulado)
                t2 = esp_millis()
                time.sleep(random.uniform(0.0, 0.0005))     # proceso en el ESP32
                t3 = esp_millis()
                seq += 1
                out = {"v": 1, "type": "pong", "seq": seq, "t_esp": t3, "ping_seq": msg["seq"],
                       "t1": msg["t_pc"], "t2": t2, "t3": t3}
                os.write(fd_slave, (json.dumps(out, separators=(",", ":")) + "\n").encode())
        if sent < n_rfid:
            seq += 1
            out = {"v": 1, "type": "rfid_read", "seq": seq, "t_esp": esp_millis(),
                   "reader": sent % 3 + 1, "uid": "04A1B2C3D4E5%02X" % (sent % 5),
                   "_t_write_pc": time.perf_counter()}  # campo SOLO de instrumentación del experimento
            os.write(fd_slave, (json.dumps(out, separators=(",", ":")) + "\n").encode())
            sent += 1
        now = time.monotonic()
        if now - last_hb > 1.0:
            seq += 1
            os.write(fd_slave, (json.dumps({"v": 1, "type": "heartbeat", "seq": seq, "t_esp": esp_millis(),
                                            "readers_ok": [1, 1, 1]}) + "\n").encode())
            last_hb = now
        time.sleep(0.0005)


class ClockSync:
    """Offset ESP32->PC estimado con las muestras de menor RTT (las menos contaminadas por colas)."""

    def __init__(self, keep=32):
        self.samples = []  # (rtt_s, offset_s)
        self.keep = keep

    def add(self, t1_pc, t2_esp_ms, t3_esp_ms, t4_pc):
        t2, t3 = t2_esp_ms / 1000.0, t3_esp_ms / 1000.0
        rtt = (t4_pc - t1_pc) - (t3 - t2)
        offset = ((t2 - t1_pc) + (t3 - t4_pc)) / 2.0   # t_esp - t_pc
        self.samples.append((rtt, offset))
        self.samples = self.samples[-self.keep:]

    def offset(self):
        best = sorted(self.samples)[: max(1, len(self.samples) // 4)]
        return st.median(o for _, o in best)

    def esp_to_pc(self, t_esp_ms):
        return t_esp_ms / 1000.0 - self.offset()


def main():
    fd_master, fd_slave = os.openpty()
    import tty
    tty.setraw(fd_slave)  # sin eco ni traducción \n->\r\n, como un puerto serial crudo
    os.set_blocking(fd_master, False)
    stop = threading.Event()
    N = 5000
    th = threading.Thread(target=esp_sim, args=(fd_slave, stop, N), daemon=True)
    th.start()

    sync = ClockSync()
    pending_ping = {}
    buf = b""
    counts = {}
    lat_pty = []
    seqs = []
    bad = 0
    ping_seq = 0
    t_start = time.perf_counter()
    last_ping = 0
    pings_sent = 0
    sync_err_ms = []
    while True:
        now = time.monotonic()
        if now - last_ping > 0.05 and pings_sent < 64:
            ping_seq += 1
            t1 = time.monotonic()
            os.write(fd_master, (json.dumps({"v": 1, "type": "ping", "seq": ping_seq, "t_pc": t1}) + "\n").encode())
            pending_ping[ping_seq] = t1
            pings_sent += 1
            last_ping = now
        try:
            chunk = os.read(fd_master, 65536)
        except BlockingIOError:
            chunk = b""
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip(b"\r")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            t = msg.get("type")
            counts[t] = counts.get(t, 0) + 1
            seqs.append(msg["seq"])
            if t == "rfid_read":
                lat_pty.append(time.perf_counter() - msg["_t_write_pc"])
            elif t == "pong":
                t4 = time.monotonic()
                sync.add(msg["t1"], msg["t2"], msg["t3"], t4)
                # verdad de terreno: offset real = t_esp - t_pc  (conocido sólo en simulación)
                true_off = esp_millis() / 1000.0 - time.monotonic()
                sync_err_ms.append(abs(sync.offset() - true_off) * 1000)
        if counts.get("rfid_read", 0) >= N and pings_sent >= 64 and counts.get("pong", 0) >= 64:
            break
        if time.perf_counter() - t_start > 60:
            break
        os.set_blocking(fd_master, False)
        time.sleep(0.0002)
    el = time.perf_counter() - t_start
    stop.set()
    gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if b != a + 1)
    lat_pty.sort()
    res = {
        "lineas_por_tipo": counts,
        "lineas_invalidas": bad,
        "huecos_seq": gaps,
        "duracion_s": round(el, 3),
        "rfid_lineas_por_s": round(counts.get("rfid_read", 0) / el, 1),
        "lat_pty_write_parse_p50_ms": round(lat_pty[len(lat_pty) // 2] * 1000, 3),
        "lat_pty_write_parse_p99_ms": round(lat_pty[int(len(lat_pty) * 0.99)] * 1000, 3),
        "lat_pty_write_parse_max_ms": round(lat_pty[-1] * 1000, 3),
        "sync_err_final_ms": round(sync_err_ms[-1], 3),
        "sync_err_max_ultimos10_ms": round(max(sync_err_ms[-10:]), 3),
        "rtt_min_ms": round(min(r for r, _ in sync.samples) * 1000, 3),
        "rtt_max_ms": round(max(r for r, _ in sync.samples) * 1000, 3),
        "nota": "resolución millis() = 1 ms; el error de sync incluye esa cuantización",
    }
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()

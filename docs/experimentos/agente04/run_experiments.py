"""Experimento: 3 autos, 2 camaras con solape, ruido, perdidas, desfase de timestamps.

Uso: python run_experiments.py [n_seeds] [T]
Salida: tabla por configuracion y p_miss, y results.json.
"""
from __future__ import annotations
import heapq
import json
import sys
import time
import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.signal import savgol_filter

import sim
from tracker_proto import (Tracker, TrackerConfig, WorldDetection, TrackState,
                           fuse_frame_pair, ClockOffsetEstimator)
from progress import CenterlinePolyline, ProgressTracker

FPS = 30.0
BUF = 0.10        # buffer de reordenamiento (s): la salida se emite con 100 ms de latencia
MATCH_MM = 200.0

CONFIGS = {
    # nombre: (modo, timestamps, modelo, aruco)
    "C0_porcamara_union":        ("percam", "exact", "cv", True),
    "C1_global_CV_sinID":        ("global", "exact", "cv", False),
    "C2_global_CV_ArUco":        ("global", "exact", "cv", True),
    "C3_global_CA_ArUco":        ("global", "exact", "ca", True),
    "C4_global_IMM_ArUco":       ("global", "exact", "imm", True),
    "C5_parframes_CV_ArUco":     ("pair", "exact", "cv", True),
    "C5n_parframes_sinCompens":  ("pair", "raw", "cv", True),
    "C6_global_sinCompens":      ("global", "raw", "cv", True),
    "C7_global_deltaEstimado":   ("global", "est", "cv", True),
    "C8_global_deltaCalibrado":  ("global", "batch", "cv", True),
}


def make_centerline():
    ss = np.arange(0, sim.L, 10.0)
    return CenterlinePolyline(np.array([sim.centerline(s)[0] for s in ss]))


LINE = make_centerline()


def estimate_offset_batch(frames, t_max=20.0, ref="A", other="B"):
    """Calibracion de desfase (lote, primeros t_max s) usando SOLO datos observables:
    detecciones con el mismo ID ArUco en ambas camaras dentro del solape.
    Para cada deteccion de B con marcador k: recta por las detecciones de A con marcador k
    en +-150 ms (t reportado), residuo r = z_B - p_A(t_B), delta_i = -(r.v)/|v|^2; delta = mediana."""
    A = [(f.t_reported, d.xy, d.aruco) for f in frames if f.cam == ref and f.t_reported < t_max
         for d in f.dets if d.aruco is not None]
    ests = []
    for f in frames:
        if f.cam != other or f.t_reported > t_max:
            continue
        for d in f.dets:
            if d.aruco is None:
                continue
            pts = [(t, xy) for t, xy, k in A if k == d.aruco and abs(t - f.t_reported) < 0.15]
            if len(pts) < 4:
                continue
            ts = np.array([p[0] for p in pts]) - f.t_reported; xs = np.array([p[1] for p in pts])
            Am = np.vstack([ts, np.ones_like(ts)]).T
            coef, *_ = np.linalg.lstsq(Am, xs, rcond=None)
            v, p = coef[0], coef[1]
            sp = np.hypot(*v)
            if sp < 500 or np.hypot(*(d.xy - p)) > 300:
                continue
            ests.append(-float((d.xy - p) @ v) / sp ** 2)
    return float(np.median(ests)) if len(ests) >= 10 else 0.0


def to_wd(f, t_used):
    return [WorldDetection(d.xy, np.eye(2) * d.sigma ** 2, t_used, f.cam, d.aruco) for d in f.dets]


def run_one(cfg_name, seed, p_miss, T=60.0, overrides=None, sim_kw=None, lanes=None):
    mode, tsm, model, aruco = CONFIGS[cfg_name]
    rng = np.random.default_rng(seed)
    gt = sim.simulate_cars(T + 1.0, rng, lanes=lanes or sim.LANES)
    delta = float(rng.uniform(0.03, 0.08))
    frames = sim.simulate_cameras(gt, rng, T, p_miss, delta, **(sim_kw or {}))
    tcfg = TrackerConfig(model=model, use_aruco=aruco, **(overrides or {}))
    est = ClockOffsetEstimator() if tsm == "est" else None
    delta_batch = estimate_offset_batch(frames, t_max=20.0) if tsm == "batch" else None
    upd_times = []

    def t_used_of(f):
        if tsm == "exact":
            return f.t_true
        if tsm == "raw":
            return f.t_reported
        if tsm == "batch":
            return f.t_reported - (delta_batch if f.cam == "B" else 0.0)
        return f.t_reported - (est.delta if f.cam == "B" else 0.0)

    # ---------- construir cola de eventos
    if mode == "pair":
        A = [f for f in frames if f.cam == "A"]; B = [f for f in frames if f.cam == "B"]
        tb = np.array([t_used_of(f) for f in B]); usedb = set()
        events = []
        for fa in A:
            ta = t_used_of(fa)
            j = int(np.argmin(np.abs(tb - ta)))
            if abs(tb[j] - ta) < 0.5 / FPS and j not in usedb:
                usedb.add(j)
                tm = 0.5 * (ta + tb[j])
                events.append((tm, "AB", fuse_frame_pair(to_wd(fa, tm), to_wd(B[j], tm)), fa.t_reported))
            else:
                events.append((ta, "A", to_wd(fa, ta), fa.t_reported))
        for j, fb in enumerate(B):
            if j not in usedb:
                events.append((tb[j], "B", to_wd(fb, tb[j]), fb.t_reported))
        pending = sorted(events, key=lambda e: e[3])
    else:
        pending = sorted([(None, f.cam, f, f.t_reported) for f in frames], key=lambda e: e[3])

    trackers = {"A": Tracker(tcfg), "B": Tracker(tcfg)} if mode == "percam" else {"G": Tracker(tcfg)}
    heap, k_in, seq = [], 0, 0
    grid = np.arange(1.0, T - 0.3, 1 / FPS)

    M = dict(frames=0, gt_obj=0, matched=0, missed=0, fp=0, dup=0, dup_overlap=0, overlap_obj=0,
             idsw_track=0, idsw_public=0, label_ok=0, label_wrong=0, label_none=0, sq=0.0,
             rank_ok=0, rank_n=0, rank_ok_sep=0, rank_n_sep=0, sq_c=0.0, n_c=0, sq_l=0.0, n_l=0, dup_lost=0, err_list=[], idsw_car=0)
    last_tid = {c: None for c in range(3)}; last_pub = {c: None for c in range(3)}
    series = {c: [] for c in range(3)}   # (t, xy_est, v_est, speed_gt) por auto real
    prog = ProgressTracker(LINE)
    delta_trace = []

    for tau in grid:
        # llegan al buffer los frames cuyo t_reportado <= tau + BUF
        while k_in < len(pending) and pending[k_in][3] <= tau + BUF:
            e = pending[k_in]; k_in += 1
            if mode == "pair":
                heapq.heappush(heap, (e[0], seq, e[1], e[2])); seq += 1
            else:
                f = e[2]; tu = t_used_of(f)
                heapq.heappush(heap, (tu, seq, f.cam, to_wd(f, tu))); seq += 1
        while heap and heap[0][0] <= tau:
            tu, _, cam, dets = heapq.heappop(heap)
            trk = trackers[cam] if mode == "percam" else trackers["G"]
            t0 = time.perf_counter()
            trk.update(dets, tu)
            upd_times.append(time.perf_counter() - t0)
            if est is not None and cam == "B":
                for j, tr in trk.last_matches:
                    if tr.state == TrackState.CONFIRMED:
                        est.observe(tr, dets[j].xy, tu)
        if est is not None:
            delta_trace.append(est.delta)
        # ---------- evaluacion en tau
        outs = []
        for name, trk in trackers.items():
            for o in trk.outputs_at(tau):
                o["tid"] = (name, o["track_id"])
                o["pub"] = ("car", o["car_id"]) if o["car_id"] is not None else o["tid"]
                outs.append(o)
        gpos, gvel, gprog = gt.at(tau)
        M["frames"] += 1; M["gt_obj"] += 3
        if outs:
            D = np.array([[np.hypot(*(o["xy"] - gpos[c])) for o in outs] for c in range(3)])
            ri, ci = linear_sum_assignment(D)
        else:
            D = np.zeros((3, 0)); ri, ci = [], []
        mt = {}
        for c, j in zip(ri, ci):
            if D[c, j] < MATCH_MM:
                mt[c] = j
        used = set(mt.values())
        for c in range(3):
            inov = sim.OVERLAP[0] <= gpos[c][0] <= sim.OVERLAP[1]
            M["overlap_obj"] += inov
            if c not in mt:
                M["missed"] += 1
                continue
            o = outs[mt[c]]
            M["matched"] += 1
            M["sq"] += D[c, mt[c]] ** 2
            M["err_list"].append(float(D[c, mt[c]]))
            if o["state"] == TrackState.CONFIRMED:
                M["sq_c"] += D[c, mt[c]] ** 2; M["n_c"] += 1
            else:
                M["sq_l"] += D[c, mt[c]] ** 2; M["n_l"] += 1
            if last_tid[c] is not None and o["tid"] != last_tid[c]:
                M["idsw_track"] += 1
            last_tid[c] = o["tid"]
            if last_pub[c] is not None and o["pub"] != last_pub[c]:
                M["idsw_public"] += 1
                if o["pub"][0] == "car" and last_pub[c][0] == "car":
                    M["idsw_car"] += 1
            last_pub[c] = o["pub"]
            if o["car_id"] is None:
                M["label_none"] += 1
            elif o["car_id"] == c + 1:
                M["label_ok"] += 1
            else:
                M["label_wrong"] += 1
            series[c].append((tau, o["xy"].copy(), o["v"].copy(), float(np.hypot(*gvel[c]))))
        for j, o in enumerate(outs):
            if j in used:
                continue
            dmin = D[:, j].min() if D.size else 1e9
            if dmin < 250:
                M["dup"] += 1
                M["dup_lost"] += o["state"] == TrackState.LOST
                cn = int(np.argmin(D[:, j]))
                if sim.OVERLAP[0] - 100 <= gpos[cn][0] <= sim.OVERLAP[1] + 100:
                    M["dup_overlap"] += 1
            else:
                M["fp"] += 1
        # ---------- progreso / ranking (solo identidades ArUco)
        if aruco:
            for o in outs:
                if o["car_id"] is not None:
                    prog.update(o["car_id"], o["xy"], tau)
            if len(prog.prog) == 3:
                rk = prog.ranking()
                rgt = [int(c) + 1 for c in np.argsort(-gprog)]
                ok = rk == rgt
                M["rank_n"] += 1; M["rank_ok"] += ok
                sp = np.sort(gprog)
                if np.min(np.diff(sp)) > 100:
                    M["rank_n_sep"] += 1; M["rank_ok_sep"] += ok

    # ---------- velocidades: Kalman vs Savitzky-Golay (sobre posiciones filtradas) vs diferencia central
    ev = dict(kf=[], sg=[], fd=[])
    for c in range(3):
        s = series[c]
        if len(s) < 15:
            continue
        ts = np.array([p[0] for p in s]); xy = np.array([p[1] for p in s])
        vk = np.array([p[2] for p in s]); sg = np.array([p[3] for p in s])
        brk = np.where(np.diff(ts) > 1.5 / FPS)[0] + 1
        for seg in np.split(np.arange(len(ts)), brk):
            if len(seg) < 15:
                continue
            ev["kf"] += list(np.hypot(*vk[seg].T) - sg[seg])
            vs = savgol_filter(xy[seg], 11, 2, deriv=1, delta=1 / FPS, axis=0)
            ev["sg"] += list(np.hypot(*vs.T) - sg[seg])
            vf = np.gradient(xy[seg], 1 / FPS, axis=0)
            ev["fd"] += list(np.hypot(*vf.T) - sg[seg])
    rms = lambda a: float(np.sqrt(np.mean(np.square(a)))) if len(a) else float("nan")

    # ---------- vueltas
    lap = dict(laps_ok=0, laps_n=0, cross_err=[], cross_missed=0, cross_extra=0)
    if aruco and len(prog.prog) == 3:
        gfin = gt.at(grid[-1])[2]
        for c in range(3):
            car = c + 1
            gl = int(np.floor(gfin[c] / sim.L))
            lap["laps_n"] += 1; lap["laps_ok"] += (prog.laps(car) == gl)
            # cruces reales
            gp = gt.prog[:, c]
            mask = (gt.t >= grid[0]) & (gt.t <= grid[-1])
            lp = np.floor(gp[mask] / sim.L)
            gcross = list(gt.t[mask][1:][np.diff(lp) > 0])
            vc = list(prog.crossings[car])
            for tc in gcross:
                if vc:
                    k = int(np.argmin(np.abs(np.array(vc) - tc)))
                    if abs(vc[k] - tc) < 1.0:
                        lap["cross_err"].append(vc[k] - tc); vc.pop(k); continue
                lap["cross_missed"] += 1
            lap["cross_extra"] += len(vc)

    return dict(cfg=cfg_name, seed=seed, p_miss=p_miss, delta=delta, M=M,
                rms_pos=float(np.sqrt(M["sq"] / max(M["matched"], 1))),
                v_rms=dict(kf=rms(ev["kf"]), sg=rms(ev["sg"]), fd=rms(ev["fd"])),
                lap=dict(laps_ok=lap["laps_ok"], laps_n=lap["laps_n"],
                         cross_abs_err_ms=float(np.mean(np.abs(lap["cross_err"])) * 1000) if lap["cross_err"] else None,
                         cross_max_err_ms=float(np.max(np.abs(lap["cross_err"])) * 1000) if lap["cross_err"] else None,
                         cross_missed=lap["cross_missed"], cross_extra=lap["cross_extra"],
                         cross_n=len(lap["cross_err"]) + lap["cross_missed"]),
                delta_hat=(delta_trace[-1] if delta_trace else delta_batch),
                upd_us=float(np.mean(upd_times) * 1e6), upd_p99_us=float(np.percentile(upd_times, 99) * 1e6),
                n_det_frames=len(frames))


def summarize(rows):
    out = {}
    for r in rows:
        key = (r["cfg"], r["p_miss"])
        out.setdefault(key, []).append(r)
    table = []
    for (cfg, pm), rs in sorted(out.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        S = lambda k: sum(r["M"][k] for r in rs)
        mins = sum(r["M"]["frames"] for r in rs) / FPS / 60
        row = dict(cfg=cfg, p_miss=pm, runs=len(rs), minutos=round(mins, 1),
                   idsw_track=S("idsw_track"), idsw_publico=S("idsw_public"), idsw_car=S("idsw_car"),
                   dup_pct=100 * S("dup") / S("gt_obj"), dup_solape_pct=100 * S("dup_overlap") / max(S("overlap_obj"), 1),
                   fp_frames=S("fp"), miss_pct=100 * S("missed") / S("gt_obj"),
                   rms_mm=float(np.sqrt(sum(r["M"]["sq"] for r in rs) / max(S("matched"), 1))),
                   rms_conf_mm=float(np.sqrt(S("sq_c") / max(S("n_c"), 1))),
                   rms_lost_mm=float(np.sqrt(S("sq_l") / max(S("n_l"), 1))),
                   lost_pct=100 * S("n_l") / max(S("matched"), 1),
                   p95_mm=float(np.percentile(sum((r["M"]["err_list"] for r in rs), []), 95)),
                   dup_lost=S("dup_lost"),
                   label_wrong_pct=100 * S("label_wrong") / max(S("matched"), 1),
                   label_none_pct=100 * S("label_none") / max(S("matched"), 1),
                   rank_ok_pct=(100 * S("rank_ok") / S("rank_n")) if S("rank_n") else None,
                   rank_ok_sep_pct=(100 * S("rank_ok_sep") / S("rank_n_sep")) if S("rank_n_sep") else None,
                   v_rms_kf=float(np.nanmean([r["v_rms"]["kf"] for r in rs])),
                   v_rms_sg=float(np.nanmean([r["v_rms"]["sg"] for r in rs])),
                   v_rms_fd=float(np.nanmean([r["v_rms"]["fd"] for r in rs])),
                   laps_ok=f'{sum(r["lap"]["laps_ok"] for r in rs)}/{sum(r["lap"]["laps_n"] for r in rs)}',
                   cross_err_ms=float(np.mean([r["lap"]["cross_abs_err_ms"] for r in rs if r["lap"]["cross_abs_err_ms"] is not None])) if any(r["lap"]["cross_abs_err_ms"] is not None for r in rs) else None,
                   cross_max_ms=max([r["lap"]["cross_max_err_ms"] for r in rs if r["lap"]["cross_max_err_ms"] is not None], default=None),
                   cross_missed=sum(r["lap"]["cross_missed"] for r in rs),
                   cross_extra=sum(r["lap"]["cross_extra"] for r in rs),
                   cross_n=sum(r["lap"]["cross_n"] for r in rs),
                   delta_err_ms=(float(np.mean([abs(r["delta_hat"] - r["delta"]) for r in rs])) * 1000) if rs[0]["delta_hat"] is not None else None,
                   upd_us=float(np.mean([r["upd_us"] for r in rs])),
                   upd_p99_us=float(np.max([r["upd_p99_us"] for r in rs])))
        table.append(row)
    return table


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    T = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    cfgs = sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3] != "all" else list(CONFIGS)
    rows = []
    lanes = (-100.0, 0.0, 100.0) if "--stress" in sys.argv else None
    out = "results_stress.json" if lanes else "results.json"
    for pm in (0.10, 0.20):
        for cfg in cfgs:
            for s in range(n):
                rows.append(run_one(cfg, 1000 + s, pm, T, lanes=lanes))
            print("done", cfg, pm, flush=True)
    tab = summarize(rows)
    with open(out, "w") as fh:
        json.dump(dict(rows=rows, table=tab), fh, indent=1, default=str)
    keys = ["cfg", "p_miss", "minutos", "idsw_track", "idsw_publico", "idsw_car", "dup_pct", "dup_solape_pct", "fp_frames",
            "miss_pct", "rms_mm", "rms_conf_mm", "rms_lost_mm", "lost_pct", "p95_mm", "dup_lost", "label_wrong_pct", "label_none_pct", "rank_ok_pct", "rank_ok_sep_pct",
            "v_rms_kf", "v_rms_sg", "v_rms_fd", "laps_ok", "cross_err_ms", "cross_max_ms", "cross_missed",
            "cross_extra", "cross_n", "delta_err_ms", "upd_us", "upd_p99_us"]
    for row in tab:
        print(" | ".join(f"{k}={row[k]:.2f}" if isinstance(row[k], float) else f"{k}={row[k]}" for k in keys))

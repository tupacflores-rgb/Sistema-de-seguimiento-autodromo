"""Pruebas del modo demo (simula autos y una carrera sin cámaras ni hardware).

    python -m pytest tests/test_demo_mode.py -v

A diferencia de tests/test_tracking.py y tests/test_admin.py, estas pruebas NO necesitan
OpenCV ni NumPy: el modo demo está pensado justamente para poder probar la sección Tracking
y la pantalla /display sin esas dependencias (ver admin/tracking.py: `_DemoWorker`).
"""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from admin.config_store import ConfigStore  # noqa: E402
from admin.tracking import Centerline, TrackingEngine, _synthetic_centerline_points  # noqa: E402


def make_engine(tmp_path) -> TrackingEngine:
    store = ConfigStore(tmp_path)
    return TrackingEngine(store, streams=None)  # el modo demo no usa `streams` (no hay cámaras)


# ------------------------------------------------------------ geometría auxiliar

def test_synthetic_track_is_closed_and_reasonably_sized():
    pts = _synthetic_centerline_points(1800.0, 1000.0, n=48)
    cl = Centerline(pts)
    assert cl.length > 0
    # una elipse de semiejes ~800x400 mm tiene un perímetro del orden de 3800-4000 mm
    assert 3000 < cl.length < 5000


def test_point_at_is_inverse_of_project():
    cl = Centerline(_synthetic_centerline_points(1800.0, 1000.0, n=48))
    for s in (0.0, cl.length * 0.25, cl.length * 0.5, cl.length * 0.9, cl.length * 1.3):
        x, y = cl.point_at(s)
        s2, d = cl.project(x, y)
        assert d < 1.0  # el punto generado cae (casi) exactamente sobre la línea
        assert min(abs(s2 - s % cl.length), cl.length - abs(s2 - s % cl.length)) < 1.0


# ------------------------------------------------------------ TrackingEngine.start_demo/stop

def test_start_demo_sin_pista_real_usa_pista_de_ejemplo(tmp_path):
    eng = make_engine(tmp_path)
    st = eng.start_demo()
    try:
        assert st["running"] is True and st["demo"] is True
        assert st["centerline_mm"] and st["centerline_mm"] > 0
        assert any("pista ovalada de ejemplo" in w for w in st["warnings"])
        assert len(st["vehicles"]) == 3  # default_vehicles() trae count=3
    finally:
        eng.stop()
    st2 = eng.state()
    assert st2["running"] is False and st2["demo"] is False


def test_start_demo_con_pista_real_la_reutiliza(tmp_path):
    store = ConfigStore(tmp_path)
    store.save_track({
        "width_mm": 2000, "height_mm": 1200, "grid_mm": 50,
        "plan": {"centerline": {"points": [[100, 100], [1900, 100], [1900, 1100], [100, 1100]],
                                 "closed": True},
                 "border_outer": {"points": [], "closed": True}},
    })
    eng = TrackingEngine(store, streams=None)
    st = eng.start_demo()
    try:
        perimeter = 2 * (1800 + 1000)  # rectángulo 1800x1000 mm
        assert st["centerline_mm"] == perimeter
        assert not any("pista ovalada de ejemplo" in w for w in st["warnings"])
    finally:
        eng.stop()


def test_demo_mueve_autos_y_cuenta_vueltas(tmp_path):
    store = ConfigStore(tmp_path)
    # pista chiquita (perímetro 400 mm) para que una vuelta tarde pocos segundos
    store.save_track({
        "width_mm": 100, "height_mm": 100, "grid_mm": 10,
        "plan": {"centerline": {"points": [[10, 10], [90, 10], [90, 90], [10, 90]],
                                 "closed": True},
                 "border_outer": {"points": [], "closed": True}},
    })
    veh = store.get_vehicles()
    veh["count"] = 2
    store.save_vehicles(veh)
    eng = TrackingEngine(store, streams=None)
    eng.start_demo()
    try:
        time.sleep(0.3)
        st = eng.state()
        moving = [v for v in st["vehicles"] if v["state"] != "buscando"]
        assert moving, "el modo demo debería ubicar a los autos casi de inmediato"
        for v in moving:
            assert v["speed_m_s"] >= 0
            assert abs(v["speed_scale_kmh"] - v["speed_m_s"] * 3.6 * 24) < 0.5  # redondeos aparte

        deadline = time.time() + 6.0
        lapped = False
        while time.time() < deadline and not lapped:
            time.sleep(0.2)
            st = eng.state()
            lapped = any(v["laps"] >= 1 for v in st["vehicles"])
        assert lapped, "con una pista de 400 mm, algún auto debería completar una vuelta en <6 s"
    finally:
        eng.stop()


def test_stop_corta_el_hilo_del_modo_demo(tmp_path):
    eng = make_engine(tmp_path)
    eng.start_demo()
    worker = eng._demo_worker
    assert worker is not None and worker.is_alive()
    eng.stop()
    assert not worker.is_alive()
    assert eng._demo_worker is None


def test_start_demo_es_idempotente(tmp_path):
    eng = make_engine(tmp_path)
    st1 = eng.start_demo()
    worker1 = eng._demo_worker
    st2 = eng.start_demo()  # ya está en modo demo: no debe reiniciar el hilo
    assert eng._demo_worker is worker1
    assert st1["demo"] is True and st2["demo"] is True
    eng.stop()


def test_modo_demo_y_tracking_real_son_excluyentes(tmp_path):
    eng = make_engine(tmp_path)  # cam1 por defecto está "enabled" con host -> start() no falla
    eng.start_demo()
    assert eng.demo is True
    try:
        eng.start()  # pasar a seguimiento real debe cortar el modo demo primero
        assert eng.demo is False
        assert eng.running is True
        assert eng._demo_worker is None
    finally:
        eng.stop()
    assert eng.running is False

    eng.start()
    try:
        assert eng.demo is False and eng.running is True
        eng.start_demo()  # pasar a demo debe cortar las cámaras reales primero
        assert eng.demo is True and eng.running is True
        assert eng._workers == {}
    finally:
        eng.stop()


def test_reload_no_toca_el_modo_demo(tmp_path):
    eng = make_engine(tmp_path)
    eng.start_demo()
    worker = eng._demo_worker
    eng.reload()  # a diferencia del seguimiento real, reload() no debe reiniciar el modo demo
    assert eng._demo_worker is worker
    assert eng.demo is True
    eng.stop()

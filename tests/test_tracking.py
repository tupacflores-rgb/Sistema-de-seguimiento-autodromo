"""Pruebas de calibración por puntos, vehículos y tracking por color + movimiento.

    python -m pytest tests/test_tracking.py -v

Usan una escena simulada: dos cámaras en perspectiva mirando la misma pista, autos con LED
de color y un objeto rojo quieto fuera de la pista. La posición real de cada auto se conoce en
todo momento, así que se mide el error real del sistema (en simulación).
"""
from __future__ import annotations

import math
import random
import socket
import statistics
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from admin import homography as hg  # noqa: E402
from admin.config_store import ValidationError, hue_distance_deg, validate_vehicles  # noqa: E402

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
from fake_ipwebcam import CAM3D_A, CAM3D_B, CAM_A_H, CAM_B_H, FakeCamState, Figure8Scene, PlanScene, serve  # noqa: E402
from admin.tracking import Centerline, KalmanCV, hue_ranges, sample_color  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def project(H, x, y):
    return cv2.perspectiveTransform(np.float64([[[x, y]]]), H)[0, 0].tolist()


# ------------------------------------------------------------------ homografía
def test_homography_matches_opencv():
    rng = random.Random(3)
    plan = [(rng.uniform(0, 1400), rng.uniform(0, 1200)) for _ in range(10)]
    img = [project(CAM_A_H, x, y) for x, y in plan]
    H = hg.fit(img, plan)
    assert max(hg.residuals(H, img, plan)) < 0.01
    Hi = hg.inverse(H)
    u, v = hg.apply(Hi, 700, 600)
    assert np.allclose([u, v], project(CAM_A_H, 700, 600), atol=1e-6)


def test_homography_rejects_collinear_and_few():
    with pytest.raises(hg.HomographyError):
        hg.fit([(0, 0), (1, 1), (2, 2), (3, 3)], [(0, 0), (1, 0), (2, 1), (5, 5)])
    with pytest.raises(hg.HomographyError):
        hg.fit([(0, 0), (1, 0), (0, 1)], [(0, 0), (1, 0), (0, 1)])


# ------------------------------------------------------------------ vehículos
def test_vehicles_validation_and_warnings():
    cfg, warn = validate_vehicles({"count": 2, "vehicles": [{"color": "#ff0000"}, {"color": "#ff2000"}]})
    assert cfg["count"] == 2 and any("parecidos" in w for w in warn)
    assert hue_distance_deg("#ff0000", "#0000ff") == pytest.approx(120, abs=2)
    with pytest.raises(ValidationError):
        validate_vehicles({"count": 5})
    with pytest.raises(ValidationError):
        validate_vehicles({"count": 1, "vehicles": [{"color": "rojo"}]})


def test_hue_ranges_wrap_red():
    assert hue_ranges(2, 10) == [(0, 12), (172, 179)]
    assert hue_ranges(175, 10) == [(165, 179), (0, 5)]


def test_sample_color_on_led():
    img = np.zeros((100, 100, 3), np.uint8)
    cv2.circle(img, (50, 50), 12, (0, 255, 0), -1)
    cv2.circle(img, (50, 50), 4, (230, 255, 230), -1)  # centro casi blanco
    jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()
    r = sample_color(jpeg, 50, 50, radius=10)
    assert abs(r["h"] - 60) <= 4 and r["only_colored_pixels"]


# ------------------------------------------------------------------ geometría y filtro
def test_centerline_progress():
    cl = Centerline([[0, 0], [1000, 0], [1000, 500], [0, 500]])
    assert cl.length == pytest.approx(3000)
    s, d = cl.project(500, 10)
    assert s == pytest.approx(500) and d == pytest.approx(10)


def test_kalman_estimates_velocity():
    kf = KalmanCV(0, 0, 0.0)
    for k in range(1, 40):
        t = k / 20
        kf.predict(t)
        kf.update(300 * t, 0, r_mm=5)
    assert kf.s[2] == pytest.approx(300, rel=0.05)


# ------------------------------------------------------------------ extremo a extremo
@pytest.fixture(scope="module")
def scene_app(tmp_path_factory):
    pytest.importorskip("flask")
    from admin.server import create_app
    root = tmp_path_factory.mktemp("e2e")
    sc = PlanScene()
    pa, pb = free_port(), free_port()
    sa, _ = serve(pa, FakeCamState("A", fps=20, scene=sc, H_plan_to_img=CAM_A_H))
    sb, _ = serve(pb, FakeCamState("B", fps=20, scene=sc, H_plan_to_img=CAM_B_H))
    app = create_app(root)
    c = app.test_client()
    c.put("/api/cameras/cam1", json={"enabled": True, "host": "127.0.0.1", "port": pa, "name": "A"})
    c.put("/api/cameras/cam2", json={"enabled": True, "host": "127.0.0.1", "port": pb, "name": "B"})
    for cid in ("cam1", "cam2"):
        assert c.post(f"/api/track/cameras/{cid}/snapshot").status_code == 200
    yield c, sc, app
    app.extensions["mcv_tracking"].stop()
    app.extensions["mcv_streams"].shutdown()
    sa.shutdown(); sb.shutdown()


def _ellipse(sc, d, n=40):
    return [[round(sc.CX + (sc.RX + d) * math.cos(2 * math.pi * k / n), 1),
             round(sc.CY + (sc.RY + d) * math.sin(2 * math.pi * k / n), 1)] for k in range(n)]


def test_calibration_from_reference_points(scene_app):
    c, sc, _ = scene_app
    rng = random.Random(0)
    t = c.get("/api/track").get_json()["track"]
    pts = []
    for name, (x, y) in sc.REF_POINTS.items():
        px = {}
        for cid, H in (("cam1", CAM_A_H), ("cam2", CAM_B_H)):
            u, v = project(H, x, y)
            if 5 <= u < 955 and 5 <= v < 715:
                px[cid] = [u + rng.gauss(0, 1), v + rng.gauss(0, 1)]  # clics con ~1 px de error
        pts.append({"name": name, "plan": [x, y], "px": px})
    t["calibration"] = {"points": pts}
    t["plan"]["centerline"] = {"points": _ellipse(sc, 0), "closed": True}
    t["plan"]["border_outer"] = {"points": _ellipse(sc, 170), "closed": True}
    t["plan"]["inner_islands"] = [{"name": "Isla 1", "points": _ellipse(sc, -170)}]
    r = c.put("/api/track", json=t)
    assert r.status_code == 200, r.get_json()
    homs = r.get_json()["track"]["calibration"]["homographies"]
    for cid in ("cam1", "cam2"):
        assert "H_img_to_plan" in homs[cid], homs[cid]
        assert homs[cid]["rms_mm"] < 6  # ruido de 1 px ≈ 2-3 mm en esta escena


def test_tracking_end_to_end(scene_app):
    c, sc, app = scene_app
    veh = c.get("/api/vehicles").get_json()["config"]
    veh["count"] = 3
    for v, col in zip(veh["vehicles"], ("#ff0000", "#00ff00", "#0000ff")):
        v["color"] = col
    assert c.put("/api/vehicles", json=veh).status_code == 200
    assert c.post("/api/tracking/start").status_code == 200
    time.sleep(3)
    errs = {1: [], 2: [], 3: []}
    end = time.time() + 8
    while time.time() < end:
        st = c.get("/api/tracking/state").get_json()
        now = time.monotonic()
        for v in st["vehicles"]:
            if v["state"] == "siguiendo":
                gx, gy = sc.car_pos(v["id"] - 1, now)
                errs[v["id"]].append(math.hypot(v["x"] - gx, v["y"] - gy))
        time.sleep(0.1)
    for vid, e in errs.items():
        assert len(e) > 40, f"auto {vid} casi no fue seguido"
        assert statistics.mean(e) < 40, f"auto {vid}: error medio {statistics.mean(e):.1f} mm"
    st = c.get("/api/tracking/state").get_json()
    assert all(cam["calibrated"] for cam in st["cameras"].values())
    assert sorted(v.get("position") for v in st["vehicles"]) == [1, 2, 3]
    # el objeto rojo quieto fuera de la pista nunca debe tomarse como el auto rojo
    red = next(v for v in st["vehicles"] if v["id"] == 1)
    assert red["x"] > 200


def test_tracking_stop_releases(scene_app):
    c, _, app = scene_app
    assert c.post("/api/tracking/stop").status_code == 200
    assert c.get("/api/tracking/state").get_json()["running"] is False
    r = c.get("/api/tracking/cameras/cam1/debug.mjpg")
    assert r.status_code == 409



# ------------------------------------------------------------------ pista en 8 y cámaras en ángulo
def test_legacy_inner_border_becomes_island():
    from admin.config_store import normalize_layers
    out = normalize_layers({"centerline": {"points": []}, "border_inner": {"points": [[1, 1], [5, 1], [5, 5]]}})
    assert "border_inner" not in out and out["inner_islands"][0]["points"] == [[1, 1], [5, 1], [5, 5]]


def test_camera_pose_from_homography_exact():
    for cam in (CAM3D_A, CAM3D_B):
        H_img_to_plan = np.linalg.inv(cam.H_plan_to_img).tolist()
        pose = hg.camera_pose(H_img_to_plan, 960, 720)
        assert pose["height_mm"] == pytest.approx(-cam.C[2], rel=0.01)
        assert pose["x_mm"] == pytest.approx(cam.C[0], abs=20)
        # un LED a 55 mm: la homografía lo ubica corrido; la corrección lo devuelve a su lugar
        (u, v), _ = cam.project(1000, 400, 55)
        gx, gy = hg.apply(H_img_to_plan, u, v)
        assert math.hypot(gx - 1000, gy - 400) > 30
        cx, cy = hg.correct_height(gx, gy, pose, 55)
        assert math.hypot(cx - 1000, cy - 400) < 1


def test_centerline_figure8_crossing_uses_hint():
    sc = Figure8Scene()
    cl = Centerline([list(p) for p in sc.centerline(128)])
    # justo en el cruce del 8 el punto pertenece a dos tramos: la pista (s anterior) decide
    s_a, _ = cl.project(sc.CX + 5, sc.CY + 3, s_hint=10.0)
    s_b, _ = cl.project(sc.CX + 5, sc.CY + 3, s_hint=cl.length / 2 + 10)
    assert min(abs(s_a), abs(s_a - cl.length)) < 60
    assert abs(s_b - cl.length / 2) < 60


@pytest.fixture(scope="module")
def eight_app(tmp_path_factory):
    pytest.importorskip("flask")
    from admin.server import create_app
    root = tmp_path_factory.mktemp("e8")
    sc = Figure8Scene()
    pa, pb = free_port(), free_port()
    sa, _ = serve(pa, FakeCamState("A", fps=20, scene=sc, cam3d=CAM3D_A))
    sb, _ = serve(pb, FakeCamState("B", fps=20, scene=sc, cam3d=CAM3D_B))
    app = create_app(root)
    c = app.test_client()
    c.put("/api/cameras/cam1", json={"enabled": True, "host": "127.0.0.1", "port": pa, "name": "A"})
    c.put("/api/cameras/cam2", json={"enabled": True, "host": "127.0.0.1", "port": pb, "name": "B"})
    for cid in ("cam1", "cam2"):
        assert c.post(f"/api/track/cameras/{cid}/snapshot").status_code == 200
    rng = random.Random(0)
    t = c.get("/api/track").get_json()["track"]
    t["calibration"] = {"points": [
        {"name": n, "plan": [x, y],
         "px": {cid: [float(v) + rng.gauss(0, 1) for v in cam.project(x, y)[0]] for cid, cam in (("cam1", CAM3D_A), ("cam2", CAM3D_B))}}
        for n, (x, y) in sc.REF_POINTS.items()]}
    t["plan"]["centerline"] = {"points": [list(p) for p in sc.centerline(64)], "closed": True}
    t["plan"]["border_outer"] = {"points": [list(p) for p in sc.outer()], "closed": True}
    t["plan"]["inner_islands"] = [{"name": f"Isla {i + 1}", "points": [list(p) for p in isl]} for i, isl in enumerate(sc.islands())]
    r = c.put("/api/track", json=t)
    assert r.status_code == 200, r.get_json()
    yield c, sc, app, r.get_json()["track"]
    app.extensions["mcv_tracking"].stop()
    app.extensions["mcv_streams"].shutdown()
    sa.shutdown(); sb.shutdown()


def test_eight_calibration_estimates_cameras(eight_app):
    _, _, _, track = eight_app
    homs = track["calibration"]["homographies"]
    for cid, cam in (("cam1", CAM3D_A), ("cam2", CAM3D_B)):
        pose = homs[cid]["camera"]
        assert pose["height_mm"] == pytest.approx(-cam.C[2], rel=0.1)   # ruido de clic de 1 px


def test_eight_rectified_view(eight_app):
    c, *_ = eight_app
    r = c.get("/api/track/cameras/cam1/rectified.jpg?px_per_mm=0.25")
    assert r.status_code == 200
    img = cv2.imdecode(np.frombuffer(r.data, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (305, 610)


def test_eight_tracking_with_height_correction(eight_app):
    c, sc, app, _ = eight_app
    veh = c.get("/api/vehicles").get_json()["config"]
    veh["count"] = 3
    for v, col in zip(veh["vehicles"], ("#ff0000", "#00ff00", "#0000ff")):
        v["color"], v["area_min_px"] = col, 4
    veh["detection"]["led_height_mm"] = 55
    assert c.put("/api/vehicles", json=veh).status_code == 200
    assert c.post("/api/tracking/start").status_code == 200
    time.sleep(2.5)
    c.post("/api/tracking/reset")
    t0 = time.monotonic()
    a0 = [car["phase"] + car["omega"] * (t0 - sc.t0) for car in sc.cars]
    errs = {1: [], 2: [], 3: []}
    end = time.time() + 14
    while time.time() < end:
        st = c.get("/api/tracking/state").get_json()
        now = time.monotonic()
        for v in st["vehicles"]:
            if v["state"] == "siguiendo":
                gx, gy = sc.car_pos(v["id"] - 1, now)
                errs[v["id"]].append(math.hypot(v["x"] - gx, v["y"] - gy))
        time.sleep(0.1)
    now = time.monotonic()
    st = c.get("/api/tracking/state").get_json()
    for v in st["vehicles"]:
        e = errs[v["id"]]
        assert len(e) > 60 and statistics.mean(e) < 35, f"{v['name']}: error medio {statistics.mean(e):.1f} mm"
        i = v["id"] - 1
        a1 = sc.cars[i]["phase"] + sc.cars[i]["omega"] * (now - sc.t0)
        true_laps = math.floor(a1 / (2 * math.pi)) - math.floor(a0[i] / (2 * math.pi))
        assert v["laps"] == true_laps, f"{v['name']}: {v['laps']} vueltas, reales {true_laps}"
    c.post("/api/tracking/stop")

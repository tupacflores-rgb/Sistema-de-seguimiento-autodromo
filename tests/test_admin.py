"""Pruebas de la página de administrador (backend).

    python -m pytest tests/test_admin.py -v

Usan una IP Webcam simulada (tests/fake_ipwebcam.py, requiere OpenCV y NumPy).
No necesitan teléfonos ni red.
"""
from __future__ import annotations

import json
import socket
import struct
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from admin.config_store import ConfigStore, ValidationError, validate_camera, validate_track  # noqa: E402
from admin.mjpeg import MjpegReader, extract_jpeg, jpeg_size  # noqa: E402

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
from fake_ipwebcam import FakeCamState, serve  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_jpeg(w=64, h=48) -> bytes:
    img = np.zeros((h, w, 3), np.uint8)
    cv2.rectangle(img, (5, 5), (w - 5, h - 5), (0, 0, 255), -1)
    return cv2.imencode(".jpg", img)[1].tobytes()


# ---------------------------------------------------------------- JPEG
def test_jpeg_size():
    assert jpeg_size(make_jpeg(320, 240)) == (320, 240)
    assert jpeg_size(b"no es jpeg") is None


def test_extract_with_exif_thumbnail_containing_eoi():
    """Una miniatura EXIF contiene su propio FFD9: el extractor no debe cortar ahí."""
    main = make_jpeg(200, 100)
    thumb = make_jpeg(16, 8)
    payload = b"Exif\x00\x00" + thumb
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    jpeg = main[:2] + app1 + main[2:]
    buf = bytearray(b"--boundary\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n--boundary\r\n")
    out, consumed = extract_jpeg(buf)
    assert out == jpeg
    assert jpeg_size(out) == (200, 100)
    assert consumed == buf.index(jpeg) + len(jpeg)


def test_extract_incomplete_returns_none():
    j = make_jpeg()
    buf = bytearray(b"basura" + j[: len(j) // 2])
    out, consumed = extract_jpeg(buf)
    assert out is None and consumed == len(b"basura")


# ---------------------------------------------------------------- validación
def test_validate_camera_accepts_full_url():
    cam = validate_camera({"id": "cam9", "host": "http://192.168.1.110:8081/video2", "enabled": True},
                          existing_ids=set(), creating=True)
    assert (cam["host"], cam["port"], cam["video_path"]) == ("192.168.1.110", 8081, "/video2")


@pytest.mark.parametrize("data,field", [
    ({"id": "Cam X", "host": "1.2.3.4"}, "id"),
    ({"id": "c1", "host": "999.1.1.1", "enabled": True}, "host"),
    ({"id": "c1", "host": "1.2.3.4", "port": 70000}, "port"),
    ({"id": "c1", "host": "", "enabled": True}, "host"),
    ({"id": "c1", "host": "1.2.3.4", "processing": {"target_fps": 500}}, "processing.target_fps"),
])
def test_validate_camera_rejects(data, field):
    with pytest.raises(ValidationError) as e:
        validate_camera(data, existing_ids=set(), creating=True)
    assert field in e.value.errors


def test_validate_track_bounds_and_unknown_cameras():
    base = {"width_mm": 2000, "height_mm": 1000, "grid_mm": 50,
            "plan": {"centerline": {"points": [[0, 0], [2000, 1000]], "closed": True}},
            "cameras": {"camX": {"image_width": 640, "image_height": 480, "layers": {}}}}
    t = validate_track(base, camera_ids={"cam1"})
    assert t["cameras"] == {}  # camX no existe: se descarta
    base["plan"]["centerline"]["points"].append([5000, 0])
    with pytest.raises(ValidationError):
        validate_track(base, camera_ids={"cam1"})


def test_store_recovers_from_corrupt_file(tmp_path):
    st = ConfigStore(tmp_path)
    st.cameras_path.parent.mkdir(parents=True)
    st.cameras_path.write_text("{roto", encoding="utf-8")
    doc = st.cameras_doc()
    assert doc["cameras"][0]["id"] == "cam1" and "_aviso" in doc
    assert list(st.cameras_path.parent.glob("cameras.json.danado_*"))


# ---------------------------------------------------------------- lector MJPEG
@pytest.mark.parametrize("content_length", [True, False])
def test_reader_streams_and_reconnects(content_length):
    port = free_port()
    srv, state = serve(port, FakeCamState("T", fps=25, with_content_length=content_length))
    r = MjpegReader("t", f"http://127.0.0.1:{port}/video", backoff_initial=0.1).start()
    try:
        f = r.wait_newer(0, timeout=5)
        assert f is not None and (f.width, f.height) == (960, 720)
        state.drop_next_connection = True  # el servidor corta la conexión
        deadline = time.time() + 6
        while time.time() < deadline and r.reconnects == 0:
            time.sleep(0.05)
        assert r.reconnects >= 1
        seq = r.latest().seq
        assert r.wait_newer(seq, timeout=5) is not None  # volvió a recibir
        assert r.status()["state"] == "streaming"
    finally:
        r.stop(); r.join(2); srv.shutdown()


def test_reader_unreachable_reports_error():
    port = free_port()  # nadie escucha
    r = MjpegReader("t", f"http://127.0.0.1:{port}/video", backoff_initial=0.1, connect_timeout=0.5).start()
    try:
        time.sleep(0.6)
        st = r.status()
        assert st["state"] in ("backoff", "connecting") and st["last_error"]
        assert r.latest() is None
    finally:
        r.stop(); r.join(2)


def test_one_camera_down_does_not_affect_other():
    pa, pb = free_port(), free_port()
    srv, _ = serve(pa, FakeCamState("A", fps=20))
    ra = MjpegReader("a", f"http://127.0.0.1:{pa}/video").start()
    rb = MjpegReader("b", f"http://127.0.0.1:{pb}/video", backoff_initial=0.1, connect_timeout=0.3).start()
    try:
        f1 = ra.wait_newer(0, 5)
        time.sleep(1.0)
        f2 = ra.latest()
        assert f1 and f2 and f2.seq > f1.seq + 5
        assert rb.latest() is None
    finally:
        for r in (ra, rb):
            r.stop(); r.join(2)
        srv.shutdown()


# ---------------------------------------------------------------- API Flask
@pytest.fixture()
def client(tmp_path):
    pytest.importorskip("flask")
    from admin.server import create_app
    port = free_port()
    srv, state = serve(port, FakeCamState("API", fps=20))
    app = create_app(tmp_path, idle_timeout=2)
    c = app.test_client()
    r = c.put("/api/cameras/cam1", json={"name": "Cam", "enabled": True, "host": "127.0.0.1", "port": port})
    assert r.status_code == 200
    yield c, state, tmp_path, app
    app.extensions["mcv_streams"].shutdown()
    srv.shutdown()


def test_api_camera_test_and_state(client):
    c, *_ = client
    r = c.post("/api/cameras/cam1/test").get_json()
    assert r["ok"] and r["video"]["width"] == 960 and r["control"]["video_size"] == "960x720"
    st = c.get("/api/cameras/cam1/state").get_json()
    assert st["state"] == "streaming"


def test_api_password_is_never_returned(client):
    c, *_ = client
    c.put("/api/cameras/cam1", json={"enabled": True, "host": "127.0.0.1", "port": 1, "password": "secreta"})
    doc = c.get("/api/cameras").get_json()
    cam = doc["cameras"][0]
    assert "password" not in cam and cam["has_password"] is True
    assert "secreta" not in json.dumps(doc)
    # editar sin mandar contraseña la conserva
    c.put("/api/cameras/cam1", json={"enabled": False, "host": "127.0.0.1", "port": 1})
    assert c.get("/api/cameras").get_json()["cameras"][0]["has_password"] is True


def test_api_ipwebcam_settings(client):
    c, state, *_ = client
    assert c.post("/api/cameras/cam1/ipwebcam/action", json={"action": "setting", "key": "quality", "value": "50"}).status_code == 200
    r = c.post("/api/cameras/cam1/ipwebcam/action", json={"action": "setting", "key": "video_size", "value": "640x480"}).get_json()
    assert r["affects_calibration"] is True
    c.post("/api/cameras/cam1/ipwebcam/action", json={"action": "torch", "value": True})
    assert state.curvals["quality"] == "50" and state.curvals["video_size"] == "640x480" and state.curvals["torch"] == "on"
    bad = c.post("/api/cameras/cam1/ipwebcam/action", json={"action": "setting", "key": "../x", "value": "1"})
    assert bad.status_code == 502
    st = c.get("/api/cameras/cam1/ipwebcam/status").get_json()
    assert "video_size" in st["status"]["avail"]


def test_api_track_snapshot_and_save(client):
    c, state, root, _ = client
    r = c.post("/api/track/cameras/cam1/snapshot").get_json()
    assert (r["image_width"], r["image_height"]) == (960, 720)
    assert (root / "data/track/snapshots/cam1.jpg").exists()
    t = c.get("/api/track").get_json()["track"]
    t["plan"]["centerline"]["points"] = [[100, 100], [1000, 100], [1000, 900]]
    t["cameras"]["cam1"]["layers"]["exclusion_zones"] = [{"name": "reflejo", "points": [[0, 0], [50, 0], [50, 50]]}]
    saved = c.put("/api/track", json=t).get_json()["track"]
    on_disk = json.loads((root / "data/track/track_layout.json").read_text(encoding="utf-8"))
    assert on_disk["plan"]["centerline"]["points"] == saved["plan"]["centerline"]["points"]
    assert on_disk["cameras"]["cam1"]["layers"]["exclusion_zones"][0]["name"] == "reflejo"


def test_api_stream_and_idle_close(client):
    c, _, _, app = client
    resp = c.get("/api/cameras/cam1/stream.mjpg", buffered=False)
    data = b""
    for chunk in resp.response:
        data += chunk
        if data.count(b"\xff\xd8") >= 2:
            break
    resp.close()
    assert data.startswith(b"--mcvframe")
    # sin nadie mirando, la conexión al teléfono se cierra sola (idle_timeout=2 s)
    mgr = app.extensions["mcv_streams"]
    deadline = time.time() + 8
    while time.time() < deadline and mgr.peek("cam1") is not None:
        time.sleep(0.2)
    assert mgr.peek("cam1") is None


def test_api_delete_camera_removes_drawings(client):
    c, _, root, _ = client
    c.post("/api/track/cameras/cam1/snapshot")
    assert c.delete("/api/cameras/cam1").status_code == 200
    assert "cam1" not in c.get("/api/track").get_json()["track"]["cameras"]
    assert not (root / "data/track/snapshots/cam1.jpg").exists()


def test_api_version_and_cache_headers(client):
    c, *_ = client
    v = c.get("/api/version").get_json()
    from admin import __version__
    assert v["version"] == __version__ and v["restart_needed"] is False
    page = c.get("/")
    assert page.headers["Cache-Control"] == "no-store"
    assert f"admin.js?v={v['assets_v']}".encode() in page.data
    assert c.get("/static/admin.js").headers["Cache-Control"] == "no-cache"


def test_shutdown_only_from_this_pc(client):
    c, *_ = client
    r = c.post("/api/admin/shutdown", environ_base={"REMOTE_ADDR": "192.168.1.50"})
    assert r.status_code == 403


# ---------------------------------------------------------------- respaldos y plantillas
def test_track_size_presets_listed(client):
    c, *_ = client
    r = c.get("/api/track/size_presets").get_json()
    assert r["ok"] and len(r["presets"]) >= 3
    assert all({"id", "label", "width_mm", "height_mm"} <= p.keys() for p in r["presets"])


def test_backup_created_before_each_save_and_can_be_restored(client):
    """El bug reportado era que un guardado podía dejar la pista vacía sin forma de volver
    atrás. Ahora cada PUT guarda antes una copia de la versión anterior en data/backups/."""
    c, _state, root, _app = client

    def track_backups():
        return [b for b in c.get("/api/backups").get_json()["backups"] if b["kind"] == "track"]

    track = c.get("/api/track").get_json()["track"]  # esto ya crea track_layout.json con valores por defecto
    before = len(track_backups())

    # primer guardado real: se respalda la versión por defecto que acaba de crearse arriba
    track["width_mm"] = 1500
    assert c.put("/api/track", json=track).status_code == 200
    assert len(track_backups()) == before + 1

    # segundo guardado: ahora se respalda la versión de 1500 mm antes de pisarla
    track["width_mm"] = 1800
    assert c.put("/api/track", json=track).status_code == 200
    backups = track_backups()
    assert len(backups) == before + 2
    backups.sort(key=lambda b: b["file"])  # el nombre tiene microsegundos; mtime en la API solo segundos
    last = backups[-1]

    # la pista actual tiene el valor nuevo
    assert c.get("/api/track").get_json()["track"]["width_mm"] == 1800

    # `last` es el respaldo que se hizo justo antes de escribir 1800, así que tiene 1500
    r = c.post("/api/backups/restore", json={"file": last["file"]})
    assert r.status_code == 200 and r.get_json()["kind"] == "track"
    assert c.get("/api/track").get_json()["track"]["width_mm"] == 1500
    # restaurar también respaldó la versión de 1800 (para poder deshacer la restauración)
    assert len(track_backups()) == before + 3
    assert (root / "data/backups").exists()


def test_backup_restore_rejects_bad_filenames(client):
    c, *_ = client
    assert c.post("/api/backups/restore", json={"file": "../cameras.json"}).status_code == 400
    assert c.post("/api/backups/restore", json={"file": "track_20200101_000000_000000.json"}).status_code == 404


def test_backups_pruned_to_max_per_kind(client):
    c, *_ = client
    track = c.get("/api/track").get_json()["track"]
    for w in range(100, 100 + 30 * 10, 10):
        track["width_mm"] = w
        assert c.put("/api/track", json=track).status_code == 200
    kinds = [b["kind"] for b in c.get("/api/backups").get_json()["backups"]]
    assert kinds.count("track") <= 25


def test_user_presets_crud(client):
    """Las plantillas propias del usuario son independientes de las predefinidas
    (TRACK_SIZE_PRESETS): se guardan, se listan y se pueden borrar."""
    c, *_ = client
    assert c.get("/api/track/presets").get_json()["presets"] == []

    r = c.post("/api/track/presets", json={"name": "Mi pista de 8", "width_mm": 2000, "height_mm": 1200})
    assert r.status_code == 201
    preset = r.get_json()["preset"]
    assert preset["name"] == "Mi pista de 8"
    assert preset["width_mm"] == 2000 and preset["height_mm"] == 1200
    assert "id" in preset and "created_at" in preset

    listed = c.get("/api/track/presets").get_json()["presets"]
    assert len(listed) == 1 and listed[0]["id"] == preset["id"]

    assert c.delete(f"/api/track/presets/{preset['id']}").status_code == 200
    assert c.get("/api/track/presets").get_json()["presets"] == []


def test_user_presets_validation(client):
    c, *_ = client
    assert c.post("/api/track/presets", json={"name": "", "width_mm": 1000, "height_mm": 1000}).status_code == 400
    assert c.post("/api/track/presets", json={"name": "x" * 41, "width_mm": 1000, "height_mm": 1000}).status_code == 400
    assert c.post("/api/track/presets", json={"name": "Chica", "width_mm": 5, "height_mm": 1000}).status_code == 400
    assert c.post("/api/track/presets", json={"name": "Enorme", "width_mm": 1000, "height_mm": 50000}).status_code == 400


def test_user_presets_delete_missing_is_404(client):
    c, *_ = client
    assert c.delete("/api/track/presets/no_existe").status_code == 404


def test_user_presets_capped(client):
    c, *_ = client
    from admin.config_store import MAX_USER_PRESETS
    for i in range(MAX_USER_PRESETS):
        r = c.post("/api/track/presets", json={"name": f"P{i}", "width_mm": 1000, "height_mm": 1000})
        assert r.status_code == 201
    r = c.post("/api/track/presets", json={"name": "Una de más", "width_mm": 1000, "height_mm": 1000})
    assert r.status_code == 400
    assert len(c.get("/api/track/presets").get_json()["presets"]) == MAX_USER_PRESETS


def test_display_page_loads_and_is_not_cached(client):
    """Pantalla de solo lectura (posiciones + velocidad) para abrir en otro teléfono/tablet."""
    c, *_ = client
    r = c.get("/display")
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") == "no-store"
    body = r.get_data(as_text=True)
    assert "api/tracking/state" in body
    assert "<script" in body.lower()


def test_display_reuses_tracking_state_api(client):
    """No duplica el motor de carrera: usa la misma /api/tracking/state que la sección Tracking."""
    c, *_ = client
    r = c.get("/api/tracking/state")
    assert r.status_code == 200
    d = r.get_json()
    assert "running" in d and "vehicles" in d

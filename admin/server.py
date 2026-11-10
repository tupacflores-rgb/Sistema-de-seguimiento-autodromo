"""Servidor web local de la página de administrador (Flask).

Ejecutar desde la carpeta MultiCam_Vision:
    python -m admin                      -> http://127.0.0.1:5050 (solo esta PC)
    python -m admin --host 0.0.0.0       -> accesible desde otros equipos de la red local

El servidor es intermediario entre el navegador y los teléfonos:
- re-emite el video (una sola conexión por teléfono, sin problemas de CORS),
- consulta y cambia ajustes de IP Webcam,
- guarda cámaras y pista en archivos JSON.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file

from . import __version__
from .config_store import ConfigStore, ValidationError, camera_base_url, camera_video_url
from .esp32 import Esp32Manager
from .esp32 import missing_dependency as esp32_missing_dependency
from .ipwebcam import GEOMETRY_KEYS, IpWebcamClient, IpWebcamError
from .mjpeg import StreamManager
from .tracking import TrackingEngine, missing_dependency

log = logging.getLogger("mcv.admin")

BOUNDARY = "mcvframe"
PKG_DIR = Path(__file__).resolve().parent


def _fingerprint(patterns: tuple[str, ...]) -> int:
    """Mayor fecha de modificación (ms) de los archivos del programa: detecta actualizaciones."""
    newest = 0.0
    for pat in patterns:
        for f in PKG_DIR.glob(pat):
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                pass
    return int(newest * 1000)


def code_fingerprint() -> int:
    return _fingerprint(("*.py",))


def assets_fingerprint() -> int:
    return _fingerprint(("static/*", "templates/*"))


def create_app(root: Path, idle_timeout: float = 30.0, *,
               esp32_scheduler=None, esp32_link_factory=None) -> Flask:
    """`esp32_scheduler`/`esp32_link_factory` solo los usan las pruebas (tests/test_esp32.py) para
    inyectar un reloj falso y no esperar seis segundos reales, y para simular el enlace serial sin
    hardware ni pyserial. En producción quedan en None y Esp32Manager usa sus valores reales."""
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["JSON_AS_ASCII"] = False
    app.json.ensure_ascii = False
    # Que los cambios de HTML/JS/CSS se vean siempre, sin reiniciar ni pelear con la caché:
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    started = {"code": code_fingerprint(), "at": time.time(), "pid": os.getpid()}
    store = ConfigStore(root)
    streams = StreamManager(idle_timeout=idle_timeout)
    app.extensions["mcv_store"] = store
    app.extensions["mcv_streams"] = streams
    engine = TrackingEngine(store, streams)
    app.extensions["mcv_tracking"] = engine
    esp32_kwargs = {}
    if esp32_scheduler is not None:
        esp32_kwargs["scheduler"] = esp32_scheduler
    if esp32_link_factory is not None:
        esp32_kwargs["link_factory"] = esp32_link_factory
    esp32 = Esp32Manager(store, engine, **esp32_kwargs)
    app.extensions["mcv_esp32"] = esp32

    # ------------------------------------------------------------ utilidades
    def camera_or_404(cid: str) -> dict:
        cam = store.get_camera(cid)
        if cam is None:
            abort(404, description=f"No existe la cámara '{cid}'")
        return cam

    def require_host(cam: dict) -> None:
        if not cam.get("host"):
            abort(409, description="La cámara no tiene IP configurada")

    def reader_for(cam: dict):
        require_host(cam)
        return streams.get(cam["id"], camera_video_url(cam), cam.get("username", ""), cam.get("password", ""))

    def client_for(cam: dict) -> IpWebcamClient:
        require_host(cam)
        return IpWebcamClient(camera_base_url(cam), cam.get("username", ""), cam.get("password", ""))

    def public_camera(cam: dict) -> dict:
        out = dict(cam)
        out["has_password"] = bool(out.pop("password", ""))
        out["video_url"] = camera_video_url(cam) if cam.get("host") else None
        return out

    @app.errorhandler(ValidationError)
    def _val(e: ValidationError):
        return jsonify(ok=False, error="Datos inválidos", fields=e.errors), 400

    @app.errorhandler(IpWebcamError)
    def _ipw(e: IpWebcamError):
        return jsonify(ok=False, error=str(e)), 502

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(403)
    @app.errorhandler(409)
    def _http(e):
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error=e.description), e.code
        return e

    @app.after_request
    def _no_cache(resp):
        if request.path.startswith("/api/") or request.path in ("/", "/display"):
            resp.headers["Cache-Control"] = "no-store"
        elif request.path.startswith("/static/"):
            # el navegador siempre pregunta si cambió (las URLs además llevan ?v=<versión>)
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    # ------------------------------------------------------------ página
    @app.get("/")
    def index():
        return render_template("admin.html", version=__version__, assets_v=assets_fingerprint())

    @app.get("/display")
    def display():
        # Pantalla de solo lectura (posiciones + velocidad), pensada para abrir en un teléfono
        # o una tablet aparte y mostrarla al público. No tiene ningún control de edición; usa
        # la misma /api/tracking/state que ya alimenta la sección 6, así que no duplica nada.
        return render_template("display.html", version=__version__, assets_v=assets_fingerprint())

    @app.get("/api/version")
    def version():
        code_now = code_fingerprint()
        return jsonify(ok=True, version=__version__, pid=started["pid"],
                       started_at=started["at"], assets_v=assets_fingerprint(),
                       restart_needed=code_now > started["code"] + 1000)

    @app.post("/api/admin/shutdown")
    def admin_shutdown():
        """Lo usa una página de administrador NUEVA para cerrar esta (vieja). Solo desde esta PC."""
        if request.remote_addr not in ("127.0.0.1", "::1"):
            abort(403, description="Solo desde esta PC")
        engine.stop()
        esp32.stop()
        streams.shutdown()
        threading.Timer(0.3, lambda: os._exit(0)).start()
        return jsonify(ok=True)

    # ------------------------------------------------------------ respaldos
    # Antes de cada guardado de cámaras/pista/vehículos se copia la versión anterior a
    # data/backups/. Esto es independiente de cualquier bug de guardado: si una edición sale
    # mal (datos borrados de más, un guardado a medio terminar), siempre hay una versión previa
    # para volver atrás sin tener que rehacer todo.
    @app.get("/api/backups")
    def backups_list():
        return jsonify(ok=True, backups=store.list_backups())

    @app.post("/api/backups/restore")
    def backups_restore():
        data = request.get_json(force=True, silent=True) or {}
        file = str(data.get("file", ""))
        try:
            info = store.restore_backup(file)
        except FileNotFoundError:
            abort(404, description="Ese respaldo ya no existe")
        except ValueError as e:
            abort(400, description=str(e))
        engine.reload()
        return jsonify(ok=True, **info)

    # ------------------------------------------------------------ cámaras (sección 1)
    @app.get("/api/cameras")
    def cameras_list():
        doc = store.cameras_doc()
        return jsonify(ok=True, cameras=[public_camera(c) for c in doc.get("cameras", [])],
                       warning=doc.get("_aviso"))

    @app.post("/api/cameras")
    def cameras_create():
        cam = store.upsert_camera(request.get_json(force=True, silent=True) or {})
        return jsonify(ok=True, camera=public_camera(cam)), 201

    @app.put("/api/cameras/<cid>")
    def cameras_update(cid):
        old = camera_or_404(cid)
        data = request.get_json(force=True, silent=True) or {}
        # Si el campo contraseña viene vacío y "keep_password" está activo, se conserva la anterior
        if data.get("keep_password", True) and not data.get("password"):
            data["password"] = old.get("password", "")
        cam = store.upsert_camera(data, cid=cid)
        if (camera_video_url(old) if old.get("host") else None) != (camera_video_url(cam) if cam.get("host") else None) \
                or not cam["enabled"] or old.get("username") != cam.get("username") \
                or old.get("password") != cam.get("password"):
            streams.drop(cid)
        return jsonify(ok=True, camera=public_camera(cam))

    @app.delete("/api/cameras/<cid>")
    def cameras_delete(cid):
        camera_or_404(cid)
        streams.drop(cid)
        store.delete_camera(cid)
        return jsonify(ok=True)

    @app.post("/api/cameras/<cid>/test")
    def cameras_test(cid):
        """Prueba la conexión de control (/status.json) y si llega un JPEG del video."""
        cam = camera_or_404(cid)
        require_host(cam)
        result = {"ok": False, "control": None, "video": None}
        try:
            result["control"] = client_for(cam).ping()
        except IpWebcamError as e:
            result["control"] = {"ok": False, "error": str(e)}
        if cam["enabled"]:
            r = reader_for(cam)
            t0 = time.monotonic()
            f = r.latest() or r.wait_newer(0, timeout=6.0)
            if f is not None:
                result["video"] = {"ok": True, "width": f.width, "height": f.height,
                                   "first_frame_ms": round((time.monotonic() - t0) * 1000)}
            else:
                result["video"] = {"ok": False, "error": r.last_error or "No llegó ningún frame en 6 s"}
        else:
            result["video"] = {"ok": False, "error": "Cámara deshabilitada: no se abre el video"}
        result["ok"] = bool(result["control"] and result["control"].get("ok")) and bool(result["video"] and result["video"].get("ok"))
        return jsonify(result)

    # ------------------------------------------------------------ video (sección 2)
    @app.get("/api/cameras/<cid>/state")
    def camera_state(cid):
        cam = camera_or_404(cid)
        r = streams.peek(cid)
        if r is None:
            st = {"cam_id": cid, "state": "idle" if cam["enabled"] else "disabled"}
        else:
            st = r.status()  # consultar el estado NO mantiene viva la conexión (solo el video)
        return jsonify(ok=True, enabled=cam["enabled"], **st)

    @app.get("/api/cameras/<cid>/stream.mjpg")
    def camera_stream(cid):
        cam = camera_or_404(cid)
        if not cam["enabled"]:
            abort(409, description="Cámara deshabilitada")
        r = reader_for(cam)
        max_fps = request.args.get("fps", type=float) or 0.0
        min_dt = 1.0 / max_fps if max_fps > 0 else 0.0

        def gen():
            last_seq, last_sent = 0, 0.0
            while True:
                f = r.wait_newer(last_seq, timeout=1.0)
                if f is None:
                    if not r.alive:
                        return
                    continue
                now = time.monotonic()
                if min_dt and now - last_sent < min_dt:
                    last_seq = f.seq
                    continue
                last_seq, last_sent = f.seq, now
                yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                       f"Content-Length: {len(f.data)}\r\n\r\n").encode() + f.data + b"\r\n"

        return Response(gen(), mimetype=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/cameras/<cid>/snapshot.jpg")
    def camera_snapshot(cid):
        cam = camera_or_404(cid)
        if not cam["enabled"]:
            abort(409, description="Cámara deshabilitada")
        r = reader_for(cam)
        f = r.latest() or r.wait_newer(0, timeout=6.0)
        if f is None:
            abort(409, description=r.last_error or "Sin imagen de la cámara")
        return Response(f.data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ ajustes IP Webcam (sección 3)
    @app.get("/api/cameras/<cid>/ipwebcam/status")
    def ipw_status(cid):
        cam = camera_or_404(cid)
        st = client_for(cam).status()
        return jsonify(ok=True, status=st, geometry_keys=sorted(GEOMETRY_KEYS))

    @app.post("/api/cameras/<cid>/ipwebcam/action")
    def ipw_action(cid):
        cam = camera_or_404(cid)
        cli = client_for(cam)
        data = request.get_json(force=True, silent=True) or {}
        action = data.get("action")
        if action == "setting":
            msg = cli.set_setting(str(data.get("key", "")), data.get("value", ""))
            affects = str(data.get("key")) in GEOMETRY_KEYS
        elif action == "zoom":
            msg, affects = cli.set_zoom_index(int(data.get("value", 0))), True
        elif action == "torch":
            msg, affects = cli.torch(bool(data.get("value"))), False
        elif action == "focus":
            msg, affects = cli.focus(bool(data.get("value"))), False
        elif action == "recommended":
            msg, affects = "\n".join(cli.apply_recommended()), True
        else:
            abort(400, description="Acción desconocida")
        return jsonify(ok=True, message=msg, affects_calibration=affects)

    @app.put("/api/cameras/<cid>/processing")
    def camera_processing(cid):
        cam = camera_or_404(cid)
        data = request.get_json(force=True, silent=True) or {}
        cam["processing"] = {**cam.get("processing", {}), **(data.get("processing") or {})}
        saved = store.upsert_camera(cam, cid=cid)
        return jsonify(ok=True, camera=public_camera(saved))

    # ------------------------------------------------------------ pista (sección 4)
    @app.get("/api/track")
    def track_get():
        return jsonify(ok=True, track=store.get_track())

    @app.get("/api/track/size_presets")
    def track_size_presets():
        from .config_store import TRACK_SIZE_PRESETS
        return jsonify(ok=True, presets=TRACK_SIZE_PRESETS)

    @app.get("/api/track/presets")
    def track_presets_list():
        return jsonify(ok=True, presets=store.list_presets())

    @app.post("/api/track/presets")
    def track_presets_create():
        data = request.get_json(force=True, silent=True) or {}
        try:
            preset = store.save_preset(data)
        except ValueError as e:
            abort(400, description=str(e))
        return jsonify(ok=True, preset=preset), 201

    @app.delete("/api/track/presets/<pid>")
    def track_presets_delete(pid):
        try:
            store.delete_preset(pid)
        except FileNotFoundError:
            abort(404, description="Esa plantilla ya no existe")
        return jsonify(ok=True)

    @app.put("/api/track")
    def track_put():
        track = store.save_track(request.get_json(force=True, silent=True) or {})
        engine.reload()  # si el tracking está activo, toma la pista/calibración nueva
        return jsonify(ok=True, track=track)

    @app.post("/api/track/cameras/<cid>/snapshot")
    def track_take_snapshot(cid):
        cam = camera_or_404(cid)
        if not cam["enabled"]:
            abort(409, description="Habilitá la cámara para capturar una imagen")
        r = reader_for(cam)
        f = r.latest() or r.wait_newer(0, timeout=6.0)
        if f is None or not f.width:
            abort(409, description=r.last_error or "No llegó imagen de la cámara")
        info = store.save_snapshot(cid, f.data, f.width, f.height)
        return jsonify(ok=True, **info)

    @app.get("/api/track/cameras/<cid>/rectified.jpg")
    def track_rectified(cid):
        """Foto de referencia 'enderezada' sobre el plano (vista cenital) con la calibración guardada."""
        camera_or_404(cid)
        need_cv()
        import cv2
        import numpy as np
        track = store.get_track()
        hom = track.get("calibration", {}).get("homographies", {}).get(cid, {})
        if "H_img_to_plan" not in hom:
            abort(409, description="Cámara sin calibrar: guardá 4 o más puntos de referencia")
        p = store.snapshot_path(cid)
        if not p.exists():
            abort(404, description="Todavía no hay imagen de referencia")
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            abort(409, description="No se pudo leer la imagen de referencia")
        k = max(0.1, min(2.0, request.args.get("px_per_mm", 0.5, type=float)))
        W, H = int(track["width_mm"] * k), int(track["height_mm"] * k)
        M = np.diag([k, k, 1.0]) @ np.array(hom["H_img_to_plan"], np.float64).reshape(3, 3)
        out = cv2.warpPerspective(img, M, (W, H), flags=cv2.INTER_LINEAR, borderValue=(30, 30, 30))
        ok, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return Response(enc.tobytes(), mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/track/cameras/<cid>/snapshot.jpg")
    def track_snapshot_file(cid):
        camera_or_404(cid)
        p = store.snapshot_path(cid)
        if not p.exists():
            abort(404, description="Todavía no hay imagen de referencia")
        return send_file(p, mimetype="image/jpeg", max_age=0)

    # ------------------------------------------------------------ vehículos (sección 5)
    def latest_jpeg(cid: str) -> bytes:
        cam = camera_or_404(cid)
        if not cam["enabled"]:
            abort(409, description="Cámara deshabilitada")
        r = reader_for(cam)
        f = r.latest() or r.wait_newer(0, timeout=6.0)
        if f is None:
            abort(409, description=r.last_error or "Sin imagen de la cámara")
        return f.data

    def need_cv():
        err = missing_dependency()
        if err:
            abort(409, description=err)

    @app.get("/api/vehicles")
    def vehicles_get():
        cfg = store.get_vehicles()
        from .config_store import validate_vehicles
        try:
            _, warnings = validate_vehicles(cfg)
        except ValidationError:
            warnings = []
        return jsonify(ok=True, config=cfg, warnings=warnings, max_vehicles=4)

    @app.put("/api/vehicles")
    def vehicles_put():
        cfg, warnings = store.save_vehicles(request.get_json(force=True, silent=True) or {})
        engine.reload()
        return jsonify(ok=True, config=cfg, warnings=warnings)

    @app.post("/api/vehicles/sample")
    def vehicles_sample():
        need_cv()
        from .tracking import sample_color
        d = request.get_json(force=True, silent=True) or {}
        jpeg = latest_jpeg(str(d.get("cam_id", "")))
        try:
            res = sample_color(jpeg, float(d.get("x", -1)), float(d.get("y", -1)), int(d.get("radius", 6)))
        except (TypeError, ValueError) as e:
            abort(400, description=str(e))
        return jsonify(ok=True, **res)

    @app.get("/api/vehicles/<int:vid>/mask/<cid>.jpg")
    def vehicles_mask(vid, cid):
        """Vista previa de detección por color. Acepta parámetros sin guardar (?color=...&hue_tol=...)."""
        need_cv()
        from .config_store import validate_vehicles
        from .tracking import mask_preview
        cfg = store.get_vehicles()
        if not 1 <= vid <= len(cfg["vehicles"]):
            abort(404, description="Vehículo inexistente")
        v = dict(cfg["vehicles"][vid - 1])
        for k in ("color", "name"):
            if request.args.get(k):
                v[k] = request.args[k]
        for k in ("hue_tol", "sat_min", "val_min", "area_min_px", "area_max_px"):
            if request.args.get(k) is not None:
                v[k] = request.args.get(k, type=int)
        probe = {**cfg, "vehicles": [v if i == vid - 1 else x for i, x in enumerate(cfg["vehicles"])]}
        validate_vehicles(probe)  # lanza ValidationError (400) si los parámetros son inválidos
        img, stats = mask_preview(latest_jpeg(cid), v)
        return Response(img, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store", "X-Mask-Stats": json.dumps(stats)})

    # ------------------------------------------------------------ tracking (sección 6)
    @app.post("/api/tracking/start")
    def tracking_start():
        try:
            st = engine.start()
        except RuntimeError as e:
            abort(409, description=str(e))
        return jsonify(ok=True, state=st)

    @app.post("/api/tracking/stop")
    def tracking_stop():
        engine.stop()  # corta tanto el seguimiento real como el modo demo, si estuviera activo
        return jsonify(ok=True, state=engine.state())

    @app.post("/api/tracking/demo/start")
    def tracking_demo_start():
        try:
            st = engine.start_demo()
        except RuntimeError as e:
            abort(409, description=str(e))
        return jsonify(ok=True, state=st)

    @app.post("/api/tracking/reset")
    def tracking_reset():
        engine.reset_race()
        return jsonify(ok=True)

    @app.get("/api/tracking/state")
    def tracking_state():
        return jsonify(ok=True, dependency_error=missing_dependency(), **engine.state())

    @app.get("/api/tracking/cameras/<cid>/debug.mjpg")
    def tracking_debug(cid):
        w = engine.debug_worker(cid)
        if w is None:
            abort(409, description="El tracking no está activo para esta cámara")

        def gen():
            last = 0
            while engine.running and engine.debug_worker(cid) is w:
                w.want_debug()
                with w.debug_cond:
                    if w.debug_seq == last:
                        w.debug_cond.wait(0.5)
                    data, seq = w.debug_jpeg, w.debug_seq
                if data is None or seq == last:
                    continue
                last = seq
                yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                       f"Content-Length: {len(data)}\r\n\r\n").encode() + data + b"\r\n"

        return Response(gen(), mimetype=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                        headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ ESP32: semáforo de salida + RFID
    # Sección nueva ("7 · ESP32 y semáforo"), aparte de las 6 de siempre. Reutiliza el mismo
    # ConfigStore (config/hardware.json + el campo rfid_uid de cada vehículo) y, al llegar a cero
    # en la cuenta regresiva, el único motor de carrera real (TrackingEngine.reset_race()).
    @app.get("/api/hardware")
    def hardware_get():
        return jsonify(ok=True, hardware=store.get_hardware())

    @app.put("/api/hardware")
    def hardware_put():
        hw = store.save_hardware(request.get_json(force=True, silent=True) or {})
        esp32.reload()  # reconecta solo si cambió modo/puerto/baudrate
        return jsonify(ok=True, hardware=hw)

    @app.get("/api/esp32/state")
    def esp32_state():
        return jsonify(ok=True, dependency_error=esp32_missing_dependency() if esp32.mode == "hardware" else None,
                       **esp32.public_state())

    @app.post("/api/esp32/race/start")
    def esp32_race_start():
        try:
            st = esp32.start_race()
        except RuntimeError as e:
            abort(409, description=str(e))
        return jsonify(ok=True, **st)

    @app.post("/api/esp32/race/cancel")
    def esp32_race_cancel():
        return jsonify(ok=True, **esp32.cancel_race())

    @app.post("/api/esp32/race/reset")
    def esp32_race_reset():
        return jsonify(ok=True, **esp32.reset_semaphore())

    @app.post("/api/esp32/rfid/simulate")
    def esp32_rfid_simulate():
        data = request.get_json(force=True, silent=True) or {}
        try:
            lane = int(data.get("lane", 0))
        except (TypeError, ValueError):
            abort(400, description="Lector inválido")
        try:
            info = esp32.simulate_rfid(lane, data.get("uid"))
        except RuntimeError as e:
            abort(409, description=str(e))
        return jsonify(ok=True, reading=info, **esp32.public_state())

    return app

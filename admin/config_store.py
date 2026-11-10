"""Persistencia y validación de la configuración editable desde la página de administrador.

Archivos (relativos a la raíz del proyecto):
    config/cameras.json            -> lista de cámaras (IP, puerto, ruta, credenciales, parámetros)
    data/track/track_layout.json   -> pista: plano en mm + dibujos por cámara en píxeles
    data/track/snapshots/<cam>.jpg -> imagen de referencia para dibujar sobre cada cámara

Decisión (registrada en docs/REGISTRO_CENTRAL.md, D15): lo que se edita desde la web se guarda
en JSON porque la librería estándar puede leer TOML (tomllib) pero no escribirlo, y no se quiere
sumar dependencias. La escritura es atómica (archivo temporal + os.replace) para no dejar
archivos corruptos si el programa se corta a mitad de una escritura.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import homography as hg

SCHEMA_VERSION = 1

_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")

LAYER_LINES = ("centerline", "border_outer")
POLY_LISTS = {"inner_islands": "Isla", "exclusion_zones": "Zona"}   # listas de polígonos cerrados
MAX_POINTS_PER_LAYER = 2000
MAX_ZONES = 50

DEFAULT_PROCESSING = {"max_frame_age_ms": 200, "latency_offset_ms": 0, "target_fps": 30}

# Respaldos automáticos: antes de sobrescribir cameras.json/track_layout.json/vehicles.json se
# copia la versión anterior a data/backups/, para poder recuperarla si una edición sale mal
# (por ejemplo, una validación que borra de más, o un guardado a medio terminar).
BACKUP_KINDS = ("cameras", "track", "vehicles")
MAX_BACKUPS_PER_KIND = 25

# Plantillas de tamaño de plancha que guarda el propio usuario (además de las de abajo).
MAX_USER_PRESETS = 40

# Tamaños de plancha típicos, para que el primer seteo no empiece desde cero.
TRACK_SIZE_PRESETS = [
    {"id": "mdf_2440x1220", "label": "Chapadur 2440 × 1220 mm (8 × 4 pies, apaisada)", "width_mm": 2440, "height_mm": 1220},
    {"id": "mdf_1220x2440", "label": "Chapadur 1220 × 2440 mm (4 × 8 pies, vertical)", "width_mm": 1220, "height_mm": 2440},
    {"id": "mdf_3050x1525", "label": "Chapadur 3050 × 1525 mm (10 × 5 pies)", "width_mm": 3050, "height_mm": 1525},
    {"id": "mdf_1830x1220", "label": "Chapadur 1830 × 1220 mm (6 × 4 pies)", "width_mm": 1830, "height_mm": 1220},
    {"id": "figure8_small", "label": "Pista en 8 chica (sugerido, 2000 × 1200 mm)", "width_mm": 2000, "height_mm": 1200},
]


class ValidationError(ValueError):
    """Error de validación con un mensaje apto para mostrar en la página."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- ESP32 + semáforo + RFID
# config/hardware.json: ESP32 (modo simulación/hardware, puerto serial, pinout informativo de los 3
# RC522 y el 74HC595N) y semáforo de salida (cantidad de LEDs, duración de la cuenta regresiva).
# El UID de la tarjeta RFID de cada vehículo se guarda junto al resto de datos del vehículo
# (config/vehicles.json, campo "rfid_uid"): es "el mapeo existente" UID -> vehicle_id.

VALID_BAUDRATES = (9600, 19200, 38400, 57600, 74880, 115200, 230400, 460800, 921600)


def default_hardware() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "esp32": {
            "mode": "simulation",          # "simulation" | "hardware"
            "port": "",                    # p. ej. "COM5"; vacío = sin elegir todavía
            "baudrate": 115200,
            "reader_count": 3,
            "reader_lanes": [1, 2, 3],
            "spi_sck": 18, "spi_miso": 19, "spi_mosi": 23,
            "reader_cs_pins": [21, 22, 32],
            "reader_rst_pin": 33,
        },
        "semaphore": {
            "led_count": 6,
            "countdown_seconds": 6,
            "tick_seconds": 1.0,
            "data_pin": 25, "clock_pin": 26, "latch_pin": 27,
        },
        "updated_at": _now(),
    }


def validate_hardware(data: dict) -> dict:
    """Devuelve la configuración normalizada o lanza ValidationError. Los pines GPIO son
    informativos para el firmware (no los usa Python); se validan como enteros 0-39 nomás."""
    if not isinstance(data, dict):
        raise ValidationError({"_": "Se esperaba un objeto JSON"})
    err: dict[str, str] = {}
    out = default_hardware()
    esp_in = data.get("esp32") or {}
    sem_in = data.get("semaphore") or {}

    mode = str(esp_in.get("mode", "simulation")).strip().lower()
    if mode not in ("simulation", "hardware"):
        err["esp32.mode"] = "Tiene que ser 'simulation' o 'hardware'"
        mode = "simulation"
    out["esp32"]["mode"] = mode
    out["esp32"]["port"] = str(esp_in.get("port", "")).strip()[:40]
    try:
        br = int(esp_in.get("baudrate", 115200))
        if br not in VALID_BAUDRATES:
            raise ValueError
        out["esp32"]["baudrate"] = br
    except (TypeError, ValueError):
        err["esp32.baudrate"] = "Velocidad serial inválida"

    for k in ("spi_sck", "spi_miso", "spi_mosi", "reader_rst_pin"):
        if k in esp_in:
            try:
                p = int(esp_in[k])
                if not 0 <= p <= 39:
                    raise ValueError
                out["esp32"][k] = p
            except (TypeError, ValueError):
                err[f"esp32.{k}"] = "GPIO entre 0 y 39"
    if "reader_cs_pins" in esp_in:
        raw = esp_in["reader_cs_pins"]
        try:
            pins = [int(p) for p in raw]
            if len(pins) != 3 or not all(0 <= p <= 39 for p in pins):
                raise ValueError
            out["esp32"]["reader_cs_pins"] = pins
        except (TypeError, ValueError):
            err["esp32.reader_cs_pins"] = "Tres pines GPIO (0-39), uno por lector"

    for k, (lo, hi) in {"led_count": (1, 16), "countdown_seconds": (1, 60)}.items():
        try:
            v = int(sem_in.get(k, out["semaphore"][k]))
            if not lo <= v <= hi:
                raise ValueError
            out["semaphore"][k] = v
        except (TypeError, ValueError):
            err[f"semaphore.{k}"] = f"Entero entre {lo} y {hi}"
    try:
        t = float(sem_in.get("tick_seconds", out["semaphore"]["tick_seconds"]))
        if not 0.2 <= t <= 5.0:
            raise ValueError
        out["semaphore"]["tick_seconds"] = t
    except (TypeError, ValueError):
        err["semaphore.tick_seconds"] = "Entre 0.2 y 5 s"
    for k in ("data_pin", "clock_pin", "latch_pin"):
        if k in sem_in:
            try:
                p = int(sem_in[k])
                if not 0 <= p <= 39:
                    raise ValueError
                out["semaphore"][k] = p
            except (TypeError, ValueError):
                err[f"semaphore.{k}"] = "GPIO entre 0 y 39"

    if err:
        raise ValidationError(err)
    out["updated_at"] = _now()
    return out


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _empty_layers() -> dict:
    layers = {name: {"points": [], "closed": True} for name in LAYER_LINES}
    layers["inner_islands"] = []
    layers["exclusion_zones"] = []
    return layers


def default_cameras() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "cameras": [
            {"id": "cam1", "name": "Cámara 1", "enabled": True, "scheme": "http",
             "host": "192.168.1.110", "port": 8080, "video_path": "/video",
             "username": "", "password": "", "processing": dict(DEFAULT_PROCESSING), "notes": ""},
            {"id": "cam2", "name": "Cámara 2", "enabled": False, "scheme": "http",
             "host": "", "port": 8080, "video_path": "/video",
             "username": "", "password": "", "processing": dict(DEFAULT_PROCESSING), "notes": "Sin configurar"},
        ],
        "updated_at": _now(),
    }


def default_track() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "units": "mm",
        "width_mm": 2440,
        "height_mm": 1220,
        "grid_mm": 50,
        "plan": _empty_layers(),
        "cameras": {},
        "calibration": {"points": [], "homographies": {}},
        "updated_at": _now(),
    }


# --------------------------------------------------------------------------- vehículos

MAX_VEHICLES = 4
DEFAULT_VEHICLE_COLORS = ["#ff0000", "#00ff00", "#0000ff", "#ff00ff"]  # tonos separados >= 60°


def _default_vehicle(i: int) -> dict:
    return {"id": i + 1, "name": f"Auto {i + 1}", "color": DEFAULT_VEHICLE_COLORS[i],
            "hue_tol": 12, "sat_min": 90, "val_min": 90, "area_min_px": 20, "area_max_px": 20000,
            "rfid_uid": ""}  # UID (hex) de la tarjeta RFID de este auto; "" = sin asignar


def default_vehicles() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "count": 3,
        "vehicles": [_default_vehicle(i) for i in range(MAX_VEHICLES)],
        "detection": {"require_motion_to_start": True, "limit_to_track": True,
                      "process_max_width": 960, "lost_after_s": 1.0, "led_height_mm": 50},
        "updated_at": _now(),
    }


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_RFID_UID_RE = re.compile(r"^[0-9A-F]{4,20}$")


def hex_to_hsv_cv(color: str) -> tuple[int, int, int]:
    """'#rrggbb' -> (H 0-179, S 0-255, V 0-255) en la convención de OpenCV."""
    import colorsys
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, s_, v = colorsys.rgb_to_hsv(r, g, b)
    return round(h * 179) % 180, round(s_ * 255), round(v * 255)


def hue_distance_deg(c1: str, c2: str) -> float:
    h1, h2 = hex_to_hsv_cv(c1)[0] * 2, hex_to_hsv_cv(c2)[0] * 2
    d = abs(h1 - h2) % 360
    return min(d, 360 - d)


def validate_vehicles(data: dict) -> tuple[dict, list[str]]:
    """Devuelve (config normalizada, avisos)."""
    if not isinstance(data, dict):
        raise ValidationError({"_": "Se esperaba un objeto JSON"})
    err: dict[str, str] = {}
    try:
        count = int(data.get("count", 3))
        if not 1 <= count <= MAX_VEHICLES:
            raise ValueError
    except (TypeError, ValueError):
        err["count"] = f"Entre 1 y {MAX_VEHICLES}"
        count = 3
    vin = data.get("vehicles") or []
    vehicles = []
    limits = {"hue_tol": (2, 45), "sat_min": (0, 255), "val_min": (0, 255),
              "area_min_px": (1, 100000), "area_max_px": (10, 2000000)}
    for i in range(MAX_VEHICLES):
        src = vin[i] if i < len(vin) and isinstance(vin[i], dict) else {}
        v = _default_vehicle(i)
        v["name"] = (str(src.get("name", v["name"])).strip() or v["name"])[:30]
        color = str(src.get("color", v["color"])).strip()
        if not _HEX_RE.match(color):
            err[f"vehicles[{i}].color"] = "Color #rrggbb"
        else:
            v["color"] = color.lower()
        for k, (lo, hi) in limits.items():
            if k in src:
                try:
                    val = int(src[k])
                    if not lo <= val <= hi:
                        raise ValueError
                    v[k] = val
                except (TypeError, ValueError):
                    err[f"vehicles[{i}].{k}"] = f"Entero entre {lo} y {hi}"
        if v["area_min_px"] >= v["area_max_px"]:
            err[f"vehicles[{i}].area_max_px"] = "Debe ser mayor que el área mínima"
        uid = str(src.get("rfid_uid", "")).strip().upper()
        if uid and not _RFID_UID_RE.match(uid):
            err[f"vehicles[{i}].rfid_uid"] = "UID hexadecimal (4 a 20 caracteres), o vacío si no tiene"
        else:
            v["rfid_uid"] = uid
        vehicles.append(v)
    det_in = data.get("detection") or {}
    det = dict(default_vehicles()["detection"])
    for k in ("require_motion_to_start", "limit_to_track"):
        if k in det_in:
            det[k] = bool(det_in[k])
    for k, (lo, hi) in {"process_max_width": (320, 1920), "led_height_mm": (0, 300)}.items():
        if k in det_in:
            try:
                val = int(det_in[k])
                if not lo <= val <= hi:
                    raise ValueError
                det[k] = val
            except (TypeError, ValueError):
                err[f"detection.{k}"] = f"Entero entre {lo} y {hi}"
    if "lost_after_s" in det_in:
        try:
            val = float(det_in["lost_after_s"])
            if not 0.1 <= val <= 10:
                raise ValueError
            det["lost_after_s"] = val
        except (TypeError, ValueError):
            err["detection.lost_after_s"] = "Entre 0.1 y 10 s"
    if err:
        raise ValidationError(err)
    warnings = []
    active = vehicles[:count]
    for a in range(len(active)):
        for b in range(a + 1, len(active)):
            d = hue_distance_deg(active[a]["color"], active[b]["color"])
            if d < 40:
                warnings.append(f"{active[a]['name']} y {active[b]['name']} tienen colores parecidos "
                                f"({d:.0f}° de diferencia de tono): las cámaras pueden confundirlos")
    for v in active:
        _, s_, v_ = hex_to_hsv_cv(v["color"])
        if s_ < 80:
            warnings.append(f"{v['name']}: color poco saturado (blanco/gris); es difícil de distinguir del fondo")
    seen_uids: dict[str, str] = {}
    for v in active:
        if v["rfid_uid"]:
            prev = seen_uids.get(v["rfid_uid"])
            if prev:
                warnings.append(f"{prev} y {v['name']} tienen la misma etiqueta RFID ({v['rfid_uid']}): "
                                f"el lector no podría distinguirlos")
            else:
                seen_uids[v["rfid_uid"]] = v["name"]
    return ({"schema_version": SCHEMA_VERSION, "count": count, "vehicles": vehicles, "detection": det},
            warnings)


# --------------------------------------------------------------------------- cámaras

def camera_base_url(cam: dict) -> str:
    host = cam["host"]
    try:
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            host = f"[{host}]"
    except ValueError:
        pass
    return f"{cam['scheme']}://{host}:{cam['port']}"


def camera_video_url(cam: dict) -> str:
    return camera_base_url(cam) + cam["video_path"]


def validate_camera(data: dict, *, existing_ids: set[str], creating: bool) -> dict:
    """Devuelve una cámara normalizada o lanza ValidationError."""
    if not isinstance(data, dict):
        raise ValidationError({"_": "Se esperaba un objeto JSON"})
    err: dict[str, str] = {}
    out: dict[str, Any] = {}

    cid = str(data.get("id", "")).strip().lower()
    if not _ID_RE.match(cid):
        err["id"] = "Usar 1–32 caracteres: minúsculas, números, '-' o '_'"
    elif creating and cid in existing_ids:
        err["id"] = "Ya existe una cámara con ese id"
    out["id"] = cid

    name = str(data.get("name", "")).strip() or cid
    if len(name) > 60:
        err["name"] = "Máximo 60 caracteres"
    out["name"] = name

    out["enabled"] = bool(data.get("enabled", False))

    scheme = str(data.get("scheme", "http")).strip().lower()
    if scheme not in ("http", "https"):
        err["scheme"] = "Solo http o https"
    out["scheme"] = scheme

    host = str(data.get("host", "")).strip()
    # Permitir que peguen una URL completa: http://192.168.1.110:8080/video
    m = re.match(r"^(https?)://([^/:\s]+|\[[0-9a-fA-F:]+\])(?::(\d+))?(/.*)?$", host)
    if m:
        scheme = out["scheme"] = m.group(1)
        host = m.group(2).strip("[]")
        if m.group(3):
            data = {**data, "port": m.group(3)}
        if m.group(4) and m.group(4) != "/":
            data = {**data, "video_path": m.group(4)}
    if host:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if re.fullmatch(r"[0-9.]+", host) or not _HOST_RE.match(host):
                err["host"] = "IP o nombre de host inválido"
    elif out["enabled"]:
        err["host"] = "Obligatorio si la cámara está habilitada"
    out["host"] = host

    try:
        port = int(data.get("port", 8080))
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        err["port"] = "Puerto entre 1 y 65535"
        port = 8080
    out["port"] = port

    path = str(data.get("video_path", "/video")).strip() or "/video"
    if not path.startswith("/") or any(c.isspace() for c in path) or len(path) > 200:
        err["video_path"] = "Debe empezar con '/' y no tener espacios (ej. /video)"
    out["video_path"] = path

    out["username"] = str(data.get("username", ""))[:64]
    out["password"] = str(data.get("password", ""))[:64]
    out["notes"] = str(data.get("notes", ""))[:500]

    proc_in = data.get("processing") or {}
    proc = dict(DEFAULT_PROCESSING)
    limits = {"max_frame_age_ms": (10, 5000), "latency_offset_ms": (-2000, 2000), "target_fps": (1, 60)}
    for key, (lo, hi) in limits.items():
        if key in proc_in:
            try:
                v = int(proc_in[key])
                if not lo <= v <= hi:
                    raise ValueError
                proc[key] = v
            except (TypeError, ValueError):
                err[f"processing.{key}"] = f"Entero entre {lo} y {hi}"
    out["processing"] = proc

    if err:
        raise ValidationError(err)
    return out


# --------------------------------------------------------------------------- pista

def _validate_points(raw: Any, field: str, err: dict, lo: tuple[float, float], hi: tuple[float, float]) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_POINTS_PER_LAYER:
        err[field] = f"Lista de hasta {MAX_POINTS_PER_LAYER} puntos"
        return []
    pts = []
    for p in raw:
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError, KeyError):
            err[field] = "Cada punto debe ser [x, y] numérico"
            return []
        if not (lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1]):
            err[field] = "Hay puntos fuera del área"
            return []
        pts.append([round(x, 2), round(y, 2)])
    return pts


def _validate_layers(raw: Any, prefix: str, err: dict, w: float, h: float) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    margin_w, margin_h = w * 0.05, h * 0.05  # tolerancia pequeña para puntos sobre el borde
    lo, hi = (-margin_w, -margin_h), (w + margin_w, h + margin_h)
    raw = normalize_layers(raw)
    out = {}
    for name in LAYER_LINES:
        layer = raw.get(name) if isinstance(raw.get(name), dict) else {}
        out[name] = {
            "points": _validate_points(layer.get("points"), f"{prefix}.{name}", err, lo, hi),
            "closed": bool(layer.get("closed", True)),
        }
    for key, label in POLY_LISTS.items():
        items_in = raw.get(key) or []
        items = []
        if not isinstance(items_in, list) or len(items_in) > MAX_ZONES:
            err[f"{prefix}.{key}"] = f"Hasta {MAX_ZONES}"
            items_in = []
        for i, z in enumerate(items_in):
            if not isinstance(z, dict):
                err[f"{prefix}.{key}[{i}]"] = "Polígono inválido"
                continue
            pts = _validate_points(z.get("points"), f"{prefix}.{key}[{i}]", err, lo, hi)
            items.append({"name": str(z.get("name") or f"{label} {i + 1}")[:40], "points": pts})
        out[key] = items
    return out


def normalize_layers(raw: dict) -> dict:
    """Compatibilidad: el antiguo 'border_inner' (una sola isla) pasa a ser 'Isla 1'."""
    if not isinstance(raw, dict):
        return {}
    raw = dict(raw)
    old = raw.pop("border_inner", None)
    if not isinstance(raw.get("inner_islands"), list):
        raw["inner_islands"] = []
    if isinstance(old, dict) and old.get("points") and not raw["inner_islands"]:
        raw["inner_islands"] = [{"name": "Isla 1", "points": old["points"]}]
    raw.setdefault("exclusion_zones", [])
    return raw


def normalize_track(track: dict) -> dict:
    track["plan"] = normalize_layers(track.get("plan") or {})
    for cam in (track.get("cameras") or {}).values():
        if isinstance(cam, dict):
            cam["layers"] = normalize_layers(cam.get("layers") or {})
    track.setdefault("calibration", {"points": [], "homographies": {}})
    return track


def validate_track(data: dict, *, camera_ids: set[str]) -> dict:
    if not isinstance(data, dict):
        raise ValidationError({"_": "Se esperaba un objeto JSON"})
    err: dict[str, str] = {}
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "units": "mm"}

    def num(key, lo, hi, default):
        try:
            v = float(data.get(key, default))
            if not lo <= v <= hi:
                raise ValueError
            return v
        except (TypeError, ValueError):
            err[key] = f"Número entre {lo} y {hi}"
            return default

    out["width_mm"] = num("width_mm", 100, 20000, 2440)
    out["height_mm"] = num("height_mm", 100, 20000, 1220)
    out["grid_mm"] = num("grid_mm", 1, 1000, 50)
    out["plan"] = _validate_layers(data.get("plan"), "plan", err, out["width_mm"], out["height_mm"])

    cams_out = {}
    cams_in = data.get("cameras") or {}
    if not isinstance(cams_in, dict):
        err["cameras"] = "Formato inválido"
        cams_in = {}
    for cid, c in cams_in.items():
        if cid not in camera_ids or not isinstance(c, dict):
            continue  # se descartan dibujos de cámaras que ya no existen
        try:
            iw, ih = int(c.get("image_width", 0)), int(c.get("image_height", 0))
        except (TypeError, ValueError):
            iw = ih = 0
        if not (16 <= iw <= 10000 and 16 <= ih <= 10000):
            err[f"cameras.{cid}"] = "Falta el tamaño de imagen: capturá una imagen de referencia primero"
            continue
        cams_out[cid] = {
            "image_width": iw,
            "image_height": ih,
            "snapshot_taken_at": str(c.get("snapshot_taken_at", ""))[:40],
            "layers": _validate_layers(c.get("layers"), f"cameras.{cid}", err, iw, ih),
        }
    out["cameras"] = cams_out
    out["calibration"] = _validate_calibration(data.get("calibration"), camera_ids, cams_out,
                                               out["width_mm"], out["height_mm"], err)
    if err:
        raise ValidationError(err)
    return out


MAX_CALIB_POINTS = 60


def _validate_calibration(raw: Any, camera_ids: set[str], cams: dict, w: float, h: float, err: dict) -> dict:
    """Puntos de referencia: posición en el plano (mm) y en cada foto (px). Calcula las homografías."""
    raw = raw if isinstance(raw, dict) else {}
    pts_in = raw.get("points") or []
    if not isinstance(pts_in, list) or len(pts_in) > MAX_CALIB_POINTS:
        err["calibration.points"] = f"Hasta {MAX_CALIB_POINTS} puntos"
        pts_in = []
    points = []
    for i, p in enumerate(pts_in):
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or f"P{i + 1}")[:30]
        plan = None
        if p.get("plan") is not None:
            got = _validate_points([p.get("plan")], f"calibration.points[{i}].plan", err,
                                   (-0.05 * w, -0.05 * h), (1.05 * w, 1.05 * h))
            plan = got[0] if got else None
        px = {}
        for cid, uv in (p.get("px") or {}).items():
            if cid not in camera_ids or uv is None:
                continue
            cam = cams.get(cid)
            iw, ih = (cam["image_width"], cam["image_height"]) if cam else (10000, 10000)
            got = _validate_points([uv], f"calibration.points[{i}].px.{cid}", err, (0, 0), (iw, ih))
            if got:
                px[cid] = got[0]
        points.append({"name": name, "plan": plan, "px": px})
    homs = {}
    for cid in sorted(camera_ids):
        pairs = [(p["px"][cid], p["plan"]) for p in points if p["plan"] is not None and cid in p["px"]]
        entry: dict[str, Any] = {"n": len(pairs)}
        if len(pairs) >= 4 and cid in cams:
            try:
                H = hg.fit([a for a, _ in pairs], [b for _, b in pairs])
                res = hg.residuals(H, [a for a, _ in pairs], [b for _, b in pairs])
                pose = hg.camera_pose(H, cams[cid]["image_width"], cams[cid]["image_height"])
                if pose:
                    entry["camera"] = pose
                entry.update(H_img_to_plan=hg.flatten(H),
                             rms_mm=round((sum(r * r for r in res) / len(res)) ** 0.5, 2),
                             max_mm=round(max(res), 2),
                             image_width=cams[cid]["image_width"], image_height=cams[cid]["image_height"],
                             computed_at=_now())
            except hg.HomographyError as e:
                entry["error"] = str(e)
        elif len(pairs) < 4:
            entry["error"] = f"Faltan {4 - len(pairs)} punto(s) con posición en el plano y en esta cámara"
        homs[cid] = entry
    return {"points": points, "homographies": homs}


# --------------------------------------------------------------------------- store

class ConfigStore:
    """Acceso concurrente (hilos de Flask) a los archivos de configuración."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.cameras_path = self.root / "config" / "cameras.json"
        self.track_path = self.root / "data" / "track" / "track_layout.json"
        self.snapshots_dir = self.root / "data" / "track" / "snapshots"
        self.vehicles_path = self.root / "config" / "vehicles.json"
        self.backups_dir = self.root / "data" / "backups"
        self.presets_path = self.root / "config" / "track_presets.json"
        self.hardware_path = self.root / "config" / "hardware.json"
        self._lock = threading.RLock()

    # -- respaldos: se guarda la versión ANTERIOR justo antes de sobrescribir cada archivo
    def _kind_path(self, kind: str) -> Path:
        return {"cameras": self.cameras_path, "track": self.track_path, "vehicles": self.vehicles_path}[kind]

    def _backup(self, kind: str) -> None:
        """Copia el archivo actual (si existe) a data/backups/ antes de sobrescribirlo."""
        path = self._kind_path(kind)
        if not path.exists():
            return
        try:
            self.backups_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            shutil.copy2(path, self.backups_dir / f"{kind}_{stamp}.json")
            files = sorted(self.backups_dir.glob(f"{kind}_*.json"))
            for old in files[:-MAX_BACKUPS_PER_KIND]:
                old.unlink(missing_ok=True)
        except OSError:
            pass  # un respaldo fallido nunca debe impedir guardar

    def list_backups(self) -> list[dict]:
        if not self.backups_dir.exists():
            return []
        out = []
        for p in self.backups_dir.glob("*.json"):
            kind, _, _ = p.stem.partition("_")
            if kind not in BACKUP_KINDS:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"file": p.name, "kind": kind, "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                        "size": st.st_size})
        out.sort(key=lambda b: b["mtime"], reverse=True)
        return out

    def restore_backup(self, file: str) -> dict:
        """Restaura un respaldo. Antes, respalda también el estado actual (por si fue un error)."""
        if not re.fullmatch(r"(cameras|track|vehicles)_[0-9_]+\.json", file):
            raise ValueError("Nombre de respaldo inválido")
        src = self.backups_dir / file
        if not src.exists():
            raise FileNotFoundError(file)
        kind = file.split("_", 1)[0]
        with self._lock:
            self._backup(kind)  # por si hay que deshacer la restauración
            dest = self._kind_path(kind)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        return {"kind": kind}

    # -- plantillas propias del usuario (además de las predefinidas en TRACK_SIZE_PRESETS)
    def list_presets(self) -> list[dict]:
        try:
            with open(self.presets_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError):
            return []
        presets = data.get("presets", []) if isinstance(data, dict) else []
        return [p for p in presets if isinstance(p, dict)]

    def save_preset(self, data: dict) -> dict:
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("El nombre es obligatorio")
        if len(name) > 40:
            raise ValueError("El nombre no puede tener más de 40 caracteres")
        try:
            width_mm = float(data.get("width_mm"))
            height_mm = float(data.get("height_mm"))
        except (TypeError, ValueError):
            raise ValueError("Ancho/largo inválidos")
        if not (10 <= width_mm <= 20000 and 10 <= height_mm <= 20000):
            raise ValueError("Ancho/largo deben estar entre 10 y 20000 mm")
        with self._lock:
            presets = self.list_presets()
            if len(presets) >= MAX_USER_PRESETS:
                raise ValueError(f"Ya hay {MAX_USER_PRESETS} plantillas guardadas; borrá alguna antes de agregar otra")
            pid = "p_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            preset = {
                "id": pid,
                "name": name,
                "width_mm": width_mm,
                "height_mm": height_mm,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            presets.append(preset)
            self.presets_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self.presets_path, {"presets": presets})
        return preset

    # -- ESP32 + semáforo + RFID (ver validate_hardware)
    def get_hardware(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._load(self.hardware_path, default_hardware))

    def save_hardware(self, data: dict) -> dict:
        hw = validate_hardware(data)
        with self._lock:
            self.hardware_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self.hardware_path, hw)
        return hw

    def delete_preset(self, pid: str) -> None:
        with self._lock:
            presets = self.list_presets()
            kept = [p for p in presets if p.get("id") != pid]
            if len(kept) == len(presets):
                raise FileNotFoundError(pid)
            self.presets_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self.presets_path, {"presets": kept})

    # -- lectura tolerante: si el archivo no existe o está dañado se usa el valor por defecto
    def _load(self, path: Path, default_fn):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            data = default_fn()
            _atomic_write_json(path, data)
            return data
        except (json.JSONDecodeError, OSError) as e:
            backup = path.with_suffix(path.suffix + f".danado_{datetime.now():%Y%m%d_%H%M%S}")
            try:
                os.replace(path, backup)
            except OSError:
                pass
            data = default_fn()
            data["_aviso"] = f"El archivo estaba dañado ({e}); se guardó una copia en {backup.name}"
            _atomic_write_json(path, data)
            return data

    # cámaras -------------------------------------------------------------
    def cameras_doc(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._load(self.cameras_path, default_cameras))

    def list_cameras(self) -> list[dict]:
        return self.cameras_doc().get("cameras", [])

    def get_camera(self, cid: str) -> dict | None:
        return next((c for c in self.list_cameras() if c.get("id") == cid), None)

    def upsert_camera(self, data: dict, cid: str | None = None) -> dict:
        with self._lock:
            doc = self.cameras_doc()
            cams = doc.get("cameras", [])
            ids = {c["id"] for c in cams}
            creating = cid is None
            if not creating:
                if cid not in ids:
                    raise KeyError(cid)
                data = {**data, "id": cid}  # el id no se cambia al editar
            cam = validate_camera(data, existing_ids=ids, creating=creating)
            if creating:
                cams.append(cam)
            else:
                cams = [cam if c["id"] == cid else c for c in cams]
            doc["cameras"] = cams
            doc["updated_at"] = _now()
            doc.pop("_aviso", None)
            self._backup("cameras")
            _atomic_write_json(self.cameras_path, doc)
            return cam

    def delete_camera(self, cid: str) -> None:
        with self._lock:
            doc = self.cameras_doc()
            cams = [c for c in doc.get("cameras", []) if c["id"] != cid]
            if len(cams) == len(doc.get("cameras", [])):
                raise KeyError(cid)
            doc["cameras"], doc["updated_at"] = cams, _now()
            self._backup("cameras")
            _atomic_write_json(self.cameras_path, doc)
            track = self.get_track()
            if cid in track.get("cameras", {}):
                track["cameras"].pop(cid)
                track["updated_at"] = _now()
                self._backup("track")
                _atomic_write_json(self.track_path, track)
            snap = self.snapshot_path(cid)
            if snap.exists():
                snap.unlink()

    # pista ---------------------------------------------------------------
    def get_track(self) -> dict:
        with self._lock:
            return normalize_track(copy.deepcopy(self._load(self.track_path, default_track)))

    def save_track(self, data: dict) -> dict:
        with self._lock:
            ids = {c["id"] for c in self.list_cameras()}
            track = validate_track(data, camera_ids=ids)
            track["updated_at"] = _now()
            self._backup("track")
            _atomic_write_json(self.track_path, track)
            return track

    # vehículos -----------------------------------------------------------
    def get_vehicles(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._load(self.vehicles_path, default_vehicles))

    def save_vehicles(self, data: dict) -> tuple[dict, list[str]]:
        with self._lock:
            cfg, warnings = validate_vehicles(data)
            cfg["updated_at"] = _now()
            self._backup("vehicles")
            _atomic_write_json(self.vehicles_path, cfg)
            return cfg, warnings

    def snapshot_path(self, cid: str) -> Path:
        if not _ID_RE.match(cid):
            raise ValueError("id inválido")
        return self.snapshots_dir / f"{cid}.jpg"

    def save_snapshot(self, cid: str, jpeg: bytes, width: int, height: int) -> dict:
        """Guarda la imagen de referencia y registra su tamaño en la pista."""
        with self._lock:
            path = self.snapshot_path(cid)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(jpeg)
            os.replace(tmp, path)
            track = self.get_track()
            cams = track.setdefault("cameras", {})
            entry = cams.get(cid) or {"layers": _empty_layers()}
            old = (entry.get("image_width"), entry.get("image_height"))
            entry.update(image_width=width, image_height=height, snapshot_taken_at=_now())
            cams[cid] = entry
            size_changed = old != (None, None) and old != (width, height)
            if size_changed:
                # los píxeles de calibración de otra resolución ya no valen
                for p in track.get("calibration", {}).get("points", []):
                    (p.get("px") or {}).pop(cid, None)
                track.get("calibration", {}).get("homographies", {}).pop(cid, None)
            track["updated_at"] = _now()
            self._backup("track")
            _atomic_write_json(self.track_path, track)
            return {"image_width": width, "image_height": height,
                    "size_changed": size_changed,
                    "snapshot_taken_at": entry["snapshot_taken_at"]}

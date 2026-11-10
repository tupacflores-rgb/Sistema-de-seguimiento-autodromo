"""Cliente de control HTTP para la app Android "IP Webcam".

Rutas usadas (fuentes: plugin platypush / integración de Home Assistant, y el app.py previo
del usuario, que ya usaba /status.json, /ptz y /settings/video_size con éxito):
    GET  /status.json?show_avail=1      -> {"curvals": {...}, "avail": {...}}
    POST /settings/<clave>?set=<valor>  -> cambia un ajuste
    POST /ptz?zoom=<indice>             -> zoom (índice de la lista avail["zoom"])
    POST /enabletorch | /disabletorch   -> linterna
    POST /focus | /nofocus              -> enfoque automático puntual / liberar

No se asume ninguna lista fija de ajustes: la página muestra los que el teléfono informa en
"curvals" y "avail". Así funciona con distintas versiones de la app.
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

_KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")

# Ajustes cuyo cambio invalida la calibración (cambian la geometría de la imagen).
GEOMETRY_KEYS = {"video_size", "zoom", "orientation", "mirror_flip", "ffc", "video_crop", "photo_size"}


class IpWebcamError(RuntimeError):
    pass


class IpWebcamClient:
    def __init__(self, base_url: str, username: str = "", password: str = "", timeout: float = 4.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._headers = {"User-Agent": "multicam-vision-admin/0.1"}
        if username or password:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            self._headers["Authorization"] = f"Basic {token}"

    def _request(self, path: str, method: str = "GET") -> tuple[bytes, float]:
        req = urllib.request.Request(self.base_url + path, method=method, headers=self._headers)
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as e:
            hint = " (revisá usuario/contraseña en IP Webcam)" if e.code == 401 else ""
            raise IpWebcamError(f"HTTP {e.code} en {path}{hint}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise IpWebcamError(f"No se pudo conectar a {self.base_url}: {reason}") from e
        return body, (time.perf_counter() - t0) * 1000

    # ------------------------------------------------------------------ lectura
    def status(self) -> dict:
        body, ms = self._request("/status.json?show_avail=1")
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            raise IpWebcamError("La respuesta de /status.json no es JSON (¿es IP Webcam?)") from e
        if not isinstance(data, dict):
            raise IpWebcamError("Formato inesperado de /status.json")
        data.setdefault("curvals", {})
        data.setdefault("avail", {})
        data["_response_ms"] = round(ms, 1)
        return data

    def ping(self) -> dict:
        st = self.status()
        cur = st.get("curvals", {})
        return {"ok": True, "response_ms": st["_response_ms"],
                "video_size": cur.get("video_size"), "quality": cur.get("quality"),
                "zoom": cur.get("zoom"), "orientation": cur.get("orientation")}

    # ------------------------------------------------------------------ escritura
    def set_setting(self, key: str, value) -> str:
        if not _KEY_RE.match(str(key)):
            raise IpWebcamError(f"Clave inválida: {key!r}")
        val = urllib.parse.quote(str(value), safe="")
        body, _ = self._request(f"/settings/{key}?set={val}", method="POST")
        return body.decode("utf-8", "replace")[:500]

    def set_zoom_index(self, index: int) -> str:
        index = int(index)
        if not 0 <= index <= 1000:
            raise IpWebcamError("Índice de zoom fuera de rango")
        body, _ = self._request(f"/ptz?zoom={index}", method="POST")
        return body.decode("utf-8", "replace")[:500]

    def torch(self, on: bool) -> str:
        body, _ = self._request("/enabletorch" if on else "/disabletorch", method="POST")
        return body.decode("utf-8", "replace")[:500]

    def focus(self, on: bool) -> str:
        body, _ = self._request("/focus" if on else "/nofocus", method="POST")
        return body.decode("utf-8", "replace")[:500]

    def apply_recommended(self, preferred_sizes=("960x720", "640x480")) -> list[str]:
        """Zoom mínimo + resolución 4:3 (sensor completo). Mismo criterio que el app.py previo."""
        log: list[str] = []
        st = self.status()
        cur, avail = st.get("curvals", {}), st.get("avail", {})
        zooms = avail.get("zoom") or []
        if zooms:
            self.set_zoom_index(0)
            log.append(f"Zoom → mínimo ({zooms[0]})")
        else:
            log.append("El teléfono no informa niveles de zoom; sin cambios")
        sizes = avail.get("video_size") or []
        target = next((s for s in preferred_sizes if s in sizes), None)
        if target and cur.get("video_size") != target:
            self.set_setting("video_size", target)
            log.append(f"Resolución {cur.get('video_size')} → {target} (4:3, el stream se reinicia)")
        elif target:
            log.append(f"Resolución ya en {target}")
        else:
            log.append("No hay resoluciones 4:3 preferidas disponibles; sin cambios")
        return log

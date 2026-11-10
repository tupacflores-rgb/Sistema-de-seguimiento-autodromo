"""python -m admin [--host 127.0.0.1] [--port 5050] [--root .]"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


# ------------------------------------------------------------------ instancia anterior
# En Windows, un segundo servidor puede abrir el MISMO puerto mientras el primero sigue
# abierto (SO_REUSEADDR), y el navegador sigue hablando con el VIEJO: los cambios "no se ven".
# Por eso, antes de arrancar se busca y se cierra una página de administrador anterior.

def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _http(url: str, method: str = "GET", timeout: float = 2.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "mcv-admin-launcher"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(200_000)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, OSError):
        return 0, b""


def _windows_listener_pids(port: int) -> list[int]:
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() in ("LISTENING", "ESCUCHANDO") and re.search(rf":{port}$", parts[1]):
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    pids.discard(os.getpid())
    return sorted(pids)


def close_previous_instance(port: int) -> bool:
    """Devuelve True si el puerto quedó libre (o ya lo estaba)."""
    if not _port_in_use(port):
        return True
    base = f"http://127.0.0.1:{port}"
    code, body = _http(base + "/api/version")
    if code == 200:
        try:
            info = json.loads(body)
        except ValueError:
            info = {}
        print(f"  Había otra página de administrador abierta (versión {info.get('version', '?')}). Cerrándola…")
        _http(base + "/api/admin/shutdown", method="POST")
    else:
        # versión vieja (sin /api/version): confirmar que es esta página antes de cerrar nada
        code, body = _http(base + "/")
        if code != 200 or b"MultiCam Vision" not in body:
            print(f"  El puerto {port} está ocupado por OTRO programa. Usá otro puerto:  python -m admin --port 5051")
            return False
        print("  Había una versión ANTERIOR de la página abierta. Cerrándola…")
        if sys.platform == "win32":
            for pid in _windows_listener_pids(port):
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            print("  Cerrá la ventana/terminal anterior y volvé a iniciar.")
            return False
    for _ in range(50):
        if not _port_in_use(port):
            print("  Listo.\n")
            return True
        time.sleep(0.1)
    if sys.platform == "win32":  # último recurso
        for pid in _windows_listener_pids(port):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        time.sleep(1)
    if _port_in_use(port):
        print("  No se pudo cerrar la página anterior: cerrá su ventana negra a mano (o reiniciá la PC) y volvé a intentar.")
        return False
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Página de administrador MULTICAM VISION")
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 = solo esta PC; 0.0.0.0 = toda la red local (sin contraseña)")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                    help="Carpeta del proyecto (donde están config/ y data/)")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--open", action="store_true", help="Abrir el navegador cuando el servidor esté listo")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        from . import __version__
        from .server import create_app
    except ModuleNotFoundError as e:
        print(f"Falta una dependencia: {e.name}. Instalala con:  python -m pip install -r requirements.txt")
        return 1

    if not close_previous_instance(a.port):
        return 2

    app = create_app(Path(a.root))
    shown = "127.0.0.1" if a.host in ("0.0.0.0", "::") else a.host
    print(f"\n  MultiCam Vision · página de administrador v{__version__}")
    print(f"  Abrí: http://{shown}:{a.port}\n  (Ctrl+C o cerrar esta ventana para detenerla)\n")
    if a.host in ("0.0.0.0", "::"):
        print("  ATENCIÓN: accesible desde toda la red local y sin contraseña.\n")
    if a.open:
        import threading
        import webbrowser

        def _open_when_ready():
            for _ in range(100):
                if _port_in_use(a.port):
                    webbrowser.open(f"http://127.0.0.1:{a.port}/")
                    return
                time.sleep(0.1)
        threading.Thread(target=_open_when_ready, daemon=True).start()
    try:
        # threaded=True: cada stream de video ocupa un hilo mientras está abierto
        app.run(host=a.host, port=a.port, debug=False, threaded=True, use_reloader=False)
    finally:
        app.extensions["mcv_tracking"].stop()
        app.extensions["mcv_streams"].shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

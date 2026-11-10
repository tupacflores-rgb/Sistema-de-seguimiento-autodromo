"""
MULTICAM VISION - Paso 1 de la Primera Orden: inspección del entorno de desarrollo.

Uso (en la PC, desde esta carpeta):
    python 00_inspeccion_entorno.py
    python 00_inspeccion_entorno.py --cam1 http://192.168.1.100:8080/video --cam2 http://192.168.1.101:8080/video

No modifica nada del sistema. Escribe el resultado en logs/entorno_<fecha>.json
y lo imprime en pantalla. Las pruebas de cámara son opcionales y miden
SOLO lo que realmente llega por la red (no inventa valores).
"""
import argparse
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime


def version_modulo(nombre):
    paquetes = {"serial": "pyserial", "yaml": "PyYAML", "flask_socketio": "Flask-SocketIO"}
    try:
        importlib.import_module(nombre)
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version(paquetes.get(nombre, nombre))
        except PackageNotFoundError:
            return "instalado (version desconocida)"
    except Exception as e:  # noqa: BLE001 - queremos reportar cualquier fallo
        return f"NO DISPONIBLE ({type(e).__name__}: {e})"


def info_opencv():
    try:
        import cv2
    except Exception as e:  # noqa: BLE001
        return {"disponible": False, "error": str(e)}
    info = {
        "disponible": True,
        "version": cv2.__version__,
        "aruco": hasattr(cv2, "aruco"),
        "charuco": hasattr(cv2, "aruco") and hasattr(cv2.aruco, "CharucoDetector"),
        "cuda_devices": None,
    }
    try:
        info["cuda_devices"] = cv2.cuda.getCudaEnabledDeviceCount()
    except Exception:  # noqa: BLE001
        info["cuda_devices"] = "sin modulo cuda"
    try:
        build = cv2.getBuildInformation()
        lineas = [l.strip() for l in build.splitlines()
                  if any(k in l for k in ("FFMPEG", "Video I/O", "MSMF", "DirectShow", "GStreamer", "Parallel framework"))]
        info["build_video_io"] = lineas
    except Exception as e:  # noqa: BLE001
        info["build_video_io"] = f"error: {e}"
    info["threads"] = cv2.getNumThreads()
    return info


def info_ffmpeg():
    ruta = shutil.which("ffmpeg")
    if not ruta:
        return {"en_PATH": False}
    try:
        salida = subprocess.run([ruta, "-version"], capture_output=True, text=True, timeout=10).stdout
        return {"en_PATH": True, "ruta": ruta, "version": salida.splitlines()[0] if salida else "?"}
    except Exception as e:  # noqa: BLE001
        return {"en_PATH": True, "ruta": ruta, "error": str(e)}


def info_gpu():
    ruta = shutil.which("nvidia-smi")
    if not ruta:
        return "nvidia-smi no encontrado"
    try:
        return subprocess.run([ruta, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def info_puertos_serie():
    try:
        from serial.tools import list_ports
        return [f"{p.device} - {p.description}" for p in list_ports.comports()]
    except Exception as e:  # noqa: BLE001
        return f"pyserial no disponible ({e})"


def probar_camara(url, segundos=10):
    """Abre el stream y mide FPS de recepción y resolución reales."""
    import cv2
    r = {"url": url}
    t0 = time.perf_counter()
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    r["abrio"] = bool(cap.isOpened())
    r["t_apertura_s"] = round(time.perf_counter() - t0, 3)
    if not cap.isOpened():
        return r
    n, fallos, forma = 0, 0, None
    t_ini = time.perf_counter()
    while time.perf_counter() - t_ini < segundos:
        ok, f = cap.read()
        if ok:
            n += 1
            forma = f.shape
        else:
            fallos += 1
            if fallos > 50:
                break
    dur = time.perf_counter() - t_ini
    cap.release()
    r.update({"frames": n, "fallos": fallos, "duracion_s": round(dur, 2),
              "fps_recepcion": round(n / dur, 2) if dur else 0,
              "resolucion": f"{forma[1]}x{forma[0]}" if forma else None})
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam1", default=None)
    ap.add_argument("--cam2", default=None)
    ap.add_argument("--segundos", type=int, default=10)
    a = ap.parse_args()

    rep = {
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "sistema": platform.platform(),
        "maquina": platform.machine(),
        "cpu": platform.processor(),
        "cpus_logicas": os.cpu_count(),
        "python": sys.version,
        "python_ejecutable": sys.executable,
        "numpy": version_modulo("numpy"),
        "scipy": version_modulo("scipy"),
        "pyserial": version_modulo("serial"),
        "flask": version_modulo("flask"),
        "flask_socketio": version_modulo("flask_socketio"),
        "pytest": version_modulo("pytest"),
        "yaml": version_modulo("yaml"),
        "onnxruntime": version_modulo("onnxruntime"),
        "opencv": info_opencv(),
        "ffmpeg": info_ffmpeg(),
        "gpu": info_gpu(),
        "puertos_serie": info_puertos_serie(),
        "camaras": [],
    }
    for url in (a.cam1, a.cam2):
        if url:
            try:
                rep["camaras"].append(probar_camara(url, a.segundos))
            except Exception as e:  # noqa: BLE001
                rep["camaras"].append({"url": url, "error": str(e)})

    os.makedirs("logs", exist_ok=True)
    nombre = os.path.join("logs", f"entorno_{datetime.now():%Y%m%d_%H%M%S}.json")
    with open(nombre, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=2, ensure_ascii=False)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    print(f"\nInforme guardado en {nombre}")


if __name__ == "__main__":
    main()

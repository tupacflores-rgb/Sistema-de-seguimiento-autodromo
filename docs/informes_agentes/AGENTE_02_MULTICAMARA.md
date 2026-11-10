# AGENTE 02 — MULTICÁMARA · Propuesta técnica

> Fase: SOLO PROPUESTA. Lo que sigue es diseño + experimentos de laboratorio.
> **Todas las mediciones son en localhost (servidor MJPEG sintético en el mismo PC de la nube, 2 CPU), NO sobre la red Wi-Fi real ni con IP Webcam real.** Sirven para comparar mecanismos (buffers, timeouts, reconexión, sincronización), no para predecir la latencia absoluta en la pista.

Scripts y resultados crudos: `/tmp/claude-0/-home-claude/0ca3fc1e-d1b0-54d9-9f08-b03b5d26a0a9/scratchpad/agente02/`

---

## 1. Objetivo

Diseñar el subsistema `cameras/` para:

1. Captura simultánea e independiente de N cámaras (hoy 2) IP Webcam (Android, HTTP MJPEG), con URL, resolución y FPS configurables por cámara.
2. Que la caída de una cámara no detenga ni bloquee a las demás ni al pipeline.
3. Entregar siempre el frame **más reciente** (descartar obsoletos) con latencia acotada.
4. Asignar a cada frame un timestamp de captura **estimado** y comparable entre cámaras.
5. Sincronizar cámaras por timestamp con tolerancia.
6. Reconexión automática con backoff y estados de conexión observables.
7. Hacer las sesiones reproducibles (grabar/reproducir con timestamps originales).

---

## 2. Investigación realizada (con fuentes)

### 2.1 IP Webcam (Android) — endpoints

| Endpoint | Qué hace | Fuente / estado |
|---|---|---|
| `/video` | MJPEG `multipart/x-mixed-replace` | platypush, c3voc, ispyconnect; y el código del usuario ya lo usa |
| `/videofeed` | MJPEG (alias, usado por OBS/ffmpeg) | tiyn wiki, ispyconnect |
| `/shot.jpg` | último frame JPEG suelto | ispyconnect |
| `/photo.jpg` | foto a resolución de *foto* (lenta, enfoca) | platypush |
| `/status.json?show_avail=1` | estado y **valores disponibles** de cada setting | platypush (código del plugin) |
| `/settings/<clave>?set=<valor>` | cambia un setting (`quality`, `video_size`, `zoom`, `focusmode`, `whitebalance`, `night_vision`, `exposure_lock`, …) | platypush / Home Assistant (claves `KEY_MAP`) |
| `/settings/ptz?zoom=N` | zoom | platypush; el código del usuario usa `/ptz` |
| `/enabletorch`, `/disabletorch`, `/focus`, `/nofocus`, `/startvideo`, `/stopvideo` | control | platypush |
| RTSP H.264 (`rtsp://IP:8080/h264_ulaw.sdp`) | streaming H.264 | **NO confirmado** por ninguna fuente consultada. Conocimiento previo no verificado. → verificar en la pantalla de la app del J7 Prime. |

**Límite de FPS:** la app tiene un ajuste de límite de FPS en la UI, pero **no encontré fuente fiable del nombre de la clave HTTP**. Propuesta: no hardcodear; leer `/status.json?show_avail=1` en el teléfono real y mapear las claves que existan (herramienta de diagnóstico, ver §5).

**Timestamps:** el MJPEG multipart estándar no lleva timestamp de captura. No verifiqué si IP Webcam agrega alguna cabecera por parte (p. ej. `X-Timestamp`); el lector propuesto **registra las cabeceras de cada parte** para comprobarlo en el stream real (Pendiente).

### 2.2 OpenCV + FFmpeg sobre HTTP MJPEG

- `CAP_PROP_BUFFERSIZE`: la documentación histórica lo describe como soportado solo por algunos backends (DC1394); para FFmpeg el backend no lo implementa ([SO 30032945](https://kiwix.lema.org/stackoverflow.com_en_all_2023-11/a/30032945), [foro OpenCV](https://forum.opencv.org/t/cv-cap-prop-buffersize-when-playing-movie/7511)). **Medido aquí (OpenCV 5.0.0, CAP_FFMPEG): `cap.set(CAP_PROP_BUFFERSIZE, 1)` devuelve `False` y `get()` devuelve `-1`** → se ignora. El `BUFFERSIZE=1` de `app.py` no tiene efecto.
- `OPENCV_FFMPEG_CAPTURE_OPTIONS`: formato `clave;valor|clave;valor` (parseado con `av_dict_parse_string(..., ";", "|")`) ([foro OpenCV](https://forum.opencv.org/t/rtsp-ffmpeg-set-flags/14382)). En ese hilo `fflags;nobuffer|flags;low_delay` no resolvió la latencia RTSP; **aquí tampoco cambió nada** (ver §2.4).
- Latencia acumulada con `VideoCapture` sobre streams en vivo cuando el bucle procesa más lento que la cámara: problema ampliamente reportado ([foro OpenCV — drop frames](https://forum.opencv.org/t/videocapture-drop-frames/14428), [delay because of buffer](https://forum.opencv.org/t/delay-in-videocapture-because-of-buffer/2755), [SO 58293187](https://kaweah.freedombox.rocks/kiwix/stackoverflow.com_en_all_2023-11/questions/58293187/opencv-real-time-streaming-video-capture-is-slow-how-to-drop-frames-or-get-syn)). Solución estándar: hilo lector que siempre se queda con el último frame.
- OpenCV ≥4.5 expone `CAP_PROP_OPEN_TIMEOUT_MSEC` y `CAP_PROP_READ_TIMEOUT_MSEC` para FFmpeg (medido: funcionan, ver §2.5).

### 2.3 Banco de pruebas construido

- `mjpeg_server.py`: servidor `multipart/x-mixed-replace` a 30 fps (640×480, q80) con **un único "sensor"** (como una cámara). Cada frame lleva codificado en píxeles `seq` y `t_gen` (ms) → ground truth de latencia. Endpoints `/video`, `/videofeed`, `/shot.jpg`, `/ctl?stall=S` (deja de enviar sin cerrar socket = Wi-Fi colgado), `/ctl?drop=1` (cierra sockets). Opciones `--delay-ms/--jitter-ms` (latencia distinta por cámara) y un "LED" que parpadea según el reloj absoluto (visto por ambas cámaras).
- `capture_proto.py`: prototipo de `Frame`, `ConnState`, `ReconnectPolicy`, `CameraConfig`, `CameraSource` (backends `mjpeg` manual y `ffmpeg`), `FrameSynchronizer`.
- `exp_latency.py`, `exp_reconnect.py`, `exp_ffmpeg_drop.py`, `exp_sync.py`, `exp_cpu.py`; resultados en `res_*.json`.

### 2.4 Resultado 1 — latencia y acumulación de buffer (localhost, 20 s por caso)

P = tiempo de procesamiento simulado por frame en el bucle consumidor. Latencia = `time.time()` al obtener el frame − `t_gen` del frame.

| Método | P | FPS consumidos | Lat. mediana | Lat. p95 | Mediana primeros 3 s | Mediana últimos 3 s |
|---|---|---|---|---|---|---|
| A `VideoCapture(CAP_FFMPEG)` directo | 0 | 30.1 | 3 ms | 6 ms | 3 | 3 |
| E idem + `fflags;nobuffer\|flags;low_delay\|probesize;32\|analyzeduration;0` | 0 | 30.1 | 3 | 7 | 3 | 4 |
| B lector multipart manual directo | 0 | 30.1 | 3 | 7 | 3 | 3 |
| C hilo lector + último frame (manual) | 0 | 30.1 | 3 | 6.9 | 3 | 3 |
| D hilo lector + último frame (FFmpeg) | 0 | 30.1 | 3 | 5 | 3 | 3 |
| A FFmpeg directo | 50 ms | 18.8 | 3 742 | 7 064 | 536 | **6 886** |
| E FFmpeg directo + nobuffer | 50 ms | 18.9 | 3 702 | 7 042 | 547 | **6 861** |
| B manual directo | 50 ms | 19.3 | 3 593 | 6 782 | 542 | **6 614** |
| C hilo (manual) | 50 ms | 19.8 | **19** | 34 | 19 | 19 |
| D hilo (FFmpeg) | 50 ms | 19.8 | **20** | 35 | 18 | 21 |
| A FFmpeg directo | 100 ms | 9.8 | 6 733 | 12 746 | 996 | **12 457** |
| E FFmpeg + nobuffer | 100 ms | 9.7 | 6 725 | 12 772 | 974 | **12 469** |
| B manual directo | 100 ms | 9.8 | 6 737 | 12 747 | 1 010 | **12 452** |
| C hilo (manual) | 100 ms | 9.9 | **17** | 33 | 29 | 9 |
| D hilo (FFmpeg) | 100 ms | 9.9 | **20** | 33 | 12 | 19 |

Conclusiones medidas:
1. Si el consumidor es más lento que la cámara, **la latencia crece sin límite** (~0.35 s por segundo con P=50 ms; 12.5 s tras 20 s con P=100 ms) **con cualquier backend**, y no se pierde ningún frame (`seq_skipped = 0`): todos quedan encolados.
2. La cola no está en FFmpeg sino en los **buffers TCP del kernel** (aquí `tcp_rmem` máx. 32 MB): por eso `nobuffer/low_delay` y `BUFFERSIZE` no cambian nada. En la red/PC real el tamaño de esa cola dependerá del SO y del comportamiento de IP Webcam (si salta frames cuando el socket no drena). **No medido en real.**
3. Con un hilo lector que drena continuamente y conserva solo el último frame, la latencia queda acotada a ~1 período de frame (mediana 17–20 ms, p95 ≤ 35 ms: es la "edad" del último frame al momento de pedirlo, uniforme en [0, 33 ms]). Los frames intermedios se descartan y se cuentan (`dropped_overwrite` = 204 y 403 en 20 s).
4. Manual y FFmpeg empatan en latencia en localhost. Tiempo a primer frame: manual 3–9 ms, FFmpeg 31–37 ms (probing).

### 2.5 Resultado 2 — corte, cuelgue y reconexión (localhost)

Política usada en el test: backoff 0.25→0.5→1→2 s (máx 2 s, sin jitter), `connect_timeout=1 s`, `read_timeout=2 s`.

| Escenario | Backend | Detección | Recuperación |
|---|---|---|---|
| `drop` (servidor cierra socket, sigue vivo) | manual | 26–32 ms (EOF) | 20–27 ms |
| `drop` | FFmpeg | 27–32 ms (`read()`→False) | 15–30 ms |
| `kill` (proceso muerto 5 s, conexión rechazada) | manual | 2–6 ms | 0.76 s después de volver el server (= fase del backoff) |
| `kill` | FFmpeg | 4–10 ms | 0.76 s |
| `stall` 8 s (socket abierto, sin datos) | manual | **2.04 s** (= read_timeout) → STALLED | primer frame a 8.006 s (al terminar el stall) |
| `stall` 8 s | FFmpeg (`READ_TIMEOUT_MSEC=2000`) | 2.03 s | primer frame a 8.79 s (+0.79 s: los intentos de `open` bloquean) |

`VideoCapture` crudo:
- Tras matar el servidor, el **mismo objeto nunca se recupera** (5/5 `read()` = False después de relanzar el server) → hay que `release()` y crear uno nuevo.
- Stall **sin** `CAP_PROP_READ_TIMEOUT_MSEC`: `read()` quedó **bloqueado 30.05 s** (timeout interno por defecto). Con `READ_TIMEOUT_MSEC=2000`: 2.01 s.
- Anomalía: en 1 de 7 corridas de `drop` con backend FFmpeg el prototipo no registró la reconexión (frames siguieron llegando). No explicada; repetida 3 veces más sin reproducirse. Queda como riesgo a investigar.

### 2.6 Resultado 3 — dos cámaras, estimación de latencia con "LED" y sincronización

Cámara A: retardo 30 ms + U[0,10]; cámara B: 120 ms + U[0,30] (ambas 30 fps). Ambas ven el mismo "LED".
Estimación: `lat = t_rx(1er frame con LED encendido) − t_flanco − T/2`, mediana sobre flancos.

| Período LED | Cámara | Flancos | Lat. estimada | Lat. real (ground truth) | Error |
|---|---|---|---|---|---|
| 1000 ms (conmensurable con 30 fps) | A | 20 | 48.3 ms | 35 ms | **+13 ms** |
| 1000 ms | B | 20 | 113.3 ms | 135 ms | **−22 ms** |
| 1037 ms (no conmensurable) | A | 39 | 35.3 ms | 36 ms | −0.7 ms |
| 1037 ms | B | 39 | 135.3 ms | 136 ms | −0.7 ms |

Hallazgo práctico: si el período del LED es múltiplo del período de frame, la fase de muestreo queda fija y la corrección T/2 introduce un sesgo de hasta ±T/2. **El ESP32 debe parpadear con período no conmensurable o con intervalos pseudoaleatorios.** El desvío por flanco individual (IQR 15.5 ms) exige ≥30–40 flancos.

Sincronización (15 s por caso, LED 1037 ms):

| Estrategia | Desfase real mediano | p95 | máx |
|---|---|---|---|
| "Último frame de cada cámara" (ingenuo) | 99 ms | 132 ms | — |
| Sincronizador por timestamp más cercano, **sin** offsets (tol 20 ms) | 99 ms | 99 ms | 132 ms |
| Sincronizador **con** offsets estimados (tol 20 ms) | **1 ms** | **2 ms** | 35 ms |

Advertencia: el 1 ms es optimista porque los dos "sensores" sintéticos arrancaron casi en fase. Con cámaras reales independientes la fase relativa es arbitraria: **el desfase mínimo alcanzable por emparejamiento es |Δfase| ≤ T/2 = 16.7 ms a 30 fps** (más el jitter de red, no medido). Para un auto a velocidad v, el error de posición por desfase es v·Δt (p. ej. 1 m/s × 16.7 ms = 16.7 mm). Por eso la fusión entre cámaras debe usar `t_capture_est` dentro del tracker (predecir/interpolar al instante común), no confiar solo en "pares".

### 2.7 Resultado 4 — CPU (localhost, imagen sintética)

| Medida | Valor |
|---|---|
| `imdecode` 640×480 / reducido ½ | 2.15 ms / 0.69 ms |
| `imdecode` 960×720 / reducido ½ | 2.80 ms / 2.51 ms |
| `imdecode` 1920×1080 / reducido ½ | 7.11 ms / 4.14 ms |
| JPEG q80 (imagen ruidosa, peor caso) | 40 / 89 / 266 kB a 640×480 / 960×720 / 1080p |
| CPU de 1 stream 640×480@30 con decodificación | 3.7 % de un núcleo (manual), 3.4 % (FFmpeg) |

Ancho de banda estimado peor caso a 960×720@30 q80: 89 kB × 30 ≈ 21 Mbit/s por cámara (la escena real comprime mejor; medir). Con 2 cámaras en 2.4 GHz esto es crítico → bajar `quality` (50–70) y fijar FPS.

### 2.8 Red

- La IP del teléfono cambió (.103/.109/.110) → **reserva DHCP en el router por MAC**. Android 10+ usa MAC aleatoria por red: reservar esa MAC (estable por SSID) o desactivar "MAC aleatoria" para esa red.
- NTP en el teléfono no sirve de nada: el stream no lleva su reloj. La base de tiempo es **el reloj monotónico del PC** (`time.monotonic()`); `time.time()` solo para BD/logs.
- Desactivar optimización de batería para IP Webcam, mantener el teléfono cargando; preferir 5 GHz si el J7 Prime lo soporta (no verificado).

---

## 3. Solución propuesta

```
 IP Webcam A ──HTTP MJPEG──► [Hilo lector A] ──► slot "último" + anillo(8)
 IP Webcam B ──HTTP MJPEG──► [Hilo lector B] ──► slot "último" + anillo(8)
                                   │ estados/eventos             │
                                   ▼                             ▼
                           CameraManager ───────────► FrameSynchronizer.get()
                                                         │  FrameSet(t_ref, {A: Frame|None, B: Frame|None})
                                                         ▼
                                                 detección / tracking (no bloquean a los lectores)
```

1. **Un hilo por cámara** (no procesos): `imdecode` y `recv` liberan el GIL; medido 3–4 % de núcleo por stream 640×480@30. El hilo **nunca** espera al consumidor; sobreescribe el slot y cuenta descartes.
2. **Backend por defecto: lector multipart manual** (socket + parser + `cv2.imdecode`). Razones: (a) timeouts de conexión/lectura explícitos y fiables; (b) acceso a los **bytes JPEG crudos** → grabación sin recompresión para reproducibilidad; (c) acceso a las **cabeceras por parte** (para descubrir si IP Webcam manda timestamp); (d) decodificación opcional reducida (`IMREAD_REDUCED_COLOR_2`); (e) primer frame en ~5 ms vs ~35 ms. **Backend FFmpeg** como alternativa (y para RTSP si se confirma), siempre con `OPEN/READ_TIMEOUT_MSEC` y recreando el objeto en cada reconexión. **No usar `CAP_PROP_BUFFERSIZE`** (no tiene efecto).
3. **Backend `/shot.jpg` (polling)** como modo diagnóstico/fallback: cada petición trae un frame fresco por construcción (sin colas), a costa de FPS y de una petición HTTP por frame. Pendiente medir en real.
4. **Timestamps:** `t_received = time.monotonic()` al terminar de recibir el JPEG; `t_capture_est = t_received − latency_offset_s[cam]`. El offset se calibra con un LED del ESP32 (período no conmensurable, ≥40 flancos) visto por ambas cámaras o con un cronómetro en pantalla. Lo crítico para fusión es el **offset relativo** entre cámaras (los sesgos comunes — p. ej. retardo del monitor — se cancelan); el absoluto importa para correlacionar con eventos RFID. Opcional (Pendiente, solo si el jitter medido en real > ~10 ms): suavizado del timestamp por envolvente inferior (`t_cap_k ≈ min_i(t_rx_i + (k−i)·T)`), ojo con frames perdidos en el teléfono.
5. **Frames obsoletos:** el sincronizador ignora frames con `now − t_capture_est > max_age_s` (default 0.25 s); el consumidor nunca procesa frames viejos acumulados.
6. **Sincronización:** `FrameSynchronizer` empareja por `t_capture_est` más cercano a `t_ref = min(último t_capture_est de cada cámara viva)`, con tolerancia (default 20 ms ≈ 0.6 T) y devuelve `None` para cámaras sin frame válido → el pipeline sigue con una sola cámara.
7. **Reconexión:** máquina de estados + backoff exponencial con jitter, reset al primer frame. Watchdog de stall: `read_timeout_s` (socket) + "sin frames decodificables durante `stall_timeout_s`".
8. **Reproducibilidad:** `SessionRecorder` guarda JPEG crudos + CSV/JSONL (`cam_id, seq, t_received, t_capture_est, t_wall, session, bytes`) y `ReplaySource` (misma interfaz que `CameraSource`) reproduce con los tiempos originales → todos los demás módulos se prueban offline con datos reales.

### Estados de conexión

```
IDLE ─start()─► CONNECTING ─1er frame─► STREAMING
                   │  ▲                     │ EOF/RST/HTTP≠200 ─► BACKOFF
                   │  └──── espera backoff ─┤ read_timeout / sin frames ─► STALLED ─► BACKOFF
                   └─ error/timeout de conexión ─► BACKOFF
cualquiera ─stop()─► STOPPED
```

### Política de reconexión

`delay(n) = min(max_s, initial_s · factor^n) · (1 ± jitter)`; defaults propuestos `initial_s=0.5, factor=2, max_s=5, jitter=0.2`; `n` vuelve a 0 al recibir el primer frame de una sesión. Peor caso de recuperación después de que la cámara vuelve: `≈ max_s + connect_time` (medido 0.76 s con `max_s=2`). El jitter evita que dos cámaras reintenten en fase. Cada cambio de estado emite un evento (`integration/events.py`) con timestamp y motivo.

---

## 4. Tecnologías utilizadas

Python 3.13 (`threading`, `socket`, `dataclasses`, `enum`), OpenCV 5.0.0 (`imdecode`, `VideoCapture` CAP_FFMPEG), NumPy. Sin dependencias nuevas (ni `requests`: el socket crudo da control total de timeouts). Pruebas con `pytest` + servidor MJPEG sintético.

---

## 5. Archivos que necesita crear

| Archivo | Contenido |
|---|---|
| `multicam_vision/cameras/capture.py` | `Frame`, `ConnState`, `ReconnectPolicy`, `CameraStats`, `FrameGrabber` (protocolo), `MjpegHttpGrabber`, `FfmpegGrabber`, `ShotJpegGrabber`, `CameraSource` |
| `multicam_vision/cameras/manager.py` *(nuevo, justificado: separa ciclo de vida de N cámaras del sincronizador)* | `CameraManager` |
| `multicam_vision/cameras/synchronization.py` | `FrameSet`, `FrameSynchronizer`, `LatencyCalibrator` |
| `multicam_vision/cameras/ipwebcam.py` *(nuevo: control REST separado de la captura)* | `IpWebcamClient` (`status`, `set_setting`, `set_zoom`, `available_settings`) |
| `multicam_vision/cameras/recorder.py` *(nuevo: reproducibilidad)* | `SessionRecorder`, `ReplaySource` |
| `multicam_vision/config/cameras.py` | `CameraConfig` por cámara (URL, backend, resolución, quality, fps_limit, offset, timeouts) — cargado desde YAML/JSON |
| `multicam_vision/tools/diag_stream.py` | diagnóstico en la red real: cabeceras por parte, FPS, jitter de llegada, kB/frame, `/status.json?show_avail=1` |
| `multicam_vision/tools/calibrate_latency.py` | calibración LED/cronómetro → escribe `latency_offset_s` |
| `multicam_vision/tests/fixtures/mjpeg_server.py` | servidor sintético (portar `agente02/mjpeg_server.py`) |
| `multicam_vision/tests/test_capture.py`, `test_reconnect.py`, `test_sync.py` | pruebas automáticas (§8) |

---

## 6. Interfaces que necesita (firmas concretas)

```python
# cameras/capture.py
from __future__ import annotations
import enum
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol
import numpy as np

@dataclass(frozen=True, slots=True)
class Frame:
    cam_id: str
    seq: int                 # contador local por cámara, monótono, sobrevive reconexiones
    t_capture_est: float     # [s] reloj monotónico del PC: t_received - latency_offset_s
    t_received: float        # [s] time.monotonic() al completar la recepción del JPEG
    image: np.ndarray        # BGR uint8 (o None si decode=False)
    t_wall: float = 0.0      # time.time() al recibir (solo logs/BD)
    session: int = 0         # nº de conexión; cambia en cada reconexión
    jpeg: bytes | None = None   # bytes crudos (si record_raw=True)
    headers: dict[str, str] = field(default_factory=dict)  # cabeceras de la parte multipart

    def age(self, now: float) -> float: ...

class ConnState(enum.Enum):
    IDLE = "idle"; CONNECTING = "connecting"; STREAMING = "streaming"
    STALLED = "stalled"; BACKOFF = "backoff"; STOPPED = "stopped"

@dataclass(frozen=True)
class ReconnectPolicy:
    initial_s: float = 0.5
    factor: float = 2.0
    max_s: float = 5.0
    jitter: float = 0.2
    def delay(self, attempt: int) -> float: ...

@dataclass
class CameraConfig:                       # vive en config/cameras.py
    cam_id: str
    url: str                              # "http://192.168.1.110:8080/video"
    backend: str = "mjpeg"                # "mjpeg" | "ffmpeg" | "shot"
    width: int | None = None              # se aplica vía IpWebcamClient (/settings/video_size)
    height: int | None = None
    jpeg_quality: int | None = None       # /settings/quality
    fps_limit: float | None = None        # clave a confirmar con /status.json?show_avail=1
    connect_timeout_s: float = 2.0
    read_timeout_s: float = 2.0
    stall_timeout_s: float = 1.0          # sin frames decodificables
    latency_offset_s: float = 0.0         # calibrado
    ring_size: int = 8
    decode_reduce: int = 1                # 1, 2, 4, 8 -> IMREAD_REDUCED_COLOR_N
    record_raw: bool = False
    reconnect: ReconnectPolicy = field(default_factory=ReconnectPolicy)

@dataclass
class CameraStats:
    frames: int = 0
    dropped_overwrite: int = 0            # frames reemplazados sin ser consumidos
    decode_errors: int = 0
    reconnects: int = 0
    fps_in: float = 0.0                   # EMA de llegada
    interarrival_jitter_ms: float = 0.0   # desvío del intervalo entre llegadas
    kbytes_per_frame: float = 0.0
    last_error: str = ""
    last_frame_t: float | None = None

class FrameGrabber(Protocol):            # un backend: abre, lee un frame, cierra (bloqueante)
    def open(self) -> None: ...
    def read(self) -> tuple[bytes | None, np.ndarray | None, dict[str, str]]: ...  # raise ConnectionError/TimeoutError
    def close(self) -> None: ...

class MjpegHttpGrabber:                   # socket crudo + parser multipart con estado
    def __init__(self, url: str, connect_timeout_s: float, read_timeout_s: float,
                 decode_reduce: int = 1): ...
class FfmpegGrabber:                      # cv2.VideoCapture(url, CAP_FFMPEG, [OPEN/READ_TIMEOUT_MSEC])
    def __init__(self, url: str, connect_timeout_s: float, read_timeout_s: float): ...
class ShotJpegGrabber:                    # polling de /shot.jpg
    def __init__(self, url: str, timeout_s: float, period_s: float): ...

StateCallback = Callable[[str, ConnState, str], None]   # (cam_id, estado, motivo)

class CameraSource:
    def __init__(self, cfg: CameraConfig, on_state: StateCallback | None = None,
                 clock: Callable[[], float] = time.monotonic): ...
    def start(self) -> None: ...
    def stop(self, timeout: float = 3.0) -> None: ...
    @property
    def state(self) -> ConnState: ...
    @property
    def stats(self) -> CameraStats: ...          # copia
    def latest(self) -> Frame | None: ...        # no bloquea
    def wait_next(self, timeout: float = 1.0) -> Frame | None: ...  # frame más nuevo que el último entregado
    def recent(self) -> list[Frame]: ...         # copia del anillo (para el sincronizador)
    def set_latency_offset(self, seconds: float) -> None: ...

# cameras/manager.py
class CameraManager:
    def __init__(self, configs: list[CameraConfig], on_state: StateCallback | None = None): ...
    def start_all(self) -> None: ...
    def stop_all(self) -> None: ...
    def sources(self) -> dict[str, CameraSource]: ...
    def health(self) -> dict[str, tuple[ConnState, CameraStats]]: ...

# cameras/synchronization.py
@dataclass(frozen=True, slots=True)
class FrameSet:
    t_ref: float                          # instante común (monotónico PC)
    frames: dict[str, Frame | None]       # None = sin frame dentro de tolerancia / cámara caída
    max_skew_s: float                     # max-min de t_capture_est entre frames presentes
    def complete(self) -> bool: ...

class FrameSynchronizer:
    def __init__(self, sources: dict[str, CameraSource], tolerance_s: float = 0.020,
                 max_age_s: float = 0.25, require: set[str] | None = None): ...
    def get(self, now: float | None = None) -> FrameSet: ...
    def wait(self, timeout: float = 0.1) -> FrameSet | None: ...   # espera un FrameSet con algún frame nuevo

class LatencyCalibrator:                  # LED del ESP32 / cronómetro en pantalla
    def __init__(self, cam_ids: list[str], roi: dict[str, tuple[int, int, int, int]],
                 frame_period_s: float, threshold: float | None = None): ...
    def feed(self, frame: Frame) -> None: ...
    def add_reference_edge(self, t_edge_mono: float) -> None: ...  # si el ESP32 informa el flanco
    def estimate(self) -> dict[str, tuple[float, float, int]]: ... # cam -> (offset_s, iqr_s, n_flancos)
    def relative_offsets(self, ref_cam: str) -> dict[str, float]: ...

# cameras/ipwebcam.py
class IpWebcamClient:
    def __init__(self, base_url: str, timeout_s: float = 2.0): ...
    def status(self, show_avail: bool = True) -> dict: ...
    def set_setting(self, key: str, value: str | int) -> bool: ...
    def set_zoom(self, zoom: int) -> bool: ...
    def apply(self, cfg: CameraConfig) -> dict[str, bool]: ...    # resolución/quality/fps si existen

# cameras/recorder.py
class SessionRecorder:
    def __init__(self, out_dir: str): ...
    def write(self, frame: Frame) -> None: ...      # JPEG crudo + línea JSONL; en hilo propio, cola acotada
    def close(self) -> None: ...
class ReplaySource:                                 # misma API pública que CameraSource
    def __init__(self, session_dir: str, cam_id: str, speed: float = 1.0): ...
```

---

## 7. Riesgos y limitaciones

1. **Todo lo medido es localhost.** Latencia de red Wi-Fi, jitter, pérdida de paquetes, comportamiento de IP Webcam ante cliente lento (¿salta frames o encola?) y latencia interna del teléfono (cámara+encoder) son **desconocidos**.
2. Sin timestamp de captura del teléfono: `t_capture_est` hereda el jitter de red; el offset puede derivar si cambia la carga del teléfono (temperatura, batería) → recalibrar por sesión y monitorear `interarrival_jitter_ms`.
3. Fase entre cámaras no controlable: error residual hasta T/2 (16.7 ms a 30 fps) más jitter. El tracker debe modelar tiempo continuo.
4. Ancho de banda: 2 cámaras a 960×720@30 pueden saturar 2.4 GHz (peor caso estimado ~21 Mbit/s c/u).
5. El modo 16:9 de IP Webcam recorta el sensor 4:3: cambiar resolución invalida la calibración intrínseca (coordinar con Agente de calibración: fijar resolución en config y verificarla al conectar con `/status.json`).
6. Anomalía no explicada (1/7) en detección de `drop` con backend FFmpeg.
7. Parser multipart sin `Content-Length` es solo prototipo (falta parser con estado robusto).
8. En Windows la PC del usuario puede tener otro build de OpenCV/FFmpeg: repetir las pruebas allí.
9. RTSP de IP Webcam y la clave HTTP de límite de FPS: no confirmados.

---

## 8. Pruebas necesarias

Automáticas (servidor sintético, CI):
- `test_latency_bounded`: consumidor con P=100 ms durante 20 s → latencia p95 < 2 períodos de frame (hoy medido 33 ms).
- `test_drop_reconnect`, `test_kill_reconnect` (recuperación ≤ `max_s` + 0.5 s), `test_stall_detect` (detección ≤ `read_timeout_s` + 0.2 s), `test_one_camera_down_other_continues` (FPS de la cámara viva no cae > 5 %).
- `test_sync_offsets`: dos servidores con retardos distintos → con offsets calibrados, desfase real mediano ≤ T/2.
- `test_latency_calibrator`: LED 1037 ms → error de offset ≤ 5 ms con ≥ 30 flancos; regresión con LED 1000 ms debe mostrar el sesgo.
- `test_replay_equivalence`: grabar 10 s y reproducir → mismas `seq`/tiempos.

Reales (PC del usuario + J7 Prime + 2ª cámara), Pendientes:
- `diag_stream.py` por cámara 5 min: FPS real, jitter de llegada, kB/frame, cabeceras por parte, claves de `/status.json?show_avail=1`, a 640×480 y 960×720, quality 50/70/90.
- Prueba de cliente lento contra IP Webcam real (¿encola o salta?).
- Latencia absoluta y relativa con LED del ESP32 (período no conmensurable o aleatorio) y con cronómetro en pantalla; repetir 3 veces y a lo largo de 30 min (deriva).
- Desconexión real: apagar Wi-Fi del teléfono, cerrar la app, alejar el teléfono (stall), reiniciar router.
- 2 cámaras simultáneas + detección corriendo: CPU, FPS y latencia en el Ryzen 7 5700X (Windows y Linux).

---

## 9. Estado del trabajo

| Ítem | Estado |
|---|---|
| Investigación de endpoints IP Webcam | Parcial (RTSP y clave de FPS sin confirmar) |
| Servidor MJPEG sintético con ground truth (seq, t_gen, LED, stall/drop, retardo) | Implementado y Probado (localhost) |
| Prototipo `CameraSource` (manual + FFmpeg), estados, backoff | Implementado como prototipo; Probado en localhost |
| Medición latencia/acumulación de buffer | Probado (localhost) |
| Medición corte/cuelgue/reconexión | Probado (localhost); 1 anomalía sin explicar |
| Calibración de latencia por LED + sincronizador | Probado con datos sintéticos (localhost) |
| Módulo final `cameras/` (capture, manager, synchronization, ipwebcam, recorder) | Pendiente |
| Parser multipart robusto sin Content-Length | Pendiente |
| `ShotJpegGrabber`, `ReplaySource`, `SessionRecorder` | Pendiente |
| Cualquier medición en la red Wi-Fi real / IP Webcam real / PC del usuario | Pendiente |

---

## Fuentes consultadas

- [platypush — plugin camera.android.ipcam (docs)](https://docs.platypush.tech/platypush/plugins/camera.android.ipcam.html)
- [platypush — commit con rutas HTTP de IP Webcam (status.json, settings, enabletorch…)](https://git.platypush.tech/fabaff/platypush/commit/66d00ee428362e66f032ca8b0a420a04955ca43a)
- [Home Assistant — componente android_ip_webcam (claves de settings)](https://git.jeena.net/jeena/home-assistant/src/commit/d732a7e670975fa71d595eff9476787ed3539869/homeassistant/components/android_ip_webcam.py)
- [iSpy — URLs de IP Webcam (Android)](https://www.ispyconnect.com/fr/camera/ip-webcam-%28android%29)
- [tiyn wiki — IP Webcam /videofeed](https://git.tiyn.eu/tiyn/wiki/src/commit/008233103aa1ef34cea53a3490e569b800f6be47/wiki/android/ip_webcam.md)
- [c3voc — Android camera en OBS](https://c3voc.de/wiki/howto:android-camera-obs)
- [StackOverflow 30032945 — CAP_PROP_BUFFERSIZE](https://kiwix.lema.org/stackoverflow.com_en_all_2023-11/a/30032945)
- [Foro OpenCV — CAP_PROP_BUFFERSIZE when playing movie](https://forum.opencv.org/t/cv-cap-prop-buffersize-when-playing-movie/7511)
- [Foro OpenCV — RTSP ffmpeg set flags (OPENCV_FFMPEG_CAPTURE_OPTIONS)](https://forum.opencv.org/t/rtsp-ffmpeg-set-flags/14382)
- [Foro OpenCV — VideoCapture, drop frames](https://forum.opencv.org/t/videocapture-drop-frames/14428)
- [Foro OpenCV — Delay in VideoCapture because of buffer](https://forum.opencv.org/t/delay-in-videocapture-because-of-buffer/2755)
- [StackOverflow 58293187 — real time streaming is slow, drop frames](https://kaweah.freedombox.rocks/kiwix/stackoverflow.com_en_all_2023-11/questions/58293187/opencv-real-time-streaming-video-capture-is-slow-how-to-drop-frames-or-get-syn)

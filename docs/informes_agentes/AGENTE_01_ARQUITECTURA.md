# AGENTE 01 — ARQUITECTURA · MULTICAM VISION (Autódromo 1:24)

Fase: **propuesta técnica**. No se implementó el sistema. Lo único que se ejecutó fueron micro-benchmarks desechables (en `scratchpad/bench/`) para fundamentar las decisiones de concurrencia.

---

## 1. Objetivo

Definir una arquitectura modular, testeable y reproducible para:
- capturar 2 o más cámaras IP MJPEG de forma independiente y tolerante a fallos;
- llevar las detecciones de cada cámara a un **sistema de coordenadas común en mm sobre el plano de la pista**;
- hacer tracking multicámara en ese espacio común, sin duplicar autos en la zona compartida;
- recibir los eventos RFID del ESP32 sin bloquear nunca el video;
- calcular el ranking 1°/2°/3° y persistirlo en SQLite, con un dashboard Flask.

Todo con interfaces explícitas que permitan a cada agente trabajar en paralelo sin dependencias circulares.

---

## 2. Investigación realizada

### 2.1 Fuentes consultadas
- OpenCV y threading/GIL: [opencv-python and multi-threading (OpenCV Q&A)](https://answers.opencv.org/question/216815/opencv-python-and-multi-threading), [Connect several cameras OpenCV python](https://answers.opencv.org/question/233966/connect-several-cameras-opencv-python), [Viewing multiple USB or IP cameras simultaneously (forum.opencv.org)](https://forum.opencv.org/t/viewing-multiple-usb-or-ip-cameras-simultaneously-with-opencv/4223), [VideoCapture returns old frame after long periods using 3 cameras](https://forum.opencv.org/t/videocapture-get-returns-old-frame-after-long-periods-of-time-using-3-cameras/4896/5), [A multi-threading approach for faster OpenCV video streaming](https://sihabsahariar.medium.com/a-multi-threading-approach-for-faster-opencv-video-streaming-in-python-7324f11dbd2f).
- Buffer y latencia en streams IP (patrón "último frame"): [Prevent Frame Buffering with OpenCV (Atomic Object)](https://spin.atomicobject.com/frame-buffering-opencv/), [IP camera capture delay/lag (OpenCV Q&A)](https://answers.opencv.org/question/102803/ip-camera-capture-delaylag/), [Skipping all but the latest frame in VideoCapture](https://answers.opencv.org/question/81702/skipping-all-but-the-latest-frame-in-videocapture), [Time delay in VideoCapture due to capture buffer](https://answers.opencv.org/question/74255/time-delay-in-videocapture-opencv-due-to-capture-buffer), [OpenCV real time streaming is slow, how to drop frames](https://kaweah.freedombox.rocks/kiwix/stackoverflow.com_en_all_2023-11/questions/58293187/opencv-real-time-streaming-video-capture-is-slow-how-to-drop-frames-or-get-syn).
- Multiprocessing, spawn y memoria compartida: [multiprocessing.shared_memory (docs.python.org)](https://docs.python.org/3/library/multiprocessing.shared_memory.html), [Python's multiprocessing performance problem](https://pycoders.com/link/10434/web), [Python multiprocessing large object on Windows](https://www.technetexperts.com/python-multiprocessing-large-object-windows/amp/).

Conclusiones de las fuentes (coinciden con lo que medí, ver 2.2):
1. Las funciones C++ de OpenCV liberan el GIL mientras corren. Con hilos de Python se obtiene paralelismo real si el trabajo pesado está en cv2 o NumPy.
2. `VideoCapture` sobre streams de red acumula buffer interno y entrega frames viejos si el consumidor es más lento que el productor. `CAP_PROP_BUFFERSIZE` no siempre se respeta con el backend FFmpeg. El patrón recomendado es un **hilo dedicado que lee continuamente y guarda solo el último frame**.
3. En Windows, multiprocessing usa `spawn`: el proceso hijo re-importa el módulo principal, los argumentos tienen que ser picklables y el arranque es lento. Para pasar frames entre procesos sin pickle hace falta `shared_memory` con un protocolo de sincronización propio.

### 2.2 Micro-benchmarks medidos (workspace en la nube: 2 vCPU, Python 3.13.18, OpenCV 5.0.0, NumPy 2.5.3, `cv2.setNumThreads(1)`)
**Son medidas reales de este entorno, no de la PC del usuario.** El Ryzen 5700X debería dar tiempos iguales o menores por núcleo, pero no está verificado. Frame sintético BGR de 960x720; JPEG calidad 80, 78 408 bytes.

| Operación | Mediana | p95 |
|---|---|---|
| `np.copy` frame 960x720x3 (2,07 MB) | 0,14–0,22 ms | 0,18–0,33 ms |
| `queue.Queue` put+get (referencia, entre hilos) | 0,001 ms | 0,001 ms |
| pickle dumps+loads (protocol 5) | 0,30–0,50 ms | 0,38–0,61 ms |
| `mp.Queue` (spawn) frame crudo + ack | **1,89 ms** | 2,76 ms |
| `shared_memory`: escribir frame + señal + ack | **0,25 ms** | 0,29 ms |
| `mp.Queue` (spawn) JPEG crudo (78 KB) + ack | 0,09 ms | 0,12 ms |
| Arranque de proceso spawn + primer roundtrip | 221 ms | — |
| `cv2.imdecode` JPEG 960x720 | 2,20 ms | 3,12 ms |
| `cvtColor` BGR→GRAY 960x720 | 0,25 ms | 0,30 ms |
| `resize` a 480x360 (INTER_AREA) | 0,27 ms | 0,35 ms |
| `MOG2.apply` 960x720 | **17,7 ms** | 20,7 ms |
| `MOG2.apply` 480x360 | 2,05 ms | 2,58 ms |
| `ArucoDetector.detectMarkers` gris 960x720 (sin marcadores) | 7,4 ms | 8,8 ms |
| `perspectiveTransform` de 10 puntos | 0,001 ms | 0,001 ms |

Prueba del GIL, mismo trabajo total repartido en 2 hilos (2 vCPU):

| Carga | Serial | 2 hilos | Speedup |
|---|---|---|---|
| `cv2.imdecode` ×300 | 592 ms | 321 ms | **1,85×** |
| `MOG2.apply` 480x360 ×400 | 1231 ms | 653 ms | **1,88×** |
| ArUco 960x720 ×80 | 184 ms | 108 ms | **1,71×** |
| Python puro (control) | 98 ms | 103 ms | 0,95× |

Observación adicional: en el benchmark con spawn, el código a nivel de módulo **se re-ejecutó en cada proceso hijo** (se ve en la salida duplicada). Es exactamente la trampa de Windows: todo `main.py` tiene que estar protegido con `if __name__ == "__main__":` y ningún módulo debe tener efectos secundarios al importarse.

Conclusiones:
- Con hilos, cv2 escala casi linealmente. El Python puro no escala: hay que mantenerlo fuera de los bucles por píxel, y nunca hacer bucles Python sobre píxeles.
- Pasar un frame crudo con `mp.Queue` cuesta ~1,9 ms por frame y por salto. Es tolerable pero inútil si no hace falta. `shared_memory` lo reduce a ~0,25 ms, a cambio de complejidad (ring buffer, ciclo de vida, `unlink`).
- **El costo dominante no es el transporte sino el procesamiento.** MOG2 a resolución completa (17,7 ms) consume más de la mitad del presupuesto de 33 ms por frame a 30 FPS. La sustracción de fondo debe hacerse a resolución reducida o sobre una ROI (esto lo decide el agente de Detección; la arquitectura lo permite con un `scale` por cámara).

---

## 3. Solución propuesta

### 3.1 Principios
1. **Todo en el espacio del mundo (mm).** Las cámaras producen observaciones en coordenadas de pista. El tracking nunca razona en píxeles de varias cámaras a la vez. Así la deduplicación en la zona compartida queda como un problema de distancia en mm.
2. **Nunca bloquear al productor.** Captura → último-frame (sobrescribe). Etapas posteriores → colas acotadas con descarte del más viejo y contador de descartes. RFID y BD van en hilos propios.
3. **Capas estrictas sin ciclos** (ver 3.4), verificadas por un test automático.
4. **Reproducibilidad:** todo el pipeline debe poder correr desde una grabación (video + sidecar de timestamps) con reloj simulado y obtener la misma salida. Es la base de las pruebas de precisión.
5. **Inmutabilidad de mensajes:** dataclasses `frozen=True, slots=True`; los `ndarray` dentro de mensajes se marcan `flags.writeable = False`. Así se comparten entre hilos sin copiar y sin carreras.

### 3.2 Modelo de concurrencia (v1: hilos en un solo proceso)

```
Proceso principal (python -m multicam_vision)
 ├─ Hilo main ............ orquestador, señales, ventana cv2.imshow (HighGUI solo en hilo main)
 ├─ cam:cam1:capture ..... FrameSource.read() en bucle → LatestSlot[Frame]   (solo red + decode)
 ├─ cam:cam1:process ..... LatestSlot → detector → CameraModel.pixel_to_world → DropQueue[ObservationBatch]
 ├─ cam:cam2:capture ..... ídem (desconectar cam2 no afecta a cam1)
 ├─ cam:cam2:process ..... ídem
 ├─ fusion ............... DropQueue[ObservationBatch] → MultiCameraTracker → TrackSnapshot
 │                         → RaceEngine (ranking) → EventBus / LatestSlot[TrackSnapshot]
 ├─ esp32 ................ ESP32Link (serial/UDP) → parse → EventBus.publish(RFIDEvent)
 ├─ db-writer ............ único escritor SQLite (WAL) ← DropQueue[DbRecord]
 ├─ log-listener ......... logging.handlers.QueueListener → archivos rotativos
 └─ web (Flask) .......... hilos de werkzeug; solo LEE snapshots y la BD (nunca toca colas del pipeline)
```

Justificación: en v1 se usan hilos y no procesos.
- Medido: cv2 libera el GIL (speedup de 1,71–1,88× con 2 hilos en 2 vCPU). La carga Python pura por frame es pequeña: asociación con <10 objetos y pocas decenas de operaciones.
- Transporte entre hilos ≈ 0 ms, contra 1,9 ms (`mp.Queue`) o 0,25 ms + complejidad (`shared_memory`).
- En Windows, spawn agrega 221 ms por proceso (medido en Linux con spawn; en Windows suele ser mayor, no medido), requiere argumentos picklables y es más difícil de depurar.
- `cv2.setNumThreads(n)` es global. Con 4 hilos de pipeline en 16 hilos lógicos se recomienda empezar con `n = 2` y ajustarlo midiendo (configurable).

**Válvula de escape (ADR-02):** el contrato del worker de cámara (`CameraPipeline`) se define de modo que pueda moverse a un proceso sin cambiar a los consumidores. La salida es una `ObservationBatch` (pequeña, picklable, sin imagen). Si el profiling en la PC real muestra contención del GIL, se pasa a "un proceso por cámara" y viajan entre procesos solo las observaciones, que pesan bytes, no MB. Los frames para la visualización irían en ese caso por `shared_memory` o como JPEG re-codificado (0,09 ms medido para 78 KB).

### 3.3 Flujo de datos

```
 IP Webcam (HTTP MJPEG)                         ESP32 + 3×RC522
        │ /video                                    │ JSON por línea (serial 115200 o UDP)
        ▼                                           ▼
 [cameras.sources.MjpegHttpSource]           [integration.esp32.ESP32Link]
        │ (bool, img, t_recv_ns)                    │ bytes
        ▼                                           ▼
 [cameras.capture.CameraWorker] ──CameraStatus──►  [integration.rfid.parse_message] → RFIDEvent
        │ Frame (último; los viejos se pisan)       │
        ▼                                           │
   LatestSlot[Frame] ──────────────► overlay/web    │
        │                                           │
        ▼                                           │
 [cameras.pipeline.CameraPipeline]                  │
   ├ descarta si now − t_capture > max_age_ms       │
   ├ detection.VehicleDetector.detect(frame)        │
   │     → list[Detection] (px)                     │
   └ calibration.CameraModel.pixel_to_world(px, z)  │
         → list[Observation] (mm + σ)               │
        │ ObservationBatch                          │
        ▼                                           │
   DropQueue[ObservationBatch] (maxsize 8)          │
        │                                           │
        ▼                                           ▼
 [tracking.fusion.FusionLoop] ◄────────────── integration.events.EventBus
   ├ cameras.synchronization.ObservationSync (ventana temporal)
   ├ tracking.association.merge_cross_camera  (dedup en zona compartida, mm)
   ├ tracking.tracker.MultiCameraTracker.update → TrackSnapshot
   ├ tracking.trajectories.TrajectoryStore.append
   └ race.engine.RaceEngine.on_snapshot / on_event(RFIDEvent)
         → Standings (1°,2°,3°), LineCrossingEvent, LapEvent
        │
        ├──► LatestSlot[TrackSnapshot], LatestSlot[Standings] ──► visualization.dashboard (Flask, SSE)
        └──► DropQueue[DbRecord] ──► persistence.database.DatabaseWriter (SQLite WAL)
                                         ▲
                     visualization.dashboard (consultas de solo lectura)
```

### 3.4 Grafo de dependencias en capas

Regla: un paquete solo puede importar paquetes de **capas inferiores**, nunca de la misma capa (salvo dentro del propio paquete) ni de capas superiores.

```
Capa 0  core            (types, clock, queues, errors, logging_setup)        ← solo stdlib + numpy
Capa 1  config          (schema dataclasses + loader TOML)                   ← core
Capa 2  calibration     (CameraModel, intrínsecos, extrínsecos, store)       ← core, config
        cameras         (sources, capture, ipwebcam, synchronization)        ← core, config
        integration     (events bus, rfid, esp32)                            ← core, config
Capa 3  detection       (motion, aruco, color, composite)                    ← core, config
Capa 4  tracking        (association, tracker, trajectories, fusion)         ← core, config, calibration*
        race            (track_model, ranking, engine)                       ← core, config
Capa 5  persistence     (database, recorder)                                 ← core, config
        visualization   (overlay, dashboard)                                 ← core, config, calibration (solo para dibujar)
Capa 6  app             (pipeline.py: cableado) + main.py                    ← todo
        tools/          (CLIs de calibración, grabación, replay)             ← todo
```
\* `tracking` usa de `calibration` solo el tipo `CameraModel` (para la zona compartida o FOV en mm). Se resuelve pasándole polígonos ya en mm desde `app`, así que `tracking` puede quedar dependiendo solo de `core`. Es lo preferido.

Notas:
- `detection` no conoce `calibration`: trabaja solo en píxeles. La proyección al mundo la hace `cameras.pipeline` (capa 6 lógica; se ubica en `app/camera_pipeline.py` para no romper capas; ver árbol).
- `race` no conoce `tracking`: consume `TrackSnapshot` y `Event` de `core.types`.
- `core.types` es el único "vocabulario común". Ningún módulo define mensajes que viajen entre paquetes fuera de `core.types`.
- Test `tests/test_architecture.py`: recorre con `ast` todos los `import` y falla si se viola la tabla de capas o si hay ciclos.

### 3.5 Árbol de carpetas definitivo

Cambios respecto de la estructura propuesta, con su justificación:
- **+ `core/`**: tipos, colas y reloj comunes. Es necesario para evitar ciclos.
- **`config/`**: `settings.py` + `cameras.py` pasan a ser un esquema en dataclasses (`schema.py`) más un loader. Los valores van en archivos `.toml` (ADR-07).
- **+ `race/`**: el ranking 1°/2°/3° no es tracking ni integración; es lógica de negocio de carrera.
- **+ `persistence/`**: se separa la BD del dashboard (un único escritor).
- **+ `app/`**: el cableado del pipeline, así `main.py` queda en unas pocas líneas y es importable sin efectos secundarios (requisito de spawn).
- **+ `cameras/sources.py`**: separa el "de dónde vienen los bytes" (HTTP MJPEG, archivo, sintético) del "cómo se gestiona el hilo"; esto habilita el replay y los tests.
- **+ `cameras/ipwebcam.py`**: el control HTTP de la app IP Webcam (zoom, tamaño, `/status.json`) queda separado de la captura.
- **+ `calibration/store.py` y `calibration/model.py`**: artefactos de calibración versionados y un objeto `CameraModel` único.
- **+ `tools/`**: CLIs reproducibles (calibrar, grabar, reproducir, medir).
- `calibration/panorama.py` se **mantiene** pero solo para estudio y visualización; nunca entra en el camino de medición (ADR-05).
- `visualization/dashboard.py` pasa a ser un paquete `visualization/web/` porque incluye plantillas y estáticos (se migra el Flask existente).

```
multicam_vision/
├── pyproject.toml                 # metadata + dependencias; requirements.txt generado/espejo
├── requirements.txt
├── README.md
├── main.py                        # 10 líneas: if __name__ == "__main__": app.cli.main()
├── config/
│   ├── default.toml               # valores por defecto versionados
│   ├── local.toml                 # override por máquina (gitignored): IPs, puertos COM
│   └── cameras/
│       ├── cam1.toml              # perfil por cámara (URL, resolución, fps, roi, calib file)
│       └── cam2.toml              # cam2.enabled = false hasta configurarla
├── src/multicam_vision/           # (layout src/: impide importar sin instalar `pip install -e .`)
│   ├── __init__.py
│   ├── core/
│   │   ├── types.py               # Frame, Detection, WorldPoint, Observation, Track, Event…
│   │   ├── queues.py              # LatestSlot, DropQueue
│   │   ├── clock.py               # Clock, MonotonicClock, ReplayClock
│   │   ├── errors.py
│   │   └── logging_setup.py       # QueueHandler/Listener, JSONL de métricas
│   ├── config/
│   │   ├── schema.py              # dataclasses Settings, CameraConfig, …
│   │   └── loader.py              # load_settings(paths) → Settings (merge + validación)
│   ├── cameras/
│   │   ├── sources.py             # FrameSource, MjpegHttpSource, FileSource, SyntheticSource
│   │   ├── capture.py             # CameraWorker (hilo, reconexión, backoff, estado)
│   │   ├── ipwebcam.py            # IpWebcamControl (settings HTTP, status.json)
│   │   └── synchronization.py     # ObservationSync, StalenessPolicy
│   ├── calibration/
│   │   ├── camera_calibration.py  # intrínsecos + distorsión (ChArUco/tablero)
│   │   ├── homography.py          # extrínsecos: solvePnP + H(z=0), correspondencias zona compartida
│   │   ├── model.py               # CameraModel (pixel↔mundo, a altura z)
│   │   ├── store.py               # save/load JSON versionado
│   │   └── panorama.py            # SOLO visual/estudio (warp a vista cenital común)
│   ├── detection/
│   │   ├── base.py                # VehicleDetector Protocol, DetectorConfig
│   │   ├── aruco.py               # ArucoVehicleDetector (código previo app.py/app2.py)
│   │   ├── motion.py              # MotionDetector (MOG2 a escala reducida + morfología)
│   │   ├── vehicles.py            # CompositeDetector, filtros de tamaño 1:24 en mm
│   │   └── color.py               # (opcional) ColorBlobDetector
│   ├── tracking/
│   │   ├── kalman.py              # CV/CA Kalman en mm
│   │   ├── association.py         # gating + Hungarian (scipy), merge_cross_camera
│   │   ├── tracker.py             # MultiCameraTracker
│   │   ├── trajectories.py        # TrajectoryStore, velocidades suavizadas
│   │   └── fusion.py              # FusionLoop (hilo)
│   ├── integration/
│   │   ├── events.py              # EventBus (pub/sub con DropQueue por suscriptor)
│   │   ├── rfid.py                # parse_message, TagRegistry, RFIDEvent factory
│   │   └── esp32.py               # ESP32Link Protocol, SerialLink, UdpJsonLink, ClockSync
│   ├── race/
│   │   ├── track_model.py         # línea central (polilínea mm), progress(), líneas de meta/sector
│   │   ├── ranking.py             # compute_standings() pura
│   │   └── engine.py              # RaceEngine (estado de sesión, vueltas, cruces)
│   ├── persistence/
│   │   ├── database.py            # DatabaseWriter (hilo), schema, migraciones, Reader
│   │   └── recorder.py            # SessionRecorder (video + timestamps + JSONL)
│   ├── visualization/
│   │   ├── overlay.py             # draw_* puras sobre np.ndarray
│   │   └── web/
│   │       ├── app.py             # create_app(...) Flask + SSE
│   │       ├── templates/ static/
│   └── app/
│       ├── camera_pipeline.py     # CameraPipeline (frame → detecciones → mundo)
│       ├── pipeline.py            # Pipeline: construye, arranca, para, salud
│       └── cli.py                 # argparse: run | replay | calibrate | record | bench
├── tools/                         # wrappers finos de app.cli (calibrate_intrinsics.py, …)
├── data/
│   ├── calib/                     # cam1_intrinsics_960x720.json, cam1_extrinsics.json …
│   ├── track/                     # track_layout.json (línea central, meta, zonas, marcadores fijos)
│   ├── recordings/                # sesiones grabadas (gitignored)
│   └── db/autodromo.sqlite3
├── logs/                          # app.log rotativo + metrics.jsonl
└── tests/
    ├── conftest.py                # fixtures: SyntheticSource, ReplayClock, settings mínimos
    ├── test_architecture.py       # capas + ausencia de ciclos
    ├── unit/                      # por paquete
    ├── integration/               # pipeline completo con video sintético/grabado
    └── data/                      # clips cortos y ground-truth sintético
```

### 3.6 Configuración (ADR-07)
- **Formato TOML**, leído con `tomllib` (stdlib desde 3.11). No agrega dependencias, admite comentarios y tiene tipos nativos. PyYAML existe en el workspace pero no está verificado en la PC del usuario. Los `.py` como configuración quedan descartados: ejecutan código, no se validan y no se pueden cambiar sin tocar el código.
- Orden de merge: `default.toml` → `cameras/*.toml` → `local.toml` → variables de entorno `MCV__SECCION__CLAVE` → argumentos CLI. El resultado es un `Settings` inmutable y validado (con errores claros, por ejemplo una URL inválida o un fps ≤ 0).
- **Perfiles por cámara**: un archivo por cámara con `id`, `enabled`, `url`, `width`, `height`, `fps_target`, `max_frame_age_ms`, `reconnect` (backoff), `ipwebcam` (aplicar zoom 1x / 4:3 al conectar), `calibration_file`, `roi_polygon_px`, `detector` (overrides). La IP cambia por DHCP: se recomienda reservar la IP en el router; `local.toml` existe para cambiarla sin tocar los archivos versionados.
- **Artefactos de calibración**: JSON con `schema_version`, `camera_id`, `image_size`, `K`, `dist`, `rms_px`, `rvec`, `tvec`, `H_img_to_world`, `reproj_err_mm`, `created_at`, `n_samples`, `tool_version`. `CameraModel.load` **se niega a cargar** si `image_size` no coincide con la resolución del stream. Escalar K se permite solo con un flag explícito, porque los modos 16:9 recortan el sensor y escalar no alcanza.
- El `Settings` efectivo se vuelca a `logs/<session_id>/settings.effective.json` al arrancar (reproducibilidad).

### 3.7 Logging (ADR-08)
- stdlib `logging` con `QueueHandler` en todos los hilos y un solo `QueueListener` con `RotatingFileHandler` (`logs/app.log`) + consola. Los hilos del pipeline nunca hacen I/O de disco al loguear.
- Jerarquía de loggers: `mcv.cameras.cam1`, `mcv.tracking`, `mcv.integration.esp32`, etc. Formato: `ts monotonic_ms level logger thread msg`.
- **Métricas** separadas en `logs/metrics.jsonl`, cada 1 s por cámara: fps de entrada, fps procesados, frames descartados por antigüedad, latencia captura→observación (p50/p95), reconexiones, drops por cola, tiempo por etapa. Es la única fuente válida para afirmar rendimiento.
- Rate-limit para mensajes repetitivos (por ejemplo "cam2 desconectada": como mucho una vez cada 5 s).
- `session_id` (timestamp + uuid corto) en cada registro, en la BD y en las grabaciones.

### 3.8 Encaje de Flask + SQLite y ranking
- **Flask**: se migra `web page/flassk.py` a `visualization/web/app.py` con `create_app(...)` (app factory). El push en vivo se hace con **Server-Sent Events** (una ruta `/stream` a ≤10 Hz), que solo requiere Flask. Flask-SocketIO no está en el workspace y para emisión unidireccional no aporta nada. Si el usuario ya lo tiene, queda como alternativa opcional detrás de la misma interfaz `StateProvider`. Flask corre en un hilo (`threaded=True`) y lee **snapshots inmutables** (`LatestSlot`) y la BD en solo lectura.
- **SQLite**: un único `DatabaseWriter` (hilo), `PRAGMA journal_mode=WAL`, inserción por lotes cada 200 ms. El escritor es dueño de la conexión de escritura; el dashboard abre sus propias conexiones de lectura. Esquema (unifica la inconsistencia de IDs 10/11/12 contra 1/2/3: **`vehicle_id` canónico = 1, 2, 3 = ID ArUco**; los tags RFID se mapean en config):
  - `vehicles(vehicle_id PK, name, aruco_id, color)`
  - `sessions(session_id PK, started_at, ended_at, settings_json)`
  - `laps(id, session_id, vehicle_id, lap_no, t_start_ns, t_end_ns, lap_ms, source TEXT('rfid'|'vision'|'fused'), confidence)`
  - `rfid_events(id, session_id, t_host_ns, t_device_ms, lane, uid, vehicle_id NULL, raw)`
  - `positions(session_id, vehicle_id, t_ns, x_mm, y_mm, v_mm_s, s_mm, track_id)`, muestreo 10 Hz configurable
  - `standings(session_id, t_ns, position, vehicle_id, laps, s_mm, gap_ms)`, guardado solo cuando cambia el orden
- **Ranking** (`race/ranking.py`, función pura y testeable): orden por `(laps desc, s_mm desc, t_último_cruce asc)`. `s_mm` es el progreso a lo largo de la línea central (`TrackModel.progress`). Las vueltas se cuentan con RFID cuando hay un evento válido (fuente primaria y discreta) y con cruce visual de la línea de meta como respaldo y validación cruzada. El origen queda registrado en `laps.source`. RFID y visión **son eventos separados** y la reconciliación ocurre solo en `RaceEngine`.

### 3.9 Tiempo y sincronización (ADR-06)
- IP Webcam no expone un timestamp de exposición. `t_capture_ns = time.monotonic_ns()` al terminar `read()` en el hilo de captura; también se guarda `t_wall` (epoch) para la BD. La latencia de red y la del encoder del teléfono son desconocidas: hay que **medirlas** (prueba con un cronómetro en pantalla, sección 8) y configurarlas como `latency_offset_ms` por cámara.
- No hay sincronización por hardware. `ObservationSync` agrupa observaciones de cámaras distintas cuyo `t_capture_ns` corregido cae dentro de una ventana (`sync_window_ms`, por defecto 1/fps). El tracker además **predice cada track al timestamp de cada observación** (Kalman con dt variable), así que no hace falta que las cámaras estén alineadas.
- Frames obsoletos: `CameraPipeline` descarta un frame si `now − t_capture > max_frame_age_ms`, y el `LatestSlot` ya pisa los no consumidos. Ambas cosas se cuentan en métricas.
- ESP32: el mensaje trae `ms` (millis del dispositivo). `ClockSync` estima el offset con un ping/pong periódico (mínimo RTT/2). Si no hay ping, se usa la hora de recepción en el host con la incertidumbre que corresponde.

---

## 4. Tecnologías utilizadas
- Python ≥3.11 (por `tomllib`; workspace en 3.13.18). Python free-threaded **no**: no hay garantía de wheels de OpenCV para esa variante y además no hace falta, porque cv2 ya libera el GIL.
- OpenCV (contrib, con `aruco`; backend `CAP_FFMPEG`), NumPy, SciPy (`linear_sum_assignment` para la asociación).
- stdlib: `threading`, `queue`, `collections.deque`, `logging.handlers`, `tomllib`, `sqlite3`, `socket` (UDP), `json`, `dataclasses`, `typing.Protocol`, `ast` (test de arquitectura).
- Flask (presente en el workspace). Opcionales con import perezoso: `pyserial` (no está en el workspace; `SerialLink` lo importa al instanciarse) y `flask_socketio` (no está en el workspace).
- pytest. FFmpeg CLI para grabar y reproducir.

---

## 5. Archivos que necesita crear (y dueño de cada uno)

La numeración de agentes es una propuesta; el orquestador la ajusta a la numeración real. **Regla:** solo el dueño modifica un archivo. Cualquier otro agente que necesite un cambio lo pide por escrito al dueño. `core/types.py` y `config/schema.py` son **contratos congelados**: se cambian solo con un ADR aprobado por Arquitectura.

| Archivo(s) | Dueño |
|---|---|
| `core/*`, `config/schema.py`, `config/loader.py`, `app/pipeline.py`, `app/cli.py`, `main.py`, `pyproject.toml`, `requirements.txt`, `tests/test_architecture.py`, `config/default.toml` (estructura) | **01 Arquitectura** |
| `cameras/sources.py`, `cameras/capture.py`, `cameras/ipwebcam.py`, `cameras/synchronization.py`, `config/cameras/*.toml`, `persistence/recorder.py` | 02 Cámaras/Captura |
| `calibration/*`, `data/calib/*`, `data/track/track_layout.json` (geometría), `tools/calibrate_*.py` | 03 Calibración |
| `detection/*`, `app/camera_pipeline.py` (compartido con 03 solo en la llamada a `pixel_to_world`, que es de 03) | 04 Detección |
| `tracking/*` | 05 Tracking |
| `integration/*`, firmware ESP32 (fuera del árbol Python: `firmware/esp32/`) | 06 Integración ESP32/RFID |
| `race/*`, `persistence/database.py`, `visualization/*` | 07 Carrera/BD/Visualización |
| `tests/integration/*`, `tests/data/*`, `tests/conftest.py`, protocolo de medición | 08 QA/Validación |
| `tests/unit/<paquete>/*` | dueño del paquete |
| `README.md` | 01 (secciones redactadas por cada dueño) |

---

## 6. Interfaces (firmas Python concretas)

### 6.1 `core/types.py` (contrato común)
```python
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import NewType, Literal
import numpy as np

CameraId = NewType("CameraId", str)      # "cam1", "cam2"
VehicleId = NewType("VehicleId", int)    # 1, 2, 3 (= ID ArUco)
TrackId = NewType("TrackId", int)        # global, monotónico, nunca se reutiliza

@dataclass(frozen=True, slots=True)
class Frame:
    camera_id: CameraId
    seq: int                       # contador por cámara, sin huecos salvo reconexión
    t_capture_ns: int              # time.monotonic_ns() al recibir (corregido: − latency_offset)
    t_wall: float                  # time.time() para persistencia
    image: np.ndarray              # HxWx3 uint8 BGR, writeable=False
    reconnect_epoch: int = 0       # cambia en cada reconexión (resetea fondos MOG2)
    @property
    def size(self) -> tuple[int, int]: return (self.image.shape[1], self.image.shape[0])

class CameraState(Enum):
    DISABLED = auto(); CONNECTING = auto(); STREAMING = auto(); STALLED = auto(); DISCONNECTED = auto()

@dataclass(frozen=True, slots=True)
class CameraStatus:
    camera_id: CameraId
    state: CameraState
    t_ns: int
    fps_in: float
    frames_total: int
    frames_dropped_stale: int
    reconnects: int
    last_error: str | None = None

@dataclass(frozen=True, slots=True)
class Detection:
    camera_id: CameraId
    frame_seq: int
    t_capture_ns: int
    bbox_px: tuple[float, float, float, float]       # x, y, w, h
    anchor_px: tuple[float, float]                   # punto a proyectar (centro de marcador o del contacto con el piso)
    anchor_height_mm: float                          # altura sobre el plano del punto anchor (techo ≈ 40–50 mm; piso = 0)
    method: Literal["aruco", "motion", "color", "composite"]
    confidence: float                                # [0, 1]
    vehicle_hint: VehicleId | None = None            # identidad si el método la da (ArUco)
    heading_px_rad: float | None = None
    corners_px: np.ndarray | None = None             # 4x2 para ArUco, writeable=False
    area_px: float = 0.0

@dataclass(frozen=True, slots=True)
class WorldPoint:
    x_mm: float
    y_mm: float
    sigma_mm: float                # 1σ isotrópica propagada desde el error de píxel + calibración

@dataclass(frozen=True, slots=True)
class Observation:
    detection: Detection
    world: WorldPoint
    in_shared_zone: bool
    heading_rad: float | None = None   # en el marco del mundo

@dataclass(frozen=True, slots=True)
class ObservationBatch:            # salida por frame procesado de una cámara (picklable, sin imagen)
    camera_id: CameraId
    frame_seq: int
    t_capture_ns: int
    observations: tuple[Observation, ...]
    processing_ms: float

class TrackState(Enum):
    TENTATIVE = auto(); CONFIRMED = auto(); LOST = auto(); DELETED = auto()

@dataclass(frozen=True, slots=True)
class Track:
    track_id: TrackId
    vehicle_id: VehicleId | None
    state: TrackState
    t_ns: int
    x_mm: float; y_mm: float
    vx_mm_s: float; vy_mm_s: float
    sigma_pos_mm: float
    heading_rad: float | None
    cameras: frozenset[CameraId]   # cámaras que lo vieron en la última actualización
    hits: int
    misses: int
    @property
    def speed_mm_s(self) -> float: return float(np.hypot(self.vx_mm_s, self.vy_mm_s))

@dataclass(frozen=True, slots=True)
class TrackSnapshot:
    t_ns: int
    tracks: tuple[Track, ...]

class EventSource(Enum):
    VISION = auto(); RFID = auto(); SYSTEM = auto()

@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    t_ns: int                      # reloj monotónico del host
    t_wall: float
    source: EventSource
    event_id: str                  # uuid4 hex

@dataclass(frozen=True, slots=True, kw_only=True)
class RFIDEvent(Event):
    lane: int                      # 1..3 (lector)
    uid: str                       # hex mayúscula
    device_ms: int | None
    device_seq: int | None
    vehicle_id: VehicleId | None   # resuelto por TagRegistry, None si el tag es de pista o desconocido
    raw: str

@dataclass(frozen=True, slots=True, kw_only=True)
class LineCrossingEvent(Event):    # source = VISION
    vehicle_id: VehicleId | None
    track_id: TrackId
    line_id: str                   # "finish", "sector1", …
    direction: int                 # +1 sentido de carrera, −1 contrario

@dataclass(frozen=True, slots=True, kw_only=True)
class LapEvent(Event):             # emitido por RaceEngine
    vehicle_id: VehicleId
    lap_no: int
    lap_ms: float | None
    basis: Literal["rfid", "vision", "fused"]
    confidence: float

@dataclass(frozen=True, slots=True, kw_only=True)
class CameraStatusEvent(Event):
    status: CameraStatus

@dataclass(frozen=True, slots=True)
class Standing:
    position: int                  # 1, 2, 3
    vehicle_id: VehicleId
    laps: int
    s_mm: float
    gap_ms: float | None
    last_lap_ms: float | None
    tracked: bool                  # False si el auto está LOST (posición congelada)

@dataclass(frozen=True, slots=True)
class Standings:
    t_ns: int
    rows: tuple[Standing, ...]
```

### 6.2 `core/queues.py` y `core/clock.py`
```python
from typing import Generic, TypeVar, Protocol
T = TypeVar("T")

class LatestSlot(Generic[T]):
    """Un solo elemento; put() pisa el anterior. Nunca bloquea al productor."""
    def __init__(self) -> None: ...
    def put(self, item: T) -> bool: ...                         # True si pisó uno no consumido
    def get(self, timeout: float | None = None, newer_than: int = -1) -> tuple[int, T] | None: ...
    def peek(self) -> tuple[int, T] | None: ...                 # sin consumir (visualización)
    @property
    def overwritten(self) -> int: ...

class DropQueue(Generic[T]):
    """Cola acotada FIFO; si está llena descarta el más VIEJO y cuenta."""
    def __init__(self, maxsize: int) -> None: ...
    def put(self, item: T) -> None: ...
    def get(self, timeout: float | None = None) -> T | None: ...
    def drain(self, max_items: int | None = None) -> list[T]: ...
    @property
    def dropped(self) -> int: ...
    def close(self) -> None: ...                                # despierta a los consumidores con None

class Clock(Protocol):
    def now_ns(self) -> int: ...
    def wall(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...

class MonotonicClock: ...                                       # time.monotonic_ns / time.time
class ReplayClock:                                              # avanzado por la fuente en replay
    def advance_to(self, t_ns: int) -> None: ...
```

### 6.3 `config/schema.py` + `loader.py`
```python
@dataclass(frozen=True, slots=True)
class ReconnectConfig: initial_s: float = 0.5; max_s: float = 10.0; factor: float = 2.0; stall_timeout_s: float = 2.0

@dataclass(frozen=True, slots=True)
class CameraConfig:
    id: CameraId; enabled: bool; url: str
    width: int; height: int; fps_target: float
    max_frame_age_ms: float = 150.0
    latency_offset_ms: float = 0.0
    apply_ipwebcam_settings: bool = True
    calibration_file: str | None = None
    roi_polygon_px: tuple[tuple[int, int], ...] | None = None
    detector_overrides: dict[str, object] = field(default_factory=dict)
    reconnect: ReconnectConfig = ReconnectConfig()

@dataclass(frozen=True, slots=True)
class Settings:
    session_name: str
    cameras: tuple[CameraConfig, ...]
    detection: DetectionConfig
    tracking: TrackingConfig
    esp32: Esp32Config
    race: RaceConfig
    database: DatabaseConfig
    web: WebConfig
    logging: LoggingConfig
    opencv_threads: int = 2

def load_settings(paths: Sequence[str | Path], env: Mapping[str, str] | None = None,
                  overrides: Mapping[str, object] | None = None) -> Settings: ...   # lanza ConfigError
def dump_effective(settings: Settings, path: Path) -> None: ...
```

### 6.4 `cameras`
```python
class FrameSource(Protocol):
    def open(self) -> None: ...                                  # lanza SourceError
    def read(self) -> tuple[bool, np.ndarray | None]: ...        # bloqueante; False = error o fin
    def close(self) -> None: ...
    @property
    def is_live(self) -> bool: ...                               # False en archivo (no reconecta, respeta timestamps)

class MjpegHttpSource:  # cv2.VideoCapture(url, cv2.CAP_FFMPEG) + timeouts de apertura/lectura
    def __init__(self, url: str, open_timeout_ms: int = 3000, read_timeout_ms: int = 2000) -> None: ...
class FileSource:       # video + sidecar .jsonl {seq, t_capture_ns}; alimenta ReplayClock
    def __init__(self, video: Path, timestamps: Path | None, clock: ReplayClock | None, realtime: bool) -> None: ...
class SyntheticSource:  # escenas generadas con ground truth (para tests)
    def __init__(self, scene: "SyntheticScene", camera_id: CameraId) -> None: ...

class CameraWorker:
    def __init__(self, cfg: CameraConfig, source_factory: Callable[[], FrameSource],
                 out: LatestSlot[Frame], clock: Clock,
                 on_status: Callable[[CameraStatus], None]) -> None: ...
    def start(self) -> None: ...
    def stop(self, timeout: float = 2.0) -> None: ...
    def status(self) -> CameraStatus: ...

class IpWebcamControl:
    def __init__(self, base_url: str, timeout_s: float = 2.0) -> None: ...
    def status(self) -> dict: ...
    def apply(self, video_size: str | None = None, zoom: int | None = None, **settings: str) -> None: ...

class ObservationSync:
    def __init__(self, window_ms: float, camera_ids: Sequence[CameraId]) -> None: ...
    def push(self, batch: ObservationBatch) -> None: ...
    def pop_ready(self, now_ns: int) -> list[ObservationBatch]: ...  # ordenadas por t_capture_ns; libera por ventana o timeout
```

### 6.5 `calibration`
```python
@dataclass(frozen=True, slots=True)
class Intrinsics:
    image_size: tuple[int, int]; K: np.ndarray; dist: np.ndarray; rms_px: float; model: Literal["pinhole", "fisheye"]

@dataclass(frozen=True, slots=True)
class Extrinsics:
    rvec: np.ndarray; tvec: np.ndarray            # mundo → cámara (mm)
    H_undist_to_world: np.ndarray                 # 3x3, plano z=0, sobre píxeles sin distorsión
    reproj_err_mm: float; n_points: int

@dataclass(frozen=True, slots=True)
class BoardSpec:
    kind: Literal["charuco", "chessboard"]; squares: tuple[int, int]; square_mm: float; marker_mm: float | None; dictionary: str

def calibrate_intrinsics(images: Iterable[np.ndarray], board: BoardSpec,
                         model: Literal["pinhole", "fisheye"] = "pinhole") -> Intrinsics: ...
def estimate_extrinsics(intr: Intrinsics, img_pts_px: np.ndarray, world_pts_mm: np.ndarray,
                        ransac_thresh_px: float = 3.0) -> Extrinsics: ...
def refine_with_shared_zone(models: Mapping[CameraId, "CameraModel"],
                            pairs: Sequence[tuple[CameraId, np.ndarray, CameraId, np.ndarray]]) -> dict[CameraId, Extrinsics]: ...

class CameraModel:
    camera_id: CameraId; intrinsics: Intrinsics; extrinsics: Extrinsics
    def undistort_points(self, px: np.ndarray) -> np.ndarray: ...                         # Nx2
    def pixel_to_world(self, px: np.ndarray, z_mm: float | np.ndarray = 0.0) -> np.ndarray: ...  # Nx2 mm (rayo ∩ plano z)
    def world_to_pixel(self, xy_mm: np.ndarray, z_mm: float = 0.0) -> np.ndarray: ...
    def sigma_mm(self, px: np.ndarray, sigma_px: float, z_mm: float = 0.0) -> np.ndarray: ...  # jacobiano numérico
    def footprint_mm(self) -> np.ndarray: ...                                             # polígono visible en el mundo

def save_model(model: CameraModel, path: Path) -> None: ...
def load_model(path: Path, expected_size: tuple[int, int] | None) -> CameraModel: ...       # CalibrationMismatchError
```

### 6.6 `detection`
```python
class VehicleDetector(Protocol):
    name: str
    def detect(self, frame: Frame, roi_mask: np.ndarray | None = None) -> list[Detection]: ...
    def reset(self) -> None: ...                     # al cambiar reconnect_epoch o iluminación

class ArucoVehicleDetector:
    def __init__(self, dictionary: str, valid_ids: Mapping[int, VehicleId], marker_mm: float,
                 roof_height_mm: float, params: Mapping[str, float]) -> None: ...
class MotionDetector:
    def __init__(self, scale: float = 0.5, history: int = 500, var_threshold: float = 16,
                 min_area_px: int = 800, morph_kernel: int = 5) -> None: ...
class CompositeDetector:
    def __init__(self, detectors: Sequence[VehicleDetector], merge_iou: float = 0.3) -> None: ...
```

### 6.7 `app/camera_pipeline.py`
```python
class CameraPipeline:
    def __init__(self, cfg: CameraConfig, inp: LatestSlot[Frame], detector: VehicleDetector,
                 model: CameraModel | None, shared_zone_mm: np.ndarray | None,
                 out: DropQueue[ObservationBatch], clock: Clock, metrics: "MetricsSink") -> None: ...
    def start(self) -> None: ...
    def stop(self, timeout: float = 2.0) -> None: ...
    def last_detections(self) -> tuple[Frame, list[Detection]] | None: ...   # para overlay
```

### 6.8 `tracking`
```python
@dataclass(frozen=True, slots=True)
class AssociationResult:
    matches: tuple[tuple[TrackId, int], ...]   # (track, índice de observación)
    unmatched_tracks: tuple[TrackId, ...]
    unmatched_obs: tuple[int, ...]

def merge_cross_camera(obs: Sequence[Observation], max_dist_mm: float, max_dt_ms: float) -> list[Observation]: ...
def associate(predicted: Sequence[Track], obs: Sequence[Observation], gate_mahalanobis: float = 9.21) -> AssociationResult: ...

class MultiCameraTracker:
    def __init__(self, cfg: TrackingConfig, id_source: Callable[[], TrackId] | None = None) -> None: ...
    def update(self, batch: Sequence[Observation], t_ns: int) -> TrackSnapshot: ...
    def predict(self, t_ns: int) -> TrackSnapshot: ...
    def tracks(self) -> tuple[Track, ...]: ...

class TrajectoryStore:
    def __init__(self, max_points_per_vehicle: int = 20_000) -> None: ...
    def append(self, snap: TrackSnapshot) -> None: ...
    def get(self, vehicle_id: VehicleId, since_ns: int | None = None) -> np.ndarray: ...   # Nx4 [t_ns, x, y, v]
    def export_csv(self, path: Path) -> None: ...

class FusionLoop:
    def __init__(self, inp: DropQueue[ObservationBatch], sync: ObservationSync, tracker: MultiCameraTracker,
                 trajectories: TrajectoryStore, on_snapshot: Callable[[TrackSnapshot], None], clock: Clock) -> None: ...
    def start(self) -> None: ...
    def stop(self, timeout: float = 2.0) -> None: ...
```

### 6.9 `integration`
```python
class Subscription(Generic[T]):
    def get(self, timeout: float | None = None) -> T | None: ...
    def drain(self) -> list[T]: ...
    @property
    def dropped(self) -> int: ...

class EventBus:
    def publish(self, event: Event) -> None: ...                                  # O(n_subs), nunca bloquea
    def subscribe(self, types: tuple[type[Event], ...], maxsize: int = 256) -> Subscription[Event]: ...

class ESP32Link(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    @property
    def connected(self) -> bool: ...

class SerialLink:   def __init__(self, port: str, baud: int, bus: EventBus, registry: "TagRegistry", clock: Clock) -> None: ...
class UdpJsonLink:  def __init__(self, bind: tuple[str, int], bus: EventBus, registry: "TagRegistry", clock: Clock) -> None: ...

class TagRegistry:
    def __init__(self, uid_to_vehicle: Mapping[str, VehicleId], uid_to_marker: Mapping[str, str]) -> None: ...
    def resolve(self, uid: str) -> VehicleId | None: ...

def parse_message(line: bytes | str, t_ns: int, t_wall: float, registry: TagRegistry) -> RFIDEvent | None: ...
# Protocolo ESP32 → host (una línea JSON, UTF-8, '\n'):
# {"v":1,"type":"rfid","seq":42,"ms":123456,"lane":2,"uid":"04A1B2C3"}
# {"v":1,"type":"hb","seq":43,"ms":124000}          heartbeat cada 1 s
# {"v":1,"type":"pong","seq":44,"ms":124010,"id":7}  respuesta a {"type":"ping","id":7}
```

### 6.10 `race`, `persistence`, `visualization`, `app`
```python
class TrackModel:
    @classmethod
    def load(cls, path: Path) -> "TrackModel": ...
    @property
    def length_mm(self) -> float: ...
    def progress(self, x_mm: float, y_mm: float) -> tuple[float, float]: ...    # (s_mm ∈ [0, L), lateral_mm)
    def crossings(self, prev: Track, curr: Track) -> list[tuple[str, int]]: ...  # (line_id, dirección)

def compute_standings(laps: Mapping[VehicleId, int], s_mm: Mapping[VehicleId, float],
                      last_cross_ns: Mapping[VehicleId, int], tracked: Mapping[VehicleId, bool],
                      t_ns: int) -> Standings: ...

class RaceEngine:
    def __init__(self, cfg: RaceConfig, track: TrackModel, bus: EventBus, clock: Clock) -> None: ...
    def on_snapshot(self, snap: TrackSnapshot) -> Standings: ...
    def on_event(self, ev: Event) -> None: ...
    def start_session(self, name: str) -> str: ...
    def end_session(self) -> None: ...

class DatabaseWriter:
    def __init__(self, path: Path, batch_ms: int = 200, queue_size: int = 10_000) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...                     # vacía la cola antes de cerrar
    def submit(self, record: "DbRecord") -> None: ...   # nunca bloquea; drop contado
class DatabaseReader:
    def __init__(self, path: Path) -> None: ...
    def standings(self, session_id: str) -> list[dict]: ...
    def laps(self, session_id: str, vehicle_id: int | None = None) -> list[dict]: ...

class SessionRecorder:
    def __init__(self, out_dir: Path, camera_ids: Sequence[CameraId], codec: str = "MJPG") -> None: ...
    def write(self, frame: Frame) -> None: ...      # en su propio hilo, con cola acotada

def draw_detections(img: np.ndarray, dets: Sequence[Detection]) -> np.ndarray: ...
def draw_tracks_topdown(snap: TrackSnapshot, track: TrackModel, scale_px_per_mm: float) -> np.ndarray: ...

class StateProvider(Protocol):
    def standings(self) -> Standings | None: ...
    def snapshot(self) -> TrackSnapshot | None: ...
    def camera_status(self) -> Mapping[CameraId, CameraStatus]: ...
    def jpeg(self, camera_id: CameraId, overlay: bool = True) -> bytes | None: ...

def create_app(state: StateProvider, db: DatabaseReader, cfg: WebConfig) -> "flask.Flask": ...

class Pipeline:
    def __init__(self, settings: Settings, clock: Clock | None = None, replay: Mapping[CameraId, Path] | None = None) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...                     # orden: fuentes → pipelines → fusión → BD → logging
    def health(self) -> dict[str, object]: ...
```

---

## 7. ADR (decisiones arquitectónicas breves)

| # | Decisión | Motivo | Alternativa descartada |
|---|---|---|---|
| ADR-01 | Un proceso con hilos: 2 por cámara (captura y proceso) más fusión, ESP32, BD, logging y web | cv2 libera el GIL (1,71–1,88× medido); transporte ≈0 ms; sin trampas de spawn | Un proceso por cámara: 1,9 ms/frame con `mp.Queue`, 221 ms de arranque, pickling |
| ADR-02 | Cámara → resto solo con `ObservationBatch` sin imagen (picklable) | Permite migrar a procesos sin cambiar consumidores | Pasar frames a la fusión |
| ADR-03 | Captura → `LatestSlot` (pisa); demás etapas → `DropQueue` acotada (descarta el más viejo) con contadores | Latencia acotada; un consumidor lento nunca acumula retraso | `queue.Queue` sin límite (latencia creciente); `put` bloqueante (frena la captura) |
| ADR-04 | Se des-distorsionan **puntos**, no imágenes; `remap` completo solo para visualización y calibración | Costo O(detecciones) frente a O(píxeles) | `undistort` de cada frame |
| ADR-05 | Mundo común = plano de pista en mm. Cada cámara tiene un `CameraModel` propio. La panorámica es solo visual | Concatenar o hacer stitching no da métrica; la proyección individual sí | Hacer tracking sobre un panorama |
| ADR-06 | Timestamps monotónicos de recepción + `latency_offset_ms` medido; Kalman con dt variable; sin sync por hardware | IP Webcam no da timestamp de exposición | Suponer cámaras sincronizadas |
| ADR-07 | Config en TOML (`tomllib`) → `Settings` frozen validado; calibración en JSON versionado; un perfil por cámara | Cero dependencias, comentarios, validación | `.py` (ejecuta código), YAML (dependencia no verificada en la PC) |
| ADR-08 | `logging` con QueueHandler/Listener + `metrics.jsonl` | Sin I/O en hilos calientes; métricas medibles | `print` / logging síncrono a archivo |
| ADR-09 | Extrínsecos completos (`solvePnP`: R, t) además de H | El marcador ArUco en el techo está a ~40–50 mm del piso: con vista oblicua, proyectarlo con la H de z=0 introduce un error de paralaje de varios cm. Con R, t se intersecta el rayo con el plano z=h | Solo homografía |
| ADR-10 | Tracking en el mundo con dedup por distancia en mm en la zona compartida; `TrackId` global y `VehicleId` por ArUco | Un auto visto por 2 cámaras cae en el mismo lugar del mundo si la calibración es buena | Trackers por cámara + hand-off por píxeles |
| ADR-11 | RFID y visión son eventos separados; solo `RaceEngine` los concilia y queda registrado `basis` | Trazabilidad; RFID no bloquea video (hilo propio, bus no bloqueante) | Contar vueltas solo por RFID o solo por visión |
| ADR-12 | Un único escritor SQLite en WAL, inserción por lotes; web solo lectura | SQLite serializa escrituras; WAL permite lecturas concurrentes | Escribir desde Flask y desde el pipeline |
| ADR-13 | Dashboard Flask + SSE; SocketIO opcional | Menos dependencias; unidireccional basta | Exigir Flask-SocketIO |
| ADR-14 | Capas estrictas + `test_architecture.py` (ast) | Evita ciclos a medida que trabajan 8 agentes | Convención informal |
| ADR-15 | Modo replay determinista (`FileSource` + `ReplayClock`) como ciudadano de primera clase | Reproducibilidad y pruebas de precisión sin hardware | Probar solo en vivo |
| ADR-16 | Layout `src/` + `pyproject.toml`; `main.py` sin efectos al importar | Compatibilidad con spawn en Windows (se observó la re-ejecución del módulo en el benchmark) | Scripts sueltos con código global |
| ADR-17 | `vehicle_id` canónico 1..3 = ID ArUco; UIDs RFID mapeados en config | Corrige la inconsistencia 10/11/12 frente a 1/2/3 del código previo | Mantener dos esquemas |
| ADR-18 | HighGUI (`imshow`/`waitKey`) solo en el hilo main y opcional (`--no-gui`) | HighGUI no es seguro entre hilos en varias plataformas | `imshow` desde los hilos de cámara |

---

## 8. Riesgos y limitaciones
1. **Rendimiento no verificado en la PC real.** Los números son de 2 vCPU en la nube. El presupuesto por frame a 960x720@30 incluye decode (~2,2 ms), ArUco (~7,4 ms) y MOG2 a escala reducida (~2 ms a 480x360), lo que sugiere margen. Es una inferencia, no una medición en el Ryzen.
2. **GIL**: si un agente agrega bucles Python por píxel o por contorno con miles de elementos, los hilos dejan de escalar. Mitigación: métricas por etapa y la válvula ADR-02.
3. **`CAP_PROP_BUFFERSIZE` con FFmpeg sobre HTTP** puede ignorarse. Mitigación: el hilo de captura lee sin pausa y `max_frame_age_ms`. Además, `VideoCapture.read()` puede bloquearse indefinidamente cuando cae la red. Se necesitan timeouts de FFmpeg (`CAP_PROP_OPEN_TIMEOUT_MSEC` / `READ_TIMEOUT_MSEC`) y un watchdog de stall que recree la captura. Falta verificarlo en OpenCV 5 + Windows (lo verifica el agente 02).
4. **Latencia y jitter de IP Webcam por Wi-Fi** desconocidos: afectan la sincronización entre cámaras y el error de posición a alta velocidad (a 2 m/s, 50 ms de error de timestamp equivalen a 100 mm). Hay que medirlos.
5. **Paralaje por altura** (ADR-09): requiere conocer la altura del marcador por auto, y la calibración necesita puntos 3D (no solo coplanares) o intrínsecos buenos.
6. **DHCP**: la IP del teléfono cambia. Se recomienda una reserva en el router; la config la tolera, pero la reconexión no puede "descubrir" la IP nueva.
7. **Hilo web en el mismo proceso**: una ruta Flask lenta no bloquea el pipeline (son hilos distintos), pero compite por el GIL. Si se nota, se mueve Flask a otro proceso que lea la BD y un snapshot vía UDP/JSON local.
8. Dependencias no disponibles en el workspace: `pyserial` y `flask_socketio`. `SerialLink` no se puede probar acá con hardware real; se prueba con un puerto simulado o con UDP.
9. Python free-threaded no se considera.

---

## 9. Pruebas necesarias
- **Arquitectura**: `test_architecture.py` (capas, ciclos, ningún import de `cv2.imshow` fuera de `app`/`visualization`).
- **core**: `LatestSlot` (pisa y cuenta, `newer_than`, timeout), `DropQueue` (descarta el más viejo, `close` despierta), inmutabilidad (frozen, `writeable=False`), picklabilidad de `ObservationBatch` y de los `Event`.
- **config**: merge de las 5 fuentes, errores de validación, `dump_effective` round-trip.
- **cameras**: `CameraWorker` con una fuente falsa que falla, se cuelga y vuelve → transiciones de estado, backoff, `reconnect_epoch`; dos cámaras con una caída que no afecta a la otra; descarte por antigüedad; servidor MJPEG local (FFmpeg) para probar `MjpegHttpSource` con un corte de red simulado.
- **calibration**: escenas sintéticas con K, dist, R, t conocidos → error de reproyección y de `pixel_to_world` en mm, con z=0 y z=h; rechazo por resolución distinta.
- **tracking**: ground truth sintético de 3 autos con cruces, oclusión de N frames y paso por la zona compartida → métricas MOTA/IDF1 simplificadas: 0 duplicados en la zona compartida, ID switches, error RMS en mm.
- **integration**: `parse_message` con mensajes válidos, inválidos, truncados y versión desconocida; el bus no bloquea con un suscriptor lento; ESP32 desconectado → el video sigue (fps sin cambios en métricas).
- **race**: `compute_standings` con empates, auto perdido y conciliación RFID/visión; `TrackModel.progress` con wrap en la meta.
- **integración end-to-end**: replay de una grabación sintética → salida determinista (mismo hash de standings en 2 corridas).
- **Rendimiento** (en la PC del usuario): `app.cli bench` → fps y latencia p50/p95 por etapa con 1 y 2 cámaras, y con 960x720 y 640x480; elegir `opencv_threads`.
- **Latencia de cámara**: filmar un cronómetro en el monitor de la PC, comparar contra `t_capture_ns` → `latency_offset_ms` por cámara.

---

## 10. Estado del trabajo

| Ítem | Estado |
|---|---|
| Arquitectura, capas, árbol, interfaces, ADRs, ownership | **Implementado** (como documento de propuesta) |
| Micro-benchmarks de transporte, GIL y costos de cv2 (2 vCPU nube) | **Probado** (números de la sección 2.2) |
| Validación de los números en la PC del usuario (Ryzen, Windows 11) | **Pendiente** |
| Código del sistema (core, cameras, …) | **Pendiente** (fuera del alcance de esta fase) |
| `test_architecture.py` | **Pendiente** |
| Comportamiento de los timeouts FFmpeg en `VideoCapture` HTTP con OpenCV 5 en Windows | **Pendiente** (agente 02) |
| Disponibilidad de Flask-SocketIO y pyserial en la PC del usuario | **Pendiente** de verificar |

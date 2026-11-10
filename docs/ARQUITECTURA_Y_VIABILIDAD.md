# MULTICAM VISION — Arquitectura definitiva propuesta e informe de viabilidad y riesgos

**Fase:** Primera Orden (investigación + propuesta). **Estado: PENDIENTE DE APROBACIÓN. No se implementó el sistema.**
**Fecha:** 2026-11-08 · **Fuentes:** informes de los 6 agentes (`docs/informes_agentes/`), experimentos reproducibles (`docs/experimentos/`).

> Regla de honestidad aplicada: todos los números de este documento salen de experimentos **sintéticos o simulados** en un workspace Linux en la nube (2 vCPU, Python 3.13.18, OpenCV 5.0.0). **Nada fue medido todavía en tu PC Ryzen/Windows, ni con tus teléfonos reales, ni en tu Wi-Fi, ni con el ESP32 real.** Única excepción: el análisis de la captura real `haruko_captura.jpg`.

---

## 1. Inspección del entorno (pasos 1 y 2 de la Primera Orden)

| Elemento | Workspace en la nube (verificado) | Tu PC Windows 11 |
|---|---|---|
| Python | 3.13.18 | **No verificado** |
| OpenCV | 5.0.0 contrib (aruco + ChArUco OK, FFmpeg backend OK) | Tu código dice "OpenCV 5.0.0" — **no verificado** |
| NumPy / SciPy | 2.5.3 / 1.18.1 | No verificado |
| FFmpeg CLI | 6.1.1 | No verificado |
| pyserial | **NO instalado** | No verificado (obligatorio para el ESP32) |
| Flask / Flask-SocketIO | 3.1.3 / **NO instalado** | No verificado |
| pytest | 9.1.1 | No verificado |
| GPU | — | GTX 550 Ti (Fermi, compute 2.1): **sin CUDA útil para OpenCV/ONNX modernos → se diseña para CPU** |

**Acción para vos:** ejecutar en la PC `python 00_inspeccion_entorno.py --cam1 http://IP_CAM1:8080/video` (y `--cam2` si la tenés). Genera `logs/entorno_*.json` con versiones reales y FPS reales recibidos. Ese archivo cierra el punto 2.

---

## 2. Comparación de propuestas y conflictos resueltos

| # | Tema | Propuestas en conflicto | Decisión | Justificación (medida) |
|---|---|---|---|---|
| C1 | Lector de video | `cv2.VideoCapture(CAP_FFMPEG)` con `BUFFERSIZE=1` (código previo) vs lector MJPEG propio | **Lector MJPEG propio por socket + hilo "último frame"**; FFmpeg como alternativa con timeouts | `BUFFERSIZE` **no tiene efecto** con FFmpeg en OpenCV 5.0.0 (set→False). Consumidor lento ⇒ latencia acumulada de 6,9 s en 20 s. Con hilo de último frame: 17–20 ms mediana (localhost). |
| C2 | Detección de posición | MOG2 (Ag.01, `tests.py`) vs fondo estático (Ag.03) | **Fondo estático de pista vacía, diferencia en color, actualización selectiva fuera de los autos**; MOG2/KNN como alternativa | MOG2 "absorbe" un auto detenido en ~1,1 s (inaceptable en largada o trompo). Fondo estático: 100 % detección, 0 FP/frame sintético, 3,0 ms @960×720 vs MOG2 8,5 ms. Escala de grises solo detecta 61 % (auto negro sobre pista negra) ⇒ **color obligatorio**. |
| C3 | Identidad del auto | ArUco siempre (código previo) vs tracker + ArUco ocasional | **Identidad = tracker (mantiene ID) + ArUco cuando legible (votación 3 lecturas) + RFID en la meta (UID de la etiqueta bajo el auto)** | ArUco con exposición ~1/30 s solo se lee por debajo de ~0,22 m/s (motion blur). Ver Riesgo R1. |
| C4 | Configuración | `settings.py`/`cameras.py` (orden) vs TOML (Ag.01) | **TOML** (`tomllib`, stdlib) + `local.toml` por máquina | Sin dependencias extra, comentarios, validable; IPs cambian por DHCP sin tocar código. Se mantiene un `config/` Python (schema + loader). |
| C5 | Coordenadas / paralaje | Homografía piso (Ag.05) vs pose completa (Ag.01/03) | **Ambas: `solvePnP` (pose) + H; proyección al plano a altura z** | Marcador en techo a 45 mm sin corregir: 72 mm de error con cámara a 1 m; corregido: 0,85 mm (sintético). |
| C6 | Estimación de homografía | RANSAC estricto vs LSQ vs MAGSAC | **RANSAC 4 px + reajuste LSQ sobre inliers** | RANSAC 3 mm: 2,59 mm; propuesto: 0,92 mm; MAGSAC falló ~80 % en OpenCV 5.0.0. |
| C7 | Sincronización entre cámaras | LED (Ag.02) vs calibración por lote con ArUco en solape (Ag.04) | **LED del ESP32 en la zona compartida con período no conmensurable (1037 ms o aleatorio) → offset por cámara; el tracker predice cada track al timestamp de cada observación** | Desfase sin compensar ⇒ 51–55 % duplicados en solape; calibrado ⇒ 0,2–0,3 %. LED a 1000 ms da sesgo de −22 ms; 1037 ms <1 ms. Estimación online descartada (20–24 ms error). |
| C8 | Fusión multicámara | Tracker por cámara + unión vs tracker único en mundo | **Tracker único en el plano de la pista (mm)** | Duplicados en solape: 84–88 % vs 0,2–0,3 % (simulación). |
| C9 | Modelo de movimiento | CV vs CA vs IMM | **Kalman velocidad constante + húngaro + gating χ²=13,82**; velocidad reportada por Savitzky-Golay | IMM = mismo resultado, 3× CPU; CA duplica cambios de ID. SG: 56–64 mm/s RMS vs Kalman 104–110. |
| C10 | Concurrencia | Hilos vs procesos | **Un proceso, hilos (2 por cámara + fusión + ESP32 + BD + web)**, con contrato que permite migrar cada cámara a proceso | cv2 libera el GIL (1,7–1,9× con 2 hilos). Trabajo Python pesado en hilos auxiliares sí degrada el video (medido) ⇒ hilos auxiliares livianos. |
| C11 | Web en vivo | Flask-SocketIO existente vs SSE | **Flask + Server-Sent Events**; SocketIO opcional tras la misma interfaz | Flask-SocketIO no está instalado; el push es unidireccional. |
| C12 | IDs de autos | 1,2,3 (`app.py`) vs 10,11,12 (`flassk.py`) | **`vehicle_id` canónico = 1,2,3 = ID ArUco**; UIDs RFID mapeados en config | Elimina la inconsistencia. |
| C13 | Enlace ESP32 | Serial vs Wi-Fi | **USB serial NDJSON 115200**; UDP detrás de la misma interfaz `Transport` | Simulado: 0 líneas perdidas en 5000, 0,18 ms mediana; sync de reloj 0,061 ms. |
| C14 | Numeración/propiedad de archivos | Ag.01 asumía 8 agentes | **Reasignado a los 6 agentes** (sección 4) | Regla: un archivo, un dueño. |

---

## 3. Arquitectura definitiva

### 3.1 Principios
1. **Todo se mide en el mundo (mm sobre el plano de la pista).** El panorama es solo visualización.
2. **Nunca bloquear al productor:** último-frame en captura; colas acotadas que descartan lo más viejo, con contadores.
3. **Capas sin ciclos**, verificadas por `tests/test_architecture.py` (análisis `ast` de imports).
4. **Reproducibilidad:** todo el pipeline corre desde grabaciones (video + timestamps) con reloj simulado.
5. **Mensajes inmutables** (`dataclass(frozen=True, slots=True)`) definidos solo en `core/types.py`.
6. **El rendimiento solo se afirma desde `logs/metrics.jsonl`.**

### 3.2 Flujo de datos
```
IP Webcam cam1 ─► MjpegHttpSource ─► CameraWorker(hilo) ─► LatestSlot[Frame] ─► CameraPipeline(hilo)
IP Webcam cam2 ─► (idéntico e independiente; su caída no afecta a cam1)          │ descarta frames viejos
                                                                                  │ detect (px) → pixel_to_world (mm, z)
                                                                                  ▼
                                                                    DropQueue[ObservationBatch]
                                                                                  ▼
ESP32 (USB, NDJSON) ─► Esp32Link(hilo) ─► EventBus ──────────────► FusionLoop(hilo)
                                                                     ├ buffer de reordenamiento (100 ms)
                                                                     ├ MultiCameraTracker (Kalman CV, húngaro, mm)
                                                                     ├ TrajectoryStore (posición, velocidad SG)
                                                                     └ RaceEngine (progreso en línea central,
                                                                        vueltas: RFID primario / visión respaldo,
                                                                        ranking 1°-2°-3°)
                                                                                  ▼
                                               LatestSlot[Snapshot] ─► Web Flask (SSE) / overlay
                                               DropQueue[DbRecord]  ─► DatabaseWriter (SQLite WAL, único escritor)
```

### 3.3 Capas (un paquete solo importa capas inferiores)
```
0 core          types, clock, queues, errors, logging_setup        (stdlib + numpy)
1 config        schema (dataclasses) + loader TOML
2 cameras · calibration · integration
3 detection     (solo píxeles; no conoce calibración)
4 tracking · race   (solo core.types; reciben polígonos/geom ya en mm)
5 persistence · visualization
6 app           cableado (camera_pipeline, pipeline, cli) + main.py + tools/
```

### 3.4 Árbol de carpetas (modifica la propuesta original, con justificación)
```
MultiCam_Vision/
├── main.py                       # mínimo; importable sin efectos (requisito de Windows spawn)
├── pyproject.toml / requirements.txt / README.md
├── config/  default.toml · local.toml (IPs, COM; no versionado) · cameras/cam1.toml · cameras/cam2.toml (enabled=false)
├── src/multicam_vision/
│   ├── core/          types.py · queues.py · clock.py · errors.py · logging_setup.py      [+ nuevo: evita ciclos]
│   ├── config/        schema.py · loader.py                                              [reemplaza settings.py/cameras.py]
│   ├── cameras/       sources.py · capture.py · ipwebcam.py · synchronization.py         [+ sources/ipwebcam]
│   ├── calibration/   camera_calibration.py · homography.py · model.py · store.py · panorama.py (solo visual)
│   ├── detection/     base.py · background.py · aruco.py · motion.py · vehicles.py · color.py
│   ├── tracking/      kalman.py · association.py · tracker.py · trajectories.py · fusion.py
│   ├── integration/   events.py · rfid.py · esp32.py
│   ├── race/          track_model.py · ranking.py · engine.py                             [+ lógica de carrera]
│   ├── persistence/   database.py · recorder.py                                           [+ BD separada de la web]
│   ├── visualization/ overlay.py · web/ (Flask + SSE, migra flassk.py)
│   └── app/           camera_pipeline.py · pipeline.py · cli.py
├── firmware/esp32/               # sketch Arduino (protocolo NDJSON v1)
├── tools/                        # calibrate_intrinsics, calibrate_track, record, replay, bench, measure_latency
├── data/  calib/ · track/track_layout.json · recordings/ · db/
├── logs/  app.log · metrics.jsonl
└── tests/ test_architecture.py · unit/ · integration/ · data/
```

### 3.5 Contratos principales (resumen; firmas completas en `informes_agentes/AGENTE_01_ARQUITECTURA.md §6`)
```python
@dataclass(frozen=True, slots=True)
class Frame:            cam_id: str; seq: int; t_capture_ns: int; t_received_ns: int; image: np.ndarray
@dataclass(frozen=True, slots=True)
class Detection:        cam_id: str; t_ns: int; bbox: tuple; centroid_px: tuple; footprint_px: tuple
                        marker_id: int | None; height_mm: float; confidence: float; method: str; flags: frozenset
@dataclass(frozen=True, slots=True)
class Observation:      cam_id: str; t_ns: int; xy_mm: tuple; cov_mm2: tuple; vehicle_hint: int | None; confidence: float
@dataclass(frozen=True, slots=True)
class Track:            track_id: int; vehicle_id: int | None; state: TrackState; t_ns: int
                        xy_mm: tuple; v_mm_s: tuple; s_mm: float; laps: int
@dataclass(frozen=True, slots=True)
class RfidEvent:        lane: int; uid: str; t_device_ms: int; t_host_ns: int; seq: int; vehicle_id: int | None

class FrameSource(Protocol):  def open(self) -> None; def read(self) -> tuple[bool, np.ndarray | None, int]; def close(self) -> None
class VehicleDetector(Protocol): def detect(self, frame: Frame) -> list[Detection]
class CameraModel:  def pixel_to_world(self, pts_px: np.ndarray, z_mm: float = 0.0) -> np.ndarray
class MultiCameraTracker: def update(self, obs: list[Observation], t_ns: int) -> list[Track]
class EventBus:     def publish(self, ev) -> None; def subscribe(self, kind, maxsize=256) -> DropQueue
class Esp32Link:    def start(self) -> None; def send(self, msg: dict) -> None; def stop(self) -> None
```

### 3.6 Protocolo ESP32 ↔ PC (NDJSON v1, una línea JSON por mensaje)
```
ESP32→PC  {"v":1,"type":"boot","seq":0,"ms":12,"fw":"1.0"}
          {"v":1,"type":"rfid","seq":42,"ms":123456,"lane":2,"uid":"04A1B2C3"}
          {"v":1,"type":"hb","seq":43,"ms":124000,"readers":[1,1,1]}
          {"v":1,"type":"pong","seq":44,"ms":124010,"id":7}
PC→ESP32  {"v":1,"type":"ping","id":7}
          {"v":1,"type":"light","state":"red|yellow|green|off"}     (semáforo / LED tricolor)
          {"v":1,"type":"led_sync","period_ms":1037}                 (calibración de latencia de cámaras)
```

---

## 4. Propiedad de archivos (un archivo = un dueño)

| Agente | Archivos |
|---|---|
| 01 Arquitectura | `core/*`, `config/*` (schema/loader/default.toml), `app/pipeline.py`, `app/cli.py`, `main.py`, `pyproject.toml`, `requirements.txt`, `tests/test_architecture.py`, `README.md` (estructura) |
| 02 Multicámara | `cameras/*`, `config/cameras/*.toml`, `persistence/recorder.py`, `tools/record.py`, `tools/measure_latency.py` |
| 03 Visión | `detection/*`, `app/camera_pipeline.py` (la llamada a `pixel_to_world` la define 05) |
| 04 Tracking | `tracking/*`, `race/track_model.py`, `race/ranking.py` |
| 05 Calibración | `calibration/*`, `data/calib/*`, `data/track/track_layout.json`, `tools/calibrate_*.py` |
| 06 Integración y testing | `integration/*`, `firmware/esp32/*`, `race/engine.py`, `persistence/database.py`, `visualization/*`, `tests/integration/*`, `tests/conftest.py`, `tools/bench.py` |

`core/types.py` y `config/schema.py` son **contratos congelados**: solo cambian con una decisión registrada en `REGISTRO_CENTRAL.md`.

---

## 5. Informe de viabilidad

**Veredicto: VIABLE CON CONDICIONES.** La arquitectura y los algoritmos se sostienen en simulación con margen (tracking ~17–19 mm RMS, ranking correcto en ~99,3 % de frames, 0,2–0,3 % duplicados en solape, CPU holgada). **La viabilidad real depende de condiciones físicas de montaje que hoy NO se cumplen** según la captura analizada:

| Condición | Estado actual | Por qué es necesaria |
|---|---|---|
| Cámaras elevadas (≥1 m, idealmente casi cenitales) y fijas | Cámara baja y oblicua | Con la vista actual: <0,4 px/mm en la zona lejana, paralaje 57–215 mm, oclusiones entre autos. |
| Imagen nítida | Varianza de Laplaciano 2,8: **ningún marcador decodificable**, ni ampliando ×2/×3 | Sin nitidez no hay identidad ni calibración precisa. |
| Iluminación fuerte y difusa (permite exposición corta) | Tira LED lateral, sombras duras | Exposición ~1/30 s ⇒ blur que impide leer ArUco a velocidad. |
| Resolución, zoom y foco fijos | Variaban entre pruebas (1080p, 960×720, 640×480) | La calibración solo vale para una resolución/zoom; modos 16:9 recortan el sensor 4:3. |
| IP fija para cada teléfono (reserva DHCP) | Cambió .103 → .109 → .110 | Reconexión automática a una IP estable. |
| Segunda cámara configurada | No configurada | El sistema arranca con `cam2.enabled=false`; la fusión se valida al habilitarla. |
| pyserial instalado en la PC | No verificado | Necesario para el ESP32. |

**Costo de CPU estimado (sintético, Xeon 2 vCPU):** por cámara a 960×720 ≈ decodificar 1,7–2,8 ms + fondo estático 3,0 ms + ArUco en ROI (≪9,7 ms del frame completo) + tracking 0,24 ms ⇒ cabe con holgura en 30 fps por cámara. **El Ryzen 5700X debería sobrar, pero no está medido.**

---

## 6. Riesgos (ordenados por impacto)

| ID | Riesgo | Prob. | Impacto | Mitigación propuesta | Cómo se verifica |
|---|---|---|---|---|---|
| R1 | **Motion blur impide identificar autos por ArUco a velocidad** (<0,22 m/s legible con 1/30 s) | Alta | Alto | Identidad la sostiene el tracker; ArUco solo cuando el auto va lento; **RFID en la meta re-etiqueta cada vuelta por UID**; más luz; probar exposición corta en IP Webcam; marcador 50 mm sobre placa blanca 60 mm; opción: parche de color único por auto | Clip real a varias velocidades: % de lecturas ArUco y cambios de ID |
| R2 | Nitidez/montaje actual insuficiente | Alta (hoy) | Alto | Elevar cámaras, fijar foco, luz difusa, resolución 960×720 4:3 | Laplaciano > umbral y detección de marcadores fijos ≥95 % |
| R3 | RC522 no lee a >2–3 m/s (ciclo de 3 lectores ~81 ms con timeout por defecto) | Media | Medio | Timeout 1–2 ms, round-robin no bloqueante, etiqueta bajo el auto a ≤2 cm del lector, visión como respaldo del cruce | Banco: pasar cada auto 20 veces a velocidad creciente |
| R4 | RST compartido (D17) causa fallas entre lectores | Media | Medio | Un solo pulso RST al inicio + soft reset por lector (**hipótesis**) | Banco con los 3 lectores |
| R5 | Latencia/jitter Wi-Fi y ancho de banda (2×960×720@30 MJPEG ≈ hasta ~21 Mbit/s c/u en peor caso) | Media | Medio | Router 5 GHz o calidad JPEG/FPS menores, último-frame, offsets medidos con LED | `tools/measure_latency.py` en la red real |
| R6 | Autos pegados/adelantamientos fusionan blobs | Media | Medio | Predicción del tracker + flags `merged` + ArUco; 0,3 % frames mal etiquetados en simulación | Clips con adelantamientos |
| R7 | Teléfono cambia zoom/recorte/foco tras reinicio | Media | Alto | Verificar `/status.json` al conectar; vigilar marcadores fijos (alarma >1 px por 2 s ⇒ recalibrar) | Test de arranque |
| R8 | Reflejos sobre pintura negra (esmalte sintético brilla) | Media | Medio | Pintura/terminación mate, luz difusa, máscaras de exclusión | Fondo estático con pista vacía y luz encendida |
| R9 | Chapadur no plana (combada) | Baja-media | Medio | Fijar la lámina; validar error en mm en varios puntos | Residuos de homografía por zona |
| R10 | Anomalía 1/7 en detección de corte con backend FFmpeg | Baja | Bajo | Usar lector propio por defecto | Test de reconexión |

---

## 7. Plan de implementación propuesto (por fases, cada una con criterio de aceptación)

| Fase | Contenido | Criterio para avanzar |
|---|---|---|
| **F0 — Validación física (vos + scripts)** | Ejecutar `00_inspeccion_entorno.py`; montar cámaras elevadas; grabar clips de referencia con ambos teléfonos (pista vacía, cada auto lento y rápido, adelantamiento) | Versiones reales registradas; clips grabados; imagen nítida |
| F1 — Esqueleto | `core`, `config`, test de arquitectura, logging/métricas | Tests verdes; sin ciclos |
| F2 — Captura | Lector MJPEG, reconexión, grabación/replay, medición de latencia | 2 cámaras independientes; corte de una no afecta a la otra (probado) |
| F3 — Calibración | ChArUco intrínseca, marcadores fijos, pose + H, validación cruzada en solape | Error en mm medido sobre puntos de control < objetivo acordado |
| F4 — Detección | Fondo estático color + ArUco en ROI + footprint | % detección / FP medidos sobre clips reales |
| F5 — Tracking + carrera | Tracker único en mm, progreso, vueltas, ranking | Métricas sobre clips grabados (ID switches, duplicados) |
| F6 — ESP32/RFID | Firmware NDJSON, Esp32Link, EventBus, reconciliación vuelta RFID/visión | Banco + prueba de no bloqueo del video |
| F7 — Web/BD + pruebas reales | Flask SSE, SQLite, prueba integral en pista | Informe de rendimiento real desde `metrics.jsonl` |

---

## 8. Decisiones que necesito que apruebes o corrijas

1. La arquitectura y el árbol de carpetas de la sección 3 (incluye los cambios justificados sobre la estructura original).
2. Identidad de autos por **tracker + ArUco ocasional + RFID en la meta** (R1), en lugar de depender solo de ArUco.
3. Detección por **fondo estático en color** (no MOG2) como método principal.
4. **TOML** para configuración y **SSE** en lugar de Flask-SocketIO.
5. **USB serial** como enlace principal con el ESP32.
6. Empezar por la **Fase F0** (validación física y grabación de clips reales) antes de escribir los módulos.

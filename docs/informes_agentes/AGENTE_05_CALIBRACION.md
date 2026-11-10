# AGENTE 05 — CALIBRACIÓN (MULTICAM VISION, Autódromo 1:24)

> Fase: **solo propuesta técnica**. Los números de este informe salen de **experimentos sintéticos** ejecutados en el workspace (Python 3.13, OpenCV 5.0.0, NumPy 2.x). **No** se midieron con las cámaras reales del usuario.
> Scripts y salidas: `/tmp/claude-0/-home-claude/0ca3fc1e-d1b0-54d9-9f08-b03b5d26a0a9/scratchpad/agente05/` (`exp_a_charuco.py`, `exp_a_salida.txt`, `exp_b_homografia.py`, `exp_b_salida.txt`).

---

## 1. Objetivo

Que cada cámara pueda convertir un píxel en una posición **en milímetros sobre el plano de la pista** (un único sistema de coordenadas común), con un error conocido y comprobable. Concretamente:

1. Calibración intrínseca (K) y corrección de distorsión (dist) de cada teléfono con IP Webcam.
2. Homografía imagen→pista (mm) de cada cámara a partir de marcadores ArUco **fijos** con posición medida.
3. Usar la zona compartida para **validar** la coherencia entre cámaras (error cruzado en mm).
4. Cuantificar y corregir el **paralaje** del marcador sobre el techo del auto (h ≈ 45 mm).
5. Evaluar stitching panorámico frente a warp al plano de la pista.
6. Detectar descalibración (cámara movida o con zoom cambiado) y persistir la calibración de forma reproducible.

## 2. Investigación realizada (con fuentes)

### 2.1 ChArUco frente a tablero de ajedrez
- La documentación de OpenCV ([Calibration with ArUco and ChArUco, OpenCV 5.0](https://docs.opencv.org/5.0/tutorials/objdetect/aruco_calibration/aruco_calibration.html), [4.x](https://docs.opencv.org/4.x/da/d13/tutorial_aruco_calibration.html)) dice dos cosas:
  - ChArUco admite oclusiones y vistas parciales: no hace falta que todas las esquinas sean visibles.
  - Las esquinas ChArUco son más precisas que las esquinas de marcadores, por eso se recomienda ChArUco antes que un ArUco board.
- La API moderna (≥4.7, también en 5.x) es `cv2.aruco.CharucoBoard`, `cv2.aruco.CharucoDetector.detectBoard()`, `board.matchImagePoints()` y `cv2.calibrateCamera()`. La antigua `calibrateCameraCharuco` dejó de estar disponible en Python ([foro OpenCV, 4.7](https://forum.opencv.org/t/how-to-use-calibratecameracharuco-with-opencv-4-7-and-python/12594), [foro OpenCV, 4.10](https://forum.opencv.org/t/aruco-module-essential-functions-not-implemented-in-python-in-opencv-4-10-0/18949)). En el workspace con OpenCV 5.0.0 verifiqué que existen `cv2.aruco.CharucoBoard`, `cv2.aruco.CharucoDetector`, `board.matchImagePoints` y `board.generateImage`.
- Tutorial de detección: [Detection of ChArUco Boards (5.0)](https://docs.opencv.org/5.0/tutorials/objdetect/charuco_detection/charuco_detection.html).

### 2.2 Homografía robusta
- `cv2.findHomography` admite 0 (mínimos cuadrados), RANSAC, LMEDS, RHO y los métodos USAC ([USAC tutorial OpenCV 5.0](https://docs.opencv.org/5.0/tutorials/calib3d/usac.html), [4.x](https://docs.opencv.org/4.x/de/d3e/tutorial_usac.html), [foro: USAC_MAGSAC con findHomography](https://forum.opencv.org/t/can-i-use-usac-magsac-with-cv2-findhomography/12383)).
- El umbral de RANSAC se expresa en las unidades del **espacio destino**.
- En mis pruebas, `USAC_MAGSAC` falló en ~80 % de los casos con OpenCV 5.0.0 (ver §4.2). Lo descarto.

### 2.3 Stitching
- `cv2.Stitcher` ([High level stitching API, OpenCV 5.0](https://docs.opencv.org/5.0/tutorials/others/stitcher.html), [4.4](https://docs.opencv.org/4.4.0/d8/d19/tutorial_stitcher.html)) tiene dos modos:
  - `PANORAMA`: modelo de rotación pura con proyección esférica o cilíndrica.
  - `SCANS`: modelo afín, pensado para escaneos.
- Ninguno de los dos produce coordenadas métricas. Estima por features en cada llamada, así que el resultado no es estable de un cuadro a otro. Además, los dos teléfonos **no comparten centro óptico** (no es rotación pura).

### 2.4 Teléfono como cámara de medición
- La estabilización electrónica u óptica (EIS/OIS) desplaza o recorta la imagen de forma dinámica ([Android Authority: image stabilization](https://www.androidauthority.com/image-stabilization-1087083/), [Lumenci: software video stabilization](https://www.lumenci.com/post/software-based-video-stabilization-in-smartphones)). Eso invalida una calibración fija.
- El contexto común indica que el J7 Prime **recorta** el sensor 4:3 en los modos 16:9.
- Conclusión: K y dist son válidas **solo** para la combinación exacta resolución + relación de aspecto + zoom + (estabilización desactivada). Esa combinación debe quedar escrita en el archivo de calibración y verificarse al arrancar.

### 2.5 Captura real observada (`haruko_captura.jpg`)
- Vista muy oblicua y baja: el horizonte de baldosas converge con fuerza y el fondo de la escena ocupa pocos píxeles.
- Tira LED lateral con reflejo especular y sombras duras.
- Auto oscuro con marcador en el techo en el borde derecho.
- Implicancia: la resolución en mm/px en la zona lejana es mala y el paralaje del techo es grande. Los experimentos cuantifican ambas cosas.

## 3. Solución propuesta

### 3.1 Principio de diseño
**Cada cámara se mapea de forma independiente al plano de la pista (mm).** No se calcula cámara→cámara. Razones:

1. **Coordenada común física.** El tracking, la velocidad y la deduplicación necesitan mm, no píxeles de otra cámara.
2. **Errores independientes.** Si la cámara B se mueve, solo se recalibra B; la calibración de A sigue siendo válida. Con cámara→cámara, un error de B contamina a A.
3. **Escalable a N cámaras** sin cadenas de homografías, que acumulan error.
4. **La zona compartida** se usa para **validar**, no para definir la geometría: los mismos marcadores de referencia, vistos por A y B, deben caer en los mismos mm.

### 3.2 Calibración intrínseca (por cámara, una vez por configuración)
- **Tablero recomendado: ChArUco** 7×5 cuadros, 40 mm de cuadro, marcador de 30 mm, diccionario **DICT_5X5_100**. Es distinto del DICT_4X4_50 de los autos y de las referencias, para que no haya confusiones. Se imprime en A3 o en A4 a escala 100 % y se pega sobre una superficie rígida. Hay que **medir el cuadro impreso con calibre** y usar el valor medido.
- **Por qué ChArUco y no ajedrez, aunque el ajedrez dio algo más de precisión en el ensayo sintético ideal** (§4.1):
  - Acepta vistas parciales, así que el usuario puede llevar el tablero hasta las esquinas de la imagen, que es donde la distorsión es mayor.
  - Cada esquina tiene un ID, así que no hay ambigüedades de orientación.
  - Es robusto a reflejos parciales de la tira LED.
  - En el ensayo, el ajedrez descartó 2–3 de 15 vistas; ChArUco usó 14–15.
  - El ajedrez con `findChessboardCornersSB` queda como **alternativa válida**.
- **Modelo:** `k1, k2, p1, p2` con `CALIB_FIX_K3` (la distorsión del teléfono es baja). Se usan 15–25 vistas.
- **Criterio de aceptación:**
  - RMS < 0.5 px en imagen real (en el sintético dio 0.06–0.07 px; en la realidad, con compresión MJPEG, se espera más).
  - Validación cruzada: calibrar con el 80 % de las vistas y medir el error de reproyección en el 20 % restante.
- **¿Hace falta con un teléfono?** Sí. Con distorsión típica de teléfono (supuesta: k1=−0.085, k2=0.06) el desplazamiento llega a 19 px en las esquinas. Ignorarla multiplicó el error en pista por ~5–10 (§4.2: media 4.2 mm y máximo 40.6 mm, frente a 0.43 mm y 3.8 mm). Los coeficientes reales del J7 Prime están **pendientes de medir**.

### 3.3 Homografía piso→mundo (por cámara)
- **Marcadores de referencia fijos:**
  - ArUco DICT_4X4_50, **IDs 20–49** (los autos usan 1–3; los IDs 4–19 quedan reservados).
  - Lado de 50 mm en la zona cercana y **60–80 mm en la zona lejana**. Con la cámara a 1 m, la zona lejana tiene 0.36 px/mm, así que un marcador de 50 mm mide unos 18 px, en el límite de detección.
  - Se pegan **planos** sobre la chapadur, fuera de la trayectoria de los autos (bordes, centro e isla) y con **al menos 4–6 en la zona compartida**.
  - Las 4 esquinas de cada uno se miden en mm desde un origen fijo de la pista, con cinta y escuadra o plantilla impresa. Objetivo: ±1 mm.
- **Estimación** (es la mejor variante medida, ver §4.2):
  1. Sin distorsión: `cv2.undistortPoints(px, K, dist, P=K)`.
  2. Se estima **mundo→imagen** (umbral en px, donde el ruido es homogéneo) con `cv2.findHomography(world_mm, px_undist, cv2.RANSAC, 4.0)`.
  3. **Reajuste por mínimos cuadrados** (`method=0`) solo sobre los inliers.
  4. Se invierte para obtener `H_img→mundo`.
- **Promediado temporal de esquinas:** como las referencias están quietas, se promedian las esquinas de N≈30–60 cuadros antes de estimar. Hay que hacerlo con la cámara bien sujeta.
- **Mínimo:** 6 marcadores (24 puntos) bien distribuidos por cámara. Con menos de 4 marcadores, se rechaza.

### 3.4 Paralaje (marcador en el techo, h ≈ 45 mm)
- La homografía solo vale para Z=0. Un punto a altura h se proyecta en el piso desplazado **alejándose de la cámara**:

  `Δ = h · d / (H_c − h)`

  donde `d` es la distancia horizontal entre el pie de la cámara y el punto, y `H_c` la altura de la cámara. La simulación coincide con la fórmula con un error < 0.001 mm.
- **El error es grande:** 33–125 mm con la cámara a 1 m (§4.3), del orden de medio auto.
- **El problema más serio es la deduplicación.** El mismo techo visto por A y por B difiere en **`h·baseline/(H_c−h)`**, que es **constante** en toda la zona compartida: 61 mm con la cámara a 1 m y 1.3 m de separación entre cámaras. El tracker no debe fusionar con un umbral menor que eso si no se corrige.
- **Corrección propuesta (por defecto):**
  - **Homografía del plano Z=h.** Con K, dist y la pose (R, t) estimada por `cv2.solvePnP` sobre las mismas referencias:

    `H_h = K·[r1, r2, h·r3 + t]`, y se usa `H_h⁻¹`.

  - Resultado medido: error medio 0.85 mm y p95 2.1 mm.
  - `image_to_world(..., height_mm=45)` aplica esta corrección.
  - h se mide con calibre en cada auto (sobre la superficie del marcador). Un error de +5 mm en h provoca un error medio de 8 mm.
- **Alternativa:** corrección radial respecto del pie de cámara, `p = C_xy + (p0 − C_xy)·(H_c − h)/H_c`. Es más simple, pero dio peor resultado (media 2.1 mm).
- **Otra alternativa:** usar el punto de contacto con el piso (base del blob del auto). No necesita h, pero en vista oblicua ese punto es ambiguo y queda tapado por la carrocería. No se recomienda como fuente primaria; sirve para validar.

### 3.5 Stitching panorámico: solo visualización
- `cv2.Stitcher`: **descartado para medir**. No da mm, re-estima por features en cada llamada, el centro óptico no es común, y la pista negra con líneas tiene pocas features.
- **Panorama propuesto:** warp de cada cámara al plano de la pista con `M_vis = S·H_img→mundo` (S = escala, por ejemplo 0.5 px/mm → lienzo de 1200×600) usando `cv2.warpPerspective`. En el solape se aplica blending con un peso proporcional a la resolución local de cada cámara (px/mm) o un feathering simple.
- **Resultado:** es un "mapa cenital" coherente con las coordenadas de medición.
- **Advertencia:** los objetos con altura (autos) aparecen "estirados" y duplicados en el solape por el paralaje. Eso es **esperado** y **no** afecta la medición, que se hace por detección en cada cámara → `image_to_world` → fusión en mm.
- **Coste:** los mapas de `warpPerspective` se precalculan con `cv2.initUndistortRectifyMap` + composición, o con un único `cv2.remap` por cámara.

### 3.6 Detección de descalibración
- En cada cuadro (o cada N cuadros), se detectan las referencias visibles y se calcula el **residuo en px** entre las esquinas detectadas y `H⁻¹(posición en mm)`.
- **Alarma** si la mediana del residuo es > 1.0 px durante más de 2 s, o si desaparecen más del 50 % de las referencias esperadas sin oclusión plausible.
- Medido (§4.4): un giro de **0.1°** desplaza las esquinas 1.4 px y causa un error medio de 5.8 mm en la pista. Con 0.25° son 3.5 px y 14.5 mm.
- El umbral de 1 px separa el ruido (~0.5 px) de un desplazamiento relevante (≥0.1°).
- **Respuesta:**
  1. Se marca la cámara como `DEGRADED` y sus detecciones se emiten con `calib_ok=False`. El tracker puede ignorarlas o darles menos peso.
  2. Se ofrece **re-estimar H en caliente** con las referencias visibles (K y dist no cambian si la configuración es la misma).
  3. Si el residuo tras re-estimar sigue alto (zoom o recorte cambiados), se exige recalibración intrínseca.
- Al arrancar se verifica también `/status.json` de IP Webcam: resolución, zoom, estabilización y enfoque contra el archivo de calibración. Si no coinciden, se rechaza la calibración.

### 3.7 Persistencia
Se guarda un archivo JSON por cámara, versionado, en `config/calibration/<cam_id>.json`:
```json
{
  "schema": 1, "cam_id": "cam_a", "created": "2026-11-08T15:30:00-03:00",
  "device": {"model": "SM-G610M", "app": "IP Webcam", "video_size": [960, 720], "zoom": 1.0,
             "stabilization": false, "focus_mode": "fixed/infinity"},
  "intrinsics": {"K": [[...],[...],[...]], "dist": [k1,k2,p1,p2,0], "rms_px": 0.31,
                 "n_views": 22, "board": "charuco_7x5_40mm_30mm_DICT_5X5_100", "holdout_rms_px": 0.35},
  "plane": {"H_img2world": [[...],[...],[...]], "rvec": [...], "tvec": [...],
            "ref_ids": [20,21,...], "rms_mm": 0.9, "rms_px": 0.42, "n_frames_avg": 60},
  "world": {"origin": "esquina SO de la pista", "units": "mm", "track_mm": [2400, 1200],
            "ref_markers_file": "config/reference_markers.json"},
  "validation": {"cross_cam_mean_mm": 1.3, "cross_cam_p95_mm": 3.0}
}
```
- `reference_markers.json` guarda `{id: [[x,y],[x,y],[x,y],[x,y]] mm}` y es compartido por todas las cámaras.
- Uso JSON y no YAML: está en la biblioteca estándar, no exige dependencias y es diffable. `cv2.FileStorage` (YAML) queda como opción de exportación.

## 4. Resultados medidos (sintéticos)

### 4.1 Experimento A: calibración intrínseca (`exp_a_charuco.py`)
**Configuración:**
- Cámara virtual de 960×720, fx=780, fy=781, cx=482, cy=357, dist=[−0.085, 0.060, 0.0006, −0.0004, 0]. La distorsión real produce un desplazamiento medio de 5.97 px y **máximo de 19.2 px**.
- 15 poses a 350–650 mm con inclinación de hasta ±38°; las últimas 4 están parcialmente fuera de cuadro.
- Render exacto por rayo con supersampling 3×3, desenfoque σ=0.8 px, ruido σ=3 niveles de gris y ganancia variable.

**Resultados (3 semillas):**

| Tablero | Vistas usadas | RMS reproy. | Error fx | \|Δc\| | Residuo de distorsión no absorbible por H (media / máx) |
|---|---|---|---|---|---|
| ChArUco | 15 / 14 / 15 | 0.059 / 0.069 / 0.070 px | 0.06 / 0.03 / 0.11 % | 1.17 / 0.63 / 0.17 px | 0.04 / 0.21 px; 0.03 / 0.19 px; 0.04 / 0.19 px |
| Ajedrez SB | 12 / 13 / 12 | 0.031 / 0.045 / 0.037 px | 0.006 / 0.02 / 0.04 % | 0.24 / 0.58 / 0.16 px | 0.02 / 0.05 px; 0.02 / 0.14 px; 0.035 / 0.22 px |

- Los coeficientes recuperados (ChArUco, semilla 0) fueron k1 = −0.0850 y k2 = 0.0606 (verdaderos: −0.085 y 0.060).
- **Lectura:** ambos métodos recuperan el modelo con un residuo de distorsión < 0.25 px. Es despreciable frente al ruido de detección.
- En condiciones ideales el ajedrez SB es algo más preciso. ChArUco usa más vistas, incluidas las parciales.
- La diferencia práctica la marcarán las condiciones reales (reflejos, vistas en esquinas). Eso queda **pendiente de medir**.
- La primera ejecución sin supersampling (con aliasing) dio un RMS de ChArUco de 0.22 px: **la calidad de imagen domina**.

### 4.2 Experimento B1: homografía piso→mundo (`exp_b_homografia.py`)
**Configuración:**
- Pista de 2400×1200 mm.
- Dos cámaras a 700 mm del borde largo, en X=550 y X=1850, apuntando a cada mitad.
- 21 marcadores de referencia de 50 mm (grilla 7×3); cada cámara ve 14–18.
- 300 ensayos Monte Carlo. Error evaluado en una grilla de 49×25 puntos de la pista visibles por la cámara.

**Error en mm**, cámara A. "Cruzado" es la distancia A↔B del mismo punto del piso en la zona compartida:

| Altura cámara | Resolución piso (cerca/lejos) | Ruido esquinas | Colocación | Media | p95 | Máx | Cruzado A-B media / p95 |
|---|---|---|---|---|---|---|---|
| 600 mm | 0.80 / 0.39 px/mm | 0.5 px | 0 | 0.60 | 1.61 | 7.5 | 0.92 / 2.21 |
| 600 mm | | 1.0 px | 1 mm | 1.34 | 3.51 | 14.9 | 2.00 / 4.82 |
| 1000 mm | 0.63 / 0.36 px/mm | 0.5 px | 0 | 0.43 | 1.03 | 3.8 | 0.66 / 1.47 |
| 1000 mm | | 1.0 px | 0 | 0.87 | 2.09 | 7.5 | 1.35 / 3.05 |
| 1000 mm | | 1.0 px | 1 mm | 0.91 | 2.09 | 6.6 | 1.39 / 3.05 |
| 1600 mm | 0.45 / 0.31 px/mm | 0.5 px | 0 | 0.45 | 1.00 | 2.9 | 0.65 / 1.38 |
| 1600 mm | | 1.0 px | 1 mm | 0.93 | 2.10 | 7.0 | 1.38 / 3.00 |

- **Sin corregir distorsión** (1000 mm, 0.5 px): media 4.19 mm, p95 9.6 mm, máximo 40.6 mm, cruzado 6.1 mm (p95 14.5 mm).
- **Con corrección:** 0.43 mm de media. La corrección de distorsión es necesaria.
- Los máximos se dan en las **esquinas lejanas o fuera del polígono de referencias** (extrapolación). Las referencias deben cubrir toda la zona útil de cada cámara.
- La cámara a 600 mm (tan baja como en la captura real) cubre el 75 % de la pista y duplica los máximos.

**Método de estimación** (1000 mm, ruido 1 px + 1 mm, 300 ensayos; error medio en mm de A):

| Método | Sin atípicos | 2 marcadores atípicos (+40 mm) |
|---|---|---|
| LSQ img→mundo | 1.00 | **5.90** |
| RANSAC img→mundo, umbral 3 mm | 2.59 | 2.79 |
| RANSAC img→mundo, umbral 10 mm | 1.63 | 1.79 |
| LSQ mundo→img (inv) | 0.89 | **6.42** |
| RANSAC mundo→img, 2 px | 1.94 | 2.06 |
| RANSAC mundo→img, 4 px | 1.14 | 1.27 |
| **RANSAC 4 px + reajuste LSQ inliers** | **0.92** | **1.13** |
| USAC_MAGSAC mundo→img, 2 px | falló 244/300 | falló 224/300 |

- **Hallazgo:** el RANSAC de OpenCV con un umbral ajustado **empeora** la precisión (descarta inliers buenos de la zona lejana). Mínimos cuadrados puro no tolera atípicos.
- La variante en dos etapas iguala a LSQ sin atípicos y es robusta con ellos.
- USAC_MAGSAC devolvió `None` en la mayoría de los casos con esta configuración en OpenCV 5.0.0. Lo descarto.

### 4.3 Experimento B2: paralaje con h = 45 mm

| Altura cámara | Elevación | Error sin corregir (media / mín / máx) | Desacuerdo A-B del mismo techo |
|---|---|---|---|
| 600 mm | 13–41° | 124.9 / 56.8 / 215.0 mm | 105.4 mm (constante) |
| 1000 mm | 21–55° | 72.5 / 33.0 / 125.0 mm | 61.3 mm (constante) |
| 1600 mm | 31–66° | 45.3 / 20.3 / 76.7 mm | 37.6 mm (constante) |

La fórmula `h·d/(H_c−h)` reproduce la simulación con un error < 0.001 mm.

**Corrección** (1000 mm, ruido 1 px + 1 mm, 300 ensayos):

| Método | Media | p95 | Máx |
|---|---|---|---|
| Sin corrección | 72.5 mm | 107.2 | 143.8 |
| **H del plano Z=h (pose por solvePnP)** | **0.85 mm** | **2.07** | 9.7 |
| Corrección radial al pie de cámara | 2.07 mm | 5.02 | 24.3 |
| Radial con h errónea (+5 mm) | 8.25 mm | 13.9 | 33.2 |

- Desacuerdo A-B del mismo techo tras corregir: media 3.0 mm, p95 7.0 mm.
- Centro óptico estimado por solvePnP: error medio 1.9 mm (p95 3.5 mm).

### 4.4 Experimento B3: sensibilidad a un giro de la cámara (H vieja)

| Giro (pitch) | Desplazamiento de las referencias en imagen | Error en pista (media / máx) |
|---|---|---|
| 0.10° | 1.39 px | 5.8 / 12.0 mm |
| 0.25° | 3.47 px | 14.5 / 30.2 mm |
| 0.50° | 6.95 px | 29.2 / 61.0 mm |
| 1.00° | 13.9 px | 59.3 / 124.6 mm |

## 5. Tecnologías utilizadas
- **OpenCV 5.0.0** (`cv2.aruco` CharucoBoard/CharucoDetector/ArucoDetector, `calibrateCamera`, `undistortPoints`, `findHomography`, `solvePnP`, `perspectiveTransform`, `warpPerspective`/`remap`).
- **NumPy.**
- **JSON** de la biblioteca estándar.
- Sin GPU y sin redes neuronales: todo es CPU y de coste trivial. Una vez calibrado, `image_to_world` cuesta microsegundos por punto.

## 6. Archivos que necesita crear
```
multicam_vision/calibration/
  camera_model.py        # CameraModel, carga/guarda JSON, undistort_points
  camera_calibration.py  # calibración intrínseca ChArUco (y ajedrez SB como fallback), CLI
  reference_markers.py   # carga reference_markers.json, detección de referencias, promediado temporal
  homography.py          # PlaneCalibration, estimate_plane(), H del plano z=h, image_to_world
  validation.py          # error cruzado en zona compartida, monitor de descalibración (CalibrationMonitor)
  panorama.py            # warp al plano de pista + blending (solo visualización)
  tools/print_board.py   # genera PNG/PDF del ChArUco y de marcadores de referencia 20-49 a escala
config/calibration/<cam_id>.json
config/reference_markers.json
tests/test_calibration_synthetic.py   # versiones pytest de exp_a / exp_b con tolerancias
```

## 7. Interfaces que necesita

```python
from dataclasses import dataclass, field
from enum import Enum
import numpy as np

@dataclass(frozen=True)
class CaptureConfig:
    video_size: tuple[int, int]          # (w, h) p.ej. (960, 720)
    zoom: float = 1.0
    stabilization: bool = False
    focus_mode: str = "fixed"

@dataclass
class CameraModel:
    cam_id: str
    image_size: tuple[int, int]          # (w, h) con el que se calibró
    K: np.ndarray                        # (3,3) float64
    dist: np.ndarray                     # (5,) k1 k2 p1 p2 k3
    rms_px: float
    holdout_rms_px: float | None
    n_views: int
    capture: CaptureConfig
    created: str                         # ISO 8601
    board_desc: str

    def undistort_points(self, pts_px: np.ndarray) -> np.ndarray:
        """(N,2) px crudos -> (N,2) px ideales (P=K). Usa cv2.undistortPoints."""
    def matches(self, size: tuple[int, int], capture: CaptureConfig) -> bool:
        """False si resolución/zoom/estabilización difieren -> calibración inválida."""

@dataclass
class PlaneCalibration:
    cam_id: str
    H_img2world: np.ndarray              # (3,3) px ideales -> mm (Z=0)
    rvec: np.ndarray                     # (3,) pose mundo->cámara (solvePnP)
    tvec: np.ndarray                     # (3,)
    ref_ids: list[int]
    rms_mm: float
    rms_px: float
    n_frames_avg: int
    created: str

    def H_for_height(self, cam: CameraModel, height_mm: float) -> np.ndarray:
        """H_img->mundo para el plano Z=height_mm: inv(K @ [r1, r2, h*r3 + t])."""
    def camera_center_mm(self) -> np.ndarray:
        """(3,) centro óptico en coordenadas de pista."""

@dataclass
class CameraCalibration:
    model: CameraModel
    plane: PlaneCalibration

def calibrate_intrinsics_charuco(frames: list[np.ndarray], board_cfg: dict, cam_id: str,
                                 capture: CaptureConfig, holdout_frac: float = 0.2) -> CameraModel: ...

def estimate_plane(cam: CameraModel, detections: dict[int, np.ndarray],   # id -> (4,2) px promediados
                   ref_world_mm: dict[int, np.ndarray],                   # id -> (4,2) mm
                   ransac_px: float = 4.0, min_markers: int = 6) -> PlaneCalibration:
    """undistort -> findHomography(mundo->img, RANSAC 4px) -> reajuste LSQ inliers -> inv; solvePnP para rvec/tvec."""

def image_to_world(cam_id: str, pts_px: np.ndarray, height_mm: float = 0.0) -> np.ndarray:
    """(N,2) px crudos de la cámara cam_id -> (N,2) mm en el plano de pista.
    height_mm>0 aplica corrección de paralaje (plano Z=h). Usa el registro global de calibraciones."""

def world_to_image(cam_id: str, pts_mm: np.ndarray, height_mm: float = 0.0) -> np.ndarray: ...

@dataclass
class CrossCheckResult:
    shared_ids: list[int]
    mean_mm: float
    p95_mm: float
    max_mm: float
    per_marker_mm: dict[int, float]

def cross_camera_check(cal_a: CameraCalibration, cal_b: CameraCalibration,
                       det_a: dict[int, np.ndarray], det_b: dict[int, np.ndarray]) -> CrossCheckResult: ...

class CalibState(Enum):
    OK = "ok"; DEGRADED = "degraded"; INVALID = "invalid"

class CalibrationMonitor:
    def __init__(self, cal: CameraCalibration, ref_world_mm: dict[int, np.ndarray],
                 thr_px: float = 1.0, hold_s: float = 2.0): ...
    def update(self, ts: float, detections: dict[int, np.ndarray]) -> CalibState: ...
    def try_refit_plane(self) -> PlaneCalibration | None: ...

def save_calibration(cal: CameraCalibration, path: str) -> None: ...
def load_calibration(path: str) -> CameraCalibration: ...

def build_topdown_maps(cal: CameraCalibration, px_per_mm: float,
                       track_mm: tuple[float, float]) -> tuple[np.ndarray, np.ndarray]:
    """mapas (map_x, map_y) para cv2.remap: vista cenital (solo visualización)."""
```

## 8. Procedimiento de calibración para el usuario (paso a paso)

**A. Preparación (una vez)**
1. **Teléfonos.** En IP Webcam fijar 960×720 (4:3, sin recorte), **zoom 1×**, **estabilización de video desactivada** y enfoque fijo o infinito si existe. Poner exposición y balance de blancos fijos si la app lo permite. Anotar la configuración: el sistema la comparará con `/status.json`.
2. **Red.** Reservar la IP de cada teléfono en el router (DHCP estático); la IP ya cambió entre pruebas (.103 → .110).
3. **Tablero.** Imprimir el ChArUco (`tools/print_board.py`) al 100 %, pegarlo en una tabla rígida y plana, y **medir el cuadro con calibre**. Introducir la medida real en mm.
4. **Referencias.** Imprimir los marcadores ArUco 20–49 (DICT_4X4_50): 50 mm en la zona cercana y 60–80 mm en la lejana. Pegarlos planos en la pista fuera del trazado de los autos, con al menos 4–6 visibles por **ambas** cámaras en la zona compartida. Medir las 4 esquinas de cada uno en mm desde el origen de la pista (esquina marcada) y cargarlas en `reference_markers.json`.
5. **Montaje.** Fijar los teléfonos a un soporte rígido, **lo más alto posible** (≥1 m) y lo más cenital posible. En el ensayo, la cámara a 600 mm duplicó los errores máximos y triplicó el paralaje frente a 1600 mm.

**B. Calibración intrínseca (por teléfono; solo se repite si cambia resolución, zoom o teléfono)**
6. Ejecutar `python -m multicam_vision.calibration.camera_calibration --cam cam_a`.
7. Mover el tablero frente a la cámara: cerca y lejos, inclinado ±30–40°, **cubriendo las cuatro esquinas y los bordes de la imagen**. La herramienta captura automáticamente cuando el tablero está quieto y la vista es nueva. Objetivo: 20–25 vistas.
8. Revisar el resultado: RMS < 0.5 px y RMS de validación con las vistas reservadas similar. Si no se cumple, repetir con mejor iluminación y sin reflejos.

**C. Calibración de plano (por cámara; repetir cada vez que se mueve la cámara)**
9. Despejar la pista (sin autos ni manos). Ejecutar `--plane --cam cam_a`. Se promedian las referencias durante ~2 s (60 cuadros).
10. Aceptar si: ≥6 referencias detectadas, RMS < 1 px y < 3 mm, y ningún marcador rechazado por RANSAC. Si se rechaza un marcador, **revisar su medición**.
11. Repetir para cam_b.
12. **Validación cruzada:** el sistema muestra el error A↔B de las referencias compartidas. Objetivo: media < 3 mm. Si es mayor, revisar las mediciones de los marcadores compartidos.
13. **Validación física:** colocar un auto con marcador en 3–5 posiciones medidas con cinta (incluida la zona compartida). Verificar que `image_to_world(..., height_mm=h_auto)` coincide dentro de ±5 mm en ambas cámaras.
14. Guardar. El JSON queda con fecha, RMS y configuración.

**D. Operación**
15. El monitor vigila las referencias. Ante una alarma de descalibración: primero se intenta re-estimar el plano automáticamente; si no alcanza, se repite el paso C (y el B si cambió el zoom o la resolución).

## 9. Riesgos y limitaciones
- **Todo lo medido es sintético:** distorsión supuesta, ruido gaussiano, plano perfecto. Faltan en el modelo:
  - artefactos MJPEG;
  - motion blur;
  - rolling shutter (los autos rápidos se deforman);
  - planitud real de la chapadur (una lámina combada es otro "plano", con error directo en mm);
  - errores sistemáticos de medición con cinta.
- **Cámara baja como en la captura actual:** zona lejana con < 0.4 px/mm (1 px ≈ 2.5–3 mm), paralaje de 57–215 mm y oclusiones entre autos. Recomiendo fuertemente elevar las cámaras.
- **El teléfono puede cambiar zoom, recorte o estabilización** tras una actualización o reinicio de la app. Mitigación: verificar `/status.json` al iniciar y vigilar las referencias en línea.
- **Autoenfoque:** en algunos teléfonos, cambiar el foco altera ligeramente la focal ("focus breathing"). Hay que fijar el foco si la app lo permite. Pendiente de verificar en el J7 Prime.
- **Altura h del marcador:** un error de 5 mm en h produce ~8 mm de error en pista (con la cámara a 1 m). Además, h varía si el auto se inclina en curva.
- **USAC_MAGSAC inestable** en OpenCV 5.0.0 en esta configuración (falló en ~80 % de los ensayos). No usarlo sin verificar. El RANSAC de OpenCV con umbral ajustado degrada la precisión: usar el esquema de dos etapas.
- **Marcadores de referencia dañados o movidos por los autos:** pegarlos fuera del trazado y protegerlos (laminado mate para evitar reflejos de la tira LED).
- **Panorama:** muestra los autos duplicados o estirados en el solape. Es esperable y no debe interpretarse como error de medición.

## 10. Pruebas necesarias
1. **Unitarias sintéticas** (pytest, versión reducida de exp_a/exp_b):
   - recuperación de K con un error < 0.5 %;
   - error de plano < 1.5 mm de media con 1 px;
   - paralaje corregido < 2 mm de media;
   - redondeo JSON sin pérdida (save → load → mismo resultado);
   - `CalibrationMonitor` dispara con un giro de 0.25° y no dispara con ruido de 0.5 px.
2. **Con el teléfono real, intrínseca:**
   - RMS y RMS de validación con las vistas reservadas;
   - repetibilidad: 3 calibraciones independientes; dispersión de fx < 0.5 % y de los coeficientes de distorsión reportada;
   - ChArUco frente a ajedrez SB con las mismas condiciones de luz.
3. **Estabilidad temporal:** dispersión de las esquinas de las referencias con la cámara quieta durante 60 s (ruido real en px), tanto en MJPEG a 960×720 como a 1920×1080.
4. **Plano real:** error en puntos de control medidos que **no** se usaron para estimar H (validación cruzada dejando un marcador fuera).
5. **Coherencia A↔B real** en la zona compartida, con referencias en el piso y con un auto a altura h.
6. **Descalibración provocada:** golpear o mover ligeramente el soporte y verificar alarma y re-estimación.
7. **Reinicio de la app o cambio de resolución:** verificar que la calibración se rechaza.
8. **Planitud de la chapadur:** error en puntos de control del centro frente a los bordes.

## 11. Estado del trabajo

| Elemento | Estado |
|---|---|
| Investigación (ChArUco, findHomography/RANSAC/USAC, Stitcher, estabilización) | Implementado (documentado) |
| Exp. A: calibración ChArUco vs ajedrez con cámara virtual | **Probado (sintético)** |
| Exp. B1: error de H piso→mundo, efecto de la distorsión, comparación de métodos | **Probado (sintético)** |
| Exp. B2: paralaje h=45 mm y su corrección (plano Z=h, radial) | **Probado (sintético)** |
| Exp. B3: sensibilidad a giros y umbral de alarma | **Probado (sintético)** |
| Diseño de interfaces y persistencia JSON | Parcial (propuesta, sin código de producción) |
| Módulos `calibration/*` del sistema | Pendiente (fase de propuesta) |
| Calibración con los teléfonos reales, error real en mm | Pendiente |
| Verificación de `/status.json` (zoom, estabilización) en el J7 Prime | Pendiente |

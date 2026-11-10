# AGENTE 03 — VISIÓN ARTIFICIAL · Propuesta técnica

> Fase: **solo propuesta**. No se implementó el módulo `detection/`. Sí se ejecutaron experimentos **sintéticos** y una prueba sobre la **captura real** del usuario. Todos los números que siguen son **medidos** en el workspace en la nube (Intel Xeon 2.3 GHz, 2 vCPU, OpenCV 5.0.0, Python 3.13). **No** son mediciones en el Ryzen 7 5700X ni con las cámaras reales salvo donde se indica "captura real".
>
> Scripts y resultados crudos: `/tmp/claude-0/-home-claude/0ca3fc1e-d1b0-54d9-9f08-b03b5d26a0a9/scratchpad/agente03/`

---

## 1. Objetivo

Definir el subsistema de detección por cámara que entregue, para cada frame, una lista de `Detection` con:

1. **Posición fiable** del auto (independiente de la velocidad y de la iluminación), expresada como **punto de contacto con el piso** en píxeles (`footprint_px`), apto para aplicar la homografía al plano de pista sin el error de paralaje del techo.
2. **Identidad** (auto 1/2/3) cuando sea observable, con confianza, sin inventarla cuando no lo es.
3. Mínimos falsos positivos (manos, personas, sombras, reflejos, cambios de luz).
4. Costo de CPU compatible con 2 cámaras a 25–30 FPS en un Ryzen 5700X sin GPU útil.

---

## 2. Investigación realizada

### 2.1 Análisis de la captura real (`haruko_captura.jpg`, 1920x1080)

Script: `exp_real.py`.

| Medida | Valor medido |
|---|---|
| Tamaño aparente del auto | ≈150x200 px (vista oblicua, borde derecho del frame) |
| Región blanca del marcador (incl. zona blanca) | ≈35x30 px, visiblemente desenfocada |
| Varianza del Laplaciano (frame completo / zona del auto) | **2.8 / 6.6** (muy baja: imagen muy blanda) |
| Energía espectral en banda baja (r<0.125 Nyquist) | 71 % |
| `detectMarkers` default / ajustado, frame completo | **0 detecciones** (6 / 135 candidatos rechazados) |
| `detectMarkers` en ROI del auto reescalada x2, x3 (cúbica) | **0 detecciones** |

Conclusión sobre la captura real: el marcador ocupa ~30 px, que en imagen nítida alcanzaría (ver 2.2), pero **la imagen carece de detalle de alta frecuencia** (desenfoque, compresión MJPEG y/o reescalado de un modo 16:9 recortado). Reescalar no recupera información. El problema de esa captura es **nitidez**, no parámetros del detector. Además: vista muy oblicua, tira LED lateral con sombras duras y un objeto oscuro grande en primer plano (mochila) que un sustractor de fondo detectaría como intruso.

### 2.2 Experimento A — Tasa de detección ArUco DICT_4X4_50 (sintético)

Script: `exp_aruco.py` (resultados: `aruco_results.json`), `summarize.py`.

Render: marcador + zona blanca de 1 celda sobre techo oscuro y pista negra, supermuestreado 4x y reducido con `INTER_AREA` (integración de sensor), rotación aleatoria, inclinación (escorzo `cos(tilt)` + leve perspectiva), blur lineal de longitud L (dirección aleatoria), ruido σ=3, JPEG q=60/90. 40 imágenes por condición, 400 condiciones, 16 000 imágenes. "Lado px" = lado del marcador (sin zona blanca) en el eje no escorzado. Éxito = ID correcto y error medio de esquinas < máx(2 px, 0.5·lado).

**Tasa de detección (%) con parámetros `default`, JPEG q60** (filas: lado px; columnas: blur px)

| lado | tilt 0°: b0 / b2 / b4 / b8 | tilt 45°: b0 / b2 / b4 / b8 | tilt 60°: b0 / b2 / b4 / b8 | tilt 70°: b0 / b2 / b4 |
|---|---|---|---|---|
| 8  | 15 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 |
| 10 | 42 / 2 / 0 / 0 | 10 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 |
| 12 | 92 / 15 / 0 / 0 | 57 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 |
| 16 | 100 / 80 / 2 / 0 | 88 / 38 / 0 / 0 | 52 / 2 / 0 / 0 | 0 / 0 / 0 |
| 20 | 100 / 98 / 48 / 0 | 98 / 65 / 15 / 0 | 85 / 20 / 0 / 0 | 15 / 0 / 0 |
| 24 | 100 / 100 / 95 / 0 | 100 / 95 / 42 / 0 | 98 / 48 / 8 / 0 | 50 / 5 / 0 |
| 32 | 100 / 100 / 100 / 60 | 100 / 100 / 92 / 10 | 98 / 88 / 30 / 2 | 92 / 52 / 8 |
| 40 | 100 / 100 / 100 / 90 | 100 / 100 / 100 / 45 | 100 / 100 / 82 / 25 | 95 / 68 / 22 |

Con blur de **16 px**: **0 %** en todas las condiciones y tamaños (hasta 40 px).

**Lado mínimo para ≥95 % de detección (JPEG q60)**

| Condición | default | ajustado ("tuned") |
|---|---|---|
| tilt 0°, sin blur | 14 px | 12 px |
| tilt 0°, blur 2 px | 20 px | 20 px |
| tilt 0°, blur 4 px | 24 px | 24 px |
| tilt 45°, sin blur | 20 px | 14 px |
| tilt 45°, blur 2 px | 24 px | 24 px |
| tilt 45°, blur 4 px | 40 px | 40 px |
| tilt 60°, sin blur | 24 px | 24 px |
| tilt 60°, blur 2 px | 40 px | 40 px |
| tilt 70°, sin blur | 40 px | 32 px |
| cualquier tilt, blur 8 px | >40 px | >40 px |

**Regla empírica derivada (sintética):** la detección se mantiene ≥95 % solo si `blur ≲ 0.1–0.15 · lado` con vista casi frontal, y `blur ≲ 0.05 · lado` a 60° de inclinación. El escorzo (tilt) y el blur se combinan multiplicativamente: lo que falla primero es la **resolución por celda** (4x4 bits + borde = 6 celdas por lado).

**Precisión de esquinas** (mediana, sin blur, JPEG q90): sin refinamiento ≈0.6–0.8 px en todos los tamaños; con `CORNER_REFINE_SUBPIX` ≈0.45–0.5 px (14–28 px) y ≈0.25 px (32–40 px) en vista frontal; 0.5–0.9 px a 60°.

**IDs falsos** (ID decodificado distinto del verdadero), 16 000 imágenes:

| Parámetros | Recall (grilla reducida, 2 880 img) | IDs falsos |
|---|---|---|
| `default` | 49.3 % | **0** (1 en 16 000 en la grilla grande) |
| `tuned` (estilo `app2.py`: + margen 0.2, `polygonalApproxAccuracyRate` 0.05, `maxErroneousBitsInBorderRate` 0.5) | 53.9 % | **26 (0.90 %)**; 143 en 16 000 |
| `safe` (tuned con bits de borde 0.35 y `errorCorrectionRate` 0) | 53.9 % | 17 (0.59 %) |
| `minimal` (solo perímetro mínimo, distancia a borde, Aruco3 off, SUBPIX) | 49.3 % | 0 |
| **`recommended`** (minimal + `adaptiveThreshWinSizeStep` 4) | **51.8 %** | **0** |

Hallazgo: la tolerancia agresiva del `app2.py` agrega ~2 pp de recall a costa de ~1 % de IDs falsos. Con un diccionario de solo 16 bits (distancia mínima del diccionario = 4, medida), un marcador borroso puede decodificarse como **otro código válido**. Se recomienda `recommended` + lista blanca + votación temporal.

**Distancia de Hamming entre IDs** (`exp_ids.py`, considerando las 4 rotaciones): los IDs actuales {1,2,3} tienen distancia mínima mutua **7**; la mejor terna del diccionario es {0,1,2} con **8**. Mejora marginal: se recomienda **mantener {1,2,3}** por compatibilidad con el código existente.

### 2.3 Experimento B — Presupuesto de motion blur (cálculo, no medición)

`blur_px = v · t_exp / (mm/px)`. Supuestos: cámara cubre ~2.4 m de pista en el ancho del frame → 2.5 mm/px a 960 px, 1.25 mm/px a 1920 px. Velocidades 1:24: 1–5 m/s.

| v | t_exp | Desplazamiento | blur @960 (2.5 mm/px) | blur @1920 (1.25 mm/px) |
|---|---|---|---|---|
| 1 m/s | 1/30 s | 33 mm | 13 px | 27 px |
| 1 m/s | 1/120 s | 8 mm | 3 px | 7 px |
| 3 m/s | 1/120 s | 25 mm | 10 px | 20 px |
| 3 m/s | 1/500 s | 6 mm | 2.4 px | 4.8 px |
| 5 m/s | 1/500 s | 10 mm | 4 px | 8 px |

Como blur y lado del marcador escalan igual con la resolución, lo que importa es `v·t_exp / lado_mm`. Para un marcador de **50 mm** y el umbral sintético `blur ≤ 0.15·lado` (vista frontal) → `v·t_exp ≤ 7.5 mm`:
- con 1/30 s (típico de un teléfono en interior): **v ≤ 0.22 m/s**;
- con 1/120 s: v ≤ 0.9 m/s;
- con 1/500 s (requiere mucha luz): v ≤ 3.75 m/s.

**Conclusión:** con el Galaxy J7 Prime en interior, **ArUco no puede ser la fuente de posición a velocidad de carrera**. Sirve para identidad con el auto detenido o lento (grilla de largada, boxes, curvas lentas, cruce de meta si se ilumina fuerte). No verificado: si IP Webcam en el J7 Prime permite fijar el tiempo de exposición (en la doc solo consta compensación de exposición); debe medirse en la PC del usuario (prueba 7.2).

### 2.4 Experimento C — Segmentación por fondo (sintético)

Script: `exp_segment.py` (`segment_results.json`). Escena 960x720, pista negra (nivel 28) con líneas blancas, 3 autos 80x32 px (≈200x80 mm) a ~2 m/s (27 px/frame), exposición = ½ frame (blur), sombra proyectada (x0.55), deriva de ganancia ±8 %, **caída de luz −25 % en el frame 150**, intruso oscuro grande en frames 220–260, ruido σ=3, JPEG q70. Auto 1 **negro** (peor caso sobre pista negra), auto 2 rojo oscuro, auto 3 azul; cada uno con placa de techo blanca con borde de color. 270 frames evaluados.

| Método | Recall | FP/frame | Error centroide mediana / p95 | Recall por auto (1/2/3) | Recall tras caída de luz |
|---|---|---|---|---|---|
| MOG2 (BGR, shadows on, solo fg=255) | 99.9 % | 0.004 | 1.04 / 6.42 px | 100/100/100 | 100 % |
| KNN (BGR, shadows on) | 100 % | 0.000 | 0.72 / 1.43 px | 100/100/100 | 100 % |
| Fondo estático **gris**, sin normalizar | 58.1 % | 0.637 | 0.80 / 1.65 px | 44/95/35 | 31 % |
| Fondo estático gris + normalización de ganancia | 61.0 % | 0.000 | 0.79 / 1.65 px | 50/100/33 | 37 % |
| **Fondo estático COLOR (máx. canal) + norm. + test de sombra** | **100 %** | **0.000** | 0.96 / 1.49 px | 100/100/100 | 100 % |
| Parche HSV (identidad por color de borde) | 53.7 % | 0.459 | 0.90 / 9.34 px | 54/100/7 | 57 % |

Hallazgos:
- **En gris, un auto negro o azul oscuro sobre chapadur negro casi no contrasta** (azul (120,40,30) → gris ≈46 vs pista 28). La diferencia **por canal (máximo de |ΔB|,|ΔG|,|ΔR|)** lo resuelve en esta simulación. La **placa blanca del techo** es, además, el objeto de mayor contraste de toda la escena: es la que garantiza el blob aunque la carrocería sea negra.
- La normalización global de ganancia (mediana del cociente frame/fondo) eliminó los FP producidos por el cambio de luz (0.637 → 0 FP/frame).
- El intruso grande se descartó por **filtro de área máxima** (6000 px²) y elongación; sin compuertas de forma, sería FP.
- El parche HSV falló por dos motivos medidos/diagnosticados: (a) colisión de tono entre el borde de color y la carrocería (auto rojo/azul), (b) bandas finas (5 px) destruidas por blur y por el submuestreo de croma 4:2:0 del JPEG. **El color sirve solo como parche grande y uniforme**, clasificado dentro del blob, nunca como búsqueda global.

### 2.5 Experimento D — Auto detenido: ¿cuándo lo "absorbe" el fondo? (sintético)

Script: `exp_stopped.py`. Auto quieto tras 300 frames de pista vacía; se mide cuántos frames pasan hasta que menos del 50 % de su cuerpo queda como foreground.

| Sustractor | learningRate | Frames hasta perderlo | A 30 FPS |
|---|---|---|---|
| MOG2 history=300 | auto (−1) | **32** | **1.1 s** |
| MOG2 history=500 | auto | 55 | 1.8 s |
| MOG2 (300 o 500) | 0.001 | 110 | 3.7 s |
| MOG2 | 0 (congelado) | nunca | — |
| KNN history=300 | auto | 57 | 1.9 s |

**Hallazgo crítico:** MOG2/KNN con parámetros por defecto **pierden un auto detenido en 1–2 s** (despiste, choque, boxes, largada). En una carrera esto es inaceptable. El fondo debe ser un **modelo estático de la pista vacía**, actualizado **solo fuera de los autos rastreados** (actualización selectiva con máscara).

### 2.6 Experimento E — Costo de CPU por frame (sintético, 2 vCPU Xeon 2.3 GHz)

Scripts: `exp_timing.py` (`timing_results.json`), `exp_timing_rec.py`, `exp_timing_static.py`. Mediana (p95) en ms, 30 frames con 3 autos con marcador.

| Operación | 960x720, 2 hilos | 960x720, 1 hilo | 1920x1080, 2 hilos | 1920x1080, 1 hilo |
|---|---|---|---|---|
| `imdecode` JPEG q80 (costo MJPEG) | 1.69 (1.80) | 1.69 | 4.33 (4.50) | 4.89 |
| `cvtColor` BGR→GRAY | 0.23 | 0.46 | 0.49 | 0.92 |
| ArUco `detectMarkers` default (frame completo) | 2.44–2.79 | 3.57–3.90 | 6.39–6.71 | 10.0–10.35 |
| ArUco `recommended` (frame completo) | 9.70 (11.4) | 17.69 | 12.08 (14.9) | 21.0 |
| ArUco `tuned` (frame completo) | 10.6–12.5 | 17.8–18.2 | 11.7–12.7 | 18.8–19.3 |
| ArUco `tuned` en 3 ROIs | 7.36 (10.5) | 10.82 | 11.74 | 19.41 |
| MOG2 BGR, shadows on | 8.49 (10.3) | 17.21 | 28.47 (36.4) | 56.49 |
| MOG2 BGR, shadows off | 8.71 | 17.65 | 29.04 | 57.09 |
| MOG2 gris | 4.04 | 14.51 | 22.82 | 40.20 |
| MOG2 BGR a media resolución | 1.51 | 2.41 | 7.92 | 13.95 |
| KNN BGR, shadows on | 9.69 (**26.3**) | 18.22 (52.7) | 32.34 (**88.0**) | 56.45 (150) |
| Fondo estático gris (absdiff+umbral) | 0.13 | 0.13 | 0.54 | 0.65 |
| **Fondo estático COLOR + norm. + sombra** | **3.04** | 2.40 | **6.79** | 7.47 |
| Morfología open+close + contornos | 0.96 | 0.97 | 3.67 | 3.23 |
| HSV + 3 `inRange` | 1.85 | 1.97 | 4.96 | 6.01 |
| `warpPerspective` frame completo | 1.95 | 3.83 | 6.45 | 11.59 |

Observaciones: KNN tiene **p95 2.7–3x su mediana** (latencia irregular). MOG2 a 1080p cuesta ~28–57 ms: no entra en presupuesto de 33 ms por cámara en 1 hilo. El fondo estático en color cuesta ~3 ms a 960x720. En ROIs, ArUco ajustado ahorra ~40 % (no 90 %, por overhead fijo por llamada).

### 2.7 Experimento F — Detectores neuronales livianos (no medido)

No se midió: el workspace no tiene modelos YOLO ni red/instalación de paquetes para obtenerlos. Referencia publicada: YOLOv8n a 640 px en CPU AMD EPYC 7V12 (64 núcleos, Docker): **76.9 ms/img con ONNX** y **31.7 ms/img con OpenVINO** ([Ultralytics community benchmark](https://community.ultralytics.com/t/unofficial-benchmark-results-how-fast-can-you-yolo/59)). En el 5700X no está verificado; del orden de decenas de ms por frame y por cámara. Requeriría dataset propio etiquetado (cientos de imágenes de la pista real), y **no resuelve la identidad** (los tres autos serían la misma clase).

### 2.8 Fuentes

- OpenCV `cv::aruco::DetectorParameters` (5.x): https://docs.opencv.org/5.x/d1/dcd/structcv_1_1aruco_1_1DetectorParameters.html y (4.x) https://docs.opencv.org/4.x/d1/dcd/structcv_1_1aruco_1_1DetectorParameters.html
- OpenCV `BackgroundSubtractorMOG2` (5.0): https://docs.opencv.org/5.0/main_modules/classcv_1_1BackgroundSubtractorMOG2.html ; (3.2) https://docs.opencv.org/3.2.0/d7/d7b/classcv_1_1BackgroundSubtractorMOG2.html (shadow value 127, `shadowThreshold`).
- Foro OpenCV, marcadores chicos en imagen grande (desactivar Aruco3; escalar `minMarkerPerimeterRate`, `minCornerDistanceRate`, `minMarkerDistanceRate`): https://forum.opencv.org/t/aruco-detecting-small-markers-in-a-large-image/16063/2
- OpenCV Q&A, patrones para detección a baja resolución: https://answers.opencv.org/question/68812/best-patternmethod-for-low-resolution-marker-detection
- Benchmark no oficial YOLO (CPU EPYC, ONNX/OpenVINO): https://community.ultralytics.com/t/unofficial-benchmark-results-how-fast-can-you-yolo/59
- Marcadores en cámaras de eventos (motivación: el blur es la falla principal de fiduciales binarios con movimiento): https://arxiv.org/pdf/2012.06516
- DeepTag (marco general de detección de fiduciales, comparación de robustez): https://arxiv.org/pdf/2105.13731

---

## 3. Solución propuesta

### 3.1 Comparación de métodos (con evidencia de §2)

| Criterio | ArUco 4x4 | MOG2/KNN | Fondo estático (color) | Color HSV | YOLO-n ONNX |
|---|---|---|---|---|---|
| Posición a velocidad (1–5 m/s) | ✗ (blur ≥8 px → ~0 %) | ✓ | ✓ | ~ (solo parche grande) | ✓ (no medido) |
| Auto detenido | ✓ | ✗ (perdido en 1–2 s) | ✓ | ✓ | ✓ |
| Identidad de cada auto | ✓✓ (lista blanca) | ✗ | ✗ | ~ (frágil, medido 54 %) | ✗ |
| Falsos positivos | muy bajos con params default/recommended | bajos con compuertas | bajos con compuertas + normalización | altos si hay colisión de tono | dependen del dataset |
| Sombras / cambios de luz | robusto (umbral adaptativo) | sombra=127 descartable; se adapta | requiere normalización + test sombra (medido OK) | sensible a balance de blancos | robusto si entrenado |
| CPU 960x720 (2 hilos) | 2.4–12 ms | 8.5 ms (1.5 a ½ res) | 3.0 ms | 1.9 ms | ~30–80 ms (ref. externa) |
| Datos de entrenamiento | no | no | no (1 captura de pista vacía) | calibración de colores | sí (cientos de imágenes) |
| Precisión centroide (sint.) | esquinas 0.25–0.9 px | 1.0 px med., 6.4 p95 | 0.96 px med., 1.5 p95 | 0.9 / 9.3 px | n/d |

### 3.2 Estrategia híbrida recomendada

**"Blob para posición, código para identidad, tracker para continuidad".**

```
frame BGR (decodificado por cameras/) ──► [máscara de pista (ROI poligonal de calibración)]
   │
   ├─► A. FOREGROUND (cada frame, resolución de trabajo 960x720 o ½ res para 1080p)
   │      fondo estático de pista vacía (BGR, float32)
   │      · normalización de ganancia global: ratio = mediana(frame/fondo) en grilla submuestreada
   │      · d = máx_canal |frame − ratio·fondo| > T (T≈25, calibrable)
   │      · test de sombra: cociente de luminancia ∈ [0.4, 0.85] y d < 60 → sombra (excluir)
   │      · morfología open 5x5 + close 5x5 x2
   │      · contornos externos → compuertas: área ∈ [A_min(y), A_max(y)], elongación < 5,
   │        solidez, dentro de máscara de pista. A_min/A_max dependen de la fila y (perspectiva),
   │        derivados de la homografía y del tamaño real 200x80 mm.
   │      · actualización selectiva del fondo: cv2.accumulateWeighted(frame, bg, α≈0.002, mask=~(fg ∪ tracks dilatados))
   │
   ├─► B. IDENTIDAD (no en todos los frames)
   │      ArUco `recommended` SOLO en ROIs alrededor de cada blob (1.5x bbox), y solo cuando:
   │        velocidad estimada del track baja (blur esperado < 0.1·lado_px), o track sin ID confirmado,
   │        o cada N frames. Lista blanca {1,2,3}. Un ID se ACEPTA tras ≥3 lecturas coincidentes
   │        en ≤10 frames (votación); un ID contradictorio aislado se ignora.
   │      Color (opcional, secundario): clasificación del parche de techo DENTRO del blob en Lab (a*,b*)
   │        contra colores de referencia calibrados por sesión; sirve a velocidad porque un parche
   │        grande y uniforme sobrevive al blur, a diferencia de un código binario.
   │
   └─► C. FOOTPRINT: punto de contacto con el piso (ver 3.3) → `Detection.footprint_px`
         └─► tracking/ (Agente 04) aplica homografía, asocia, mantiene ID entre lecturas ArUco.
```

Justificación:
- **Posición** con fondo estático en color: 100 % recall, 0 FP/frame en el sintético, incluyendo auto negro, sombras y caída de luz; ~3 ms a 960x720; **no absorbe autos detenidos** (a diferencia de MOG2/KNN, §2.5). MOG2 queda como **alternativa configurable** (`method="mog2"`) para diagnóstico y para escenas sin captura de fondo vacío, siempre con `learningRate` fijo bajo y sabiendo que pierde autos quietos.
- **Identidad** con ArUco: 0 IDs falsos con `recommended` en 16 000+2 880 imágenes, pero solo legible a baja velocidad: por eso se usa **para etiquetar tracks**, no para encontrarlos. El tracker conserva el ID entre lecturas.
- **YOLO no se justifica** en esta fase: costo ≥10x el pipeline clásico, requiere dataset, no da identidad, y los problemas observados en la captura real (nitidez, contraste, oblicuidad) no son los que resuelve una red. Queda como plan C si, con la pista negra real, el fondo estático presenta FP por reflejos que no se puedan enmascarar.
- **Diferencia en el plano rectificado** (opción 3 del pedido): **no recomendada como etapa principal**. Costo de `warpPerspective` medido 1.95–6.45 ms por frame (2 hilos), interpolación degrada bordes, y — sobre todo — el auto **no es plano**: su silueta en el plano rectificado se "estira" alejándose de la cámara (ver 3.3). Conviene segmentar en la imagen original y transformar **solo puntos** (footprint), que es O(1). Sí es útil rectificar **una vez** para construir la máscara de pista y los límites de área por fila.

### 3.3 Footprint: corrección de paralaje del techo (vista oblicua)

La homografía solo es válida para puntos sobre el plano de la pista. Un punto del techo a altura `h` se proyecta sobre el plano más lejos de la cámara. Con el centro óptico de la cámara `C = (Cx, Cy, Cz)` en coordenadas de pista (obtenible de calibración intrínseca + homografía vía `solvePnP`/descomposición), y `R'` = posición en el plano (mm) obtenida aplicando la homografía a un punto que en realidad está a altura `h`:

```
P_xy = C_xy + (R'_xy − C_xy) · (Cz − h) / Cz          (corrección exacta, modelo pinhole)
error si no se corrige = d · h / (Cz − h)
```

Ejemplo: cámara a 1.5 m de altura, auto a 2 m de distancia horizontal, techo h=45 mm → **error ≈62 mm** (casi el ancho del auto, 80 mm). Por eso `Detection` lleva `footprint_px` explícito.

Estimación propuesta de `footprint_px`, en orden de preferencia:
1. **Con ArUco leído**: centro del marcador (techo, `h_marker` conocido por auto) → plano con homografía → corrección exacta de arriba → reproyectar al píxel del piso (o entregar directamente el punto corregido; ver interfaz).
2. **Solo blob**: centroide del blob con altura efectiva `h_eff ≈ h_auto/2` (la silueta visible mezcla techo y laterales) y la misma fórmula. `h_eff` debe **calibrarse empíricamente** (prueba 7.5).
3. Si no hay pose de cámara: punto medio del borde del blob más cercano al punto nadir de la cámara (aproximación grosera; marcar `confidence` baja).

Como `detection/` trabaja en píxeles y la homografía es de `calibration/`, se propone que `detection/` reciba un `GroundProjector` (inyectado) que implemente la corrección; si no hay calibración, `footprint_px = centroide` y `footprint_corrected=False`.

### 3.4 Recomendaciones físicas (marcadores, cámara)

- **Marcador**: DICT_4X4_50, IDs {1,2,3}. Lado del cuadrado negro **50 mm** + **5–6 mm de zona blanca** a cada lado → placa blanca de **60–62 mm**, que cabe en un techo 1:24 de 60–80 mm. Imprimir mate (evitar reflejos de la tira LED), pegar plano. Más chico que 40 mm no tiene sentido: el factor limitante es px/celda.
- **Placa blanca** obligatoria aunque se use solo color: da el contraste del blob sobre chapadur negro. Si se usa color de identidad, que sea un **parche grande y uniforme** (≥20x40 mm, p. ej. el resto del techo), con tonos que **no** coincidan con ninguna carrocería ni con las líneas blancas.
- **Resolución/píxeles**: con 50 mm de marcador, 960 px sobre 2.4 m dan ~20 px (insuficiente en vista oblicua según §2.2); **1920 px sobre 2.4 m dan ~40 px**. Si se usa 1080p, preferir el **modo 4:3 nativo** (p. ej. 1440x1080 si la app lo ofrece) para no recortar sensor; segmentar a media resolución (MOG2 ½ res: 7.9 ms; fondo estático escala igual) y correr ArUco sobre ROIs a resolución completa.
- **Ángulo**: mantener la inclinación del marcador respecto del eje óptico ≤ 45–60° (§2.2: a 70° hace falta ≥32–40 px aun sin blur). Montar las cámaras lo más alto posible.
- **Exposición/luz**: iluminar fuerte y uniforme (difusa) para que el teléfono baje la exposición. Bloquear foco. Exigir en el arranque un **control de nitidez** (varianza del Laplaciano en una ROI de pista) con umbral calibrado; la captura real dio 2.8, lo que indica una imagen inutilizable para códigos.

### 3.5 Parámetros iniciales propuestos

```python
# ArUco "recommended" (0 IDs falsos en el sintético)
p = cv2.aruco.DetectorParameters()
p.minMarkerPerimeterRate = 0.005      # relativo al lado mayor de la imagen/ROI; clave en 1080p
p.minCornerDistanceRate = 0.01
p.minDistanceToBorder = 1
p.useAruco3Detection = False          # peor para marcadores chicos (foro OpenCV)
p.adaptiveThreshWinSizeMin = 3
p.adaptiveThreshWinSizeMax = 23
p.adaptiveThreshWinSizeStep = 4       # barrido fino de umbral: +2.5 pp de recall sin IDs falsos
p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
p.cornerRefinementWinSize = 2         # ventana chica: marcadores de 12–40 px
# NO subir maxErroneousBitsInBorderRate ni perspectiveRemoveIgnoredMarginPerCell (causan IDs falsos, §2.2)

# Fondo estático
FG_THRESHOLD = 25          # máx. canal, niveles 0-255
SHADOW_RATIO = (0.40, 0.85)
SHADOW_MAX_DIFF = 60
BG_ALPHA = 0.002           # actualización selectiva fuera de autos
MORPH_KERNEL = 5           # elipse; escalar con resolución
MAX_ELONGATION = 5.0
# Áreas min/max por fila: derivadas de 200x80 mm vía homografía, con margen 0.4x–2.5x
```

---

## 4. Tecnologías utilizadas

- OpenCV 5.0.0 (contrib/aruco): `ArucoDetector`, `absdiff`, `accumulateWeighted`, `morphologyEx`, `findContours`, `minAreaRect`, `connectedComponentsWithStats`, `cvtColor` (Lab/HSV), `BackgroundSubtractorMOG2` (alternativa/diagnóstico).
- NumPy 2.x.
- Sin redes neuronales en esta fase (onnxruntime disponible, no se usa).

---

## 5. Archivos que necesita crear

```
multicam_vision/detection/
  __init__.py
  types.py            # Detection, BlobCandidate, MarkerReading (dataclasses)
  background.py       # StaticBackgroundModel (captura pista vacía, normalización, sombra, update selectivo)
                      # + MOG2Foreground (alternativa configurable)
  blobs.py            # morfología, contornos, compuertas de área/forma por fila
  markers.py          # ArucoIdentifier: params, ROIs, lista blanca, lectura con pose opcional
  color_id.py         # ColorIdentifier opcional (Lab, referencias calibradas)
  footprint.py        # GroundProjector (protocolo) + corrección de paralaje
  vehicles.py         # VehicleDetector: orquesta foreground → blobs → identidad → footprint
  quality.py          # sharpness_score(), exposure stats (gate de calidad al arranque)
config/detection.yaml (o sección en settings.py): umbrales, params ArUco, IDs, alturas de techo/marcador
tests/detection/
  synth.py            # generador sintético (derivado de exp_aruco.py / exp_segment.py)
  test_markers.py, test_background.py, test_footprint.py, test_vehicles.py
tools/
  capture_background.py   # guarda fondo de pista vacía por cámara (mediana de N frames)
  bench_detection.py      # benchmark reproducible en la PC del usuario
```

`detection/motion.py` del árbol original se reemplaza por `background.py` + `blobs.py` (nombre más preciso; la "detección de movimiento" no detecta autos quietos).

---

## 6. Interfaces que necesita

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Literal, Protocol, Sequence
import numpy as np

Method = Literal["bg_static", "mog2", "aruco", "color", "fused"]

@dataclass(frozen=True, slots=True)
class Detection:
    cam_id: str                                  # id lógico de cámara ("cam0", "cam1")
    t: float                                     # timestamp de captura (time.monotonic() del frame, de cameras/)
    frame_idx: int                               # índice de frame en esa cámara
    bbox: tuple[int, int, int, int]              # x, y, w, h en px de la imagen original (no rectificada)
    centroid_px: tuple[float, float]             # centroide del blob (o del marcador si method="aruco")
    footprint_px: tuple[float, float]            # punto de CONTACTO con el piso estimado, en px; es el que
                                                 # se pasa por la homografía. NO es el centro del techo.
    footprint_corrected: bool                    # True si se aplicó corrección de paralaje con pose de cámara
    marker_id: Optional[int] = None              # ID ArUco ∈ whitelist, si se leyó en ESTE frame
    color_id: Optional[int] = None               # ID por color, si se usa
    confidence: float = 0.0                      # [0,1]; combina calidad de blob y de identidad
    method: Method = "bg_static"
    area_px: float = 0.0
    orientation_deg: Optional[float] = None      # de minAreaRect o del marcador (ambigüedad 180° sin marcador)
    marker_corners_px: Optional[np.ndarray] = None   # (4,2) float32, si hubo lectura
    footprint_cov_px: Optional[np.ndarray] = None    # (2,2) incertidumbre estimada, para el tracker
    flags: frozenset[str] = field(default_factory=frozenset)  # {"touching_border","merged","low_sharpness",...}


@dataclass(frozen=True, slots=True)
class MarkerReading:
    marker_id: int
    corners_px: np.ndarray        # (4,2)
    center_px: tuple[float, float]
    side_px: float                # lado medio en px (para estimar legibilidad)
    reproj_err_px: Optional[float] = None


class GroundProjector(Protocol):
    """Implementado por calibration/ (Agente de calibración). detection/ solo lo consume."""
    def image_to_track(self, cam_id: str, pts_px: np.ndarray, height_mm: float = 0.0) -> np.ndarray:
        """(N,2) px -> (N,2) mm en plano de pista, corrigiendo paralaje para puntos a height_mm."""
    def track_to_image(self, cam_id: str, pts_mm: np.ndarray) -> np.ndarray: ...
    def camera_center_mm(self, cam_id: str) -> Optional[tuple[float, float, float]]: ...
    def expected_car_area_px(self, cam_id: str, y_px: float) -> tuple[float, float]: ...


class ForegroundModel(Protocol):
    def apply(self, frame_bgr: np.ndarray, protect_mask: Optional[np.ndarray] = None) -> np.ndarray:
        """Devuelve máscara uint8 {0,255}. protect_mask: píxeles que NO deben aprenderse (autos rastreados)."""
    def reset(self, empty_track_bgr: np.ndarray) -> None: ...


class StaticBackgroundModel:
    def __init__(self, empty_track_bgr: np.ndarray, track_mask: np.ndarray, *,
                 threshold: int = 25, shadow_ratio: tuple[float, float] = (0.40, 0.85),
                 shadow_max_diff: int = 60, alpha: float = 0.002) -> None: ...
    def apply(self, frame_bgr: np.ndarray, protect_mask: Optional[np.ndarray] = None) -> np.ndarray: ...
    def reset(self, empty_track_bgr: np.ndarray) -> None: ...
    @property
    def last_gain(self) -> float: ...            # para diagnóstico de cambios de luz


class ArucoIdentifier:
    def __init__(self, dictionary: int = cv2.aruco.DICT_4X4_50, whitelist: Sequence[int] = (1, 2, 3),
                 params: Optional[cv2.aruco.DetectorParameters] = None) -> None: ...
    def read(self, gray: np.ndarray, rois: Sequence[tuple[int, int, int, int]] | None = None) -> list[MarkerReading]: ...


class VehicleDetector:
    def __init__(self, cam_id: str, fg: ForegroundModel, ids: ArucoIdentifier,
                 projector: Optional[GroundProjector], cfg: "DetectionConfig") -> None: ...
    def detect(self, frame_bgr: np.ndarray, t: float, frame_idx: int,
               protect_rois: Sequence[tuple[int, int, int, int]] = (),
               want_identity: Sequence[tuple[int, int, int, int]] | None = None) -> list[Detection]:
        """Sin estado de tracking: el tracker indica qué ROIs proteger del aprendizaje de fondo
        y en cuáles intentar leer identidad (p. ej. tracks lentos o sin ID)."""


def sharpness_score(gray: np.ndarray, roi: Optional[tuple[int, int, int, int]] = None) -> float:
    """Varianza del Laplaciano; gate de calidad al arrancar y en runtime."""
```

Contrato con tracking/ (Agente 04): `detection/` **no** decide IDs persistentes; entrega `marker_id` por lectura y el tracker hace la votación temporal y la fusión entre cámaras. Contrato con calibration/: `GroundProjector`.

---

## 7. Riesgos y limitaciones

1. **Todo lo medido es sintético** (salvo §2.1). Las texturas, reflejos del chapadur pintado, el MJPEG real del J7 Prime y la luz real pueden cambiar los umbrales. Ninguna cifra aquí garantiza rendimiento en la pista.
2. **Motion blur**: con exposición ~1/30 s, ArUco solo identifica autos casi quietos (<0.25 m/s, cálculo §2.3). La identidad en carrera depende del tracker y, opcionalmente, del color. Si el teléfono no permite exposición corta, la identificación a velocidad es la principal limitación del sistema.
3. **Nitidez de la captura real** (Laplaciano 2.8): con esa calidad no se decodifica ningún marcador. Es un riesgo de hardware/configuración, no de algoritmo.
4. **Reflejos especulares** sobre pintura negra (tira LED) pueden generar blobs brillantes; mitigación: pintura mate, luz difusa, máscara de exclusión, compuertas de área/forma. No probado.
5. **Autos en contacto/adelantamiento**: blobs fusionados (`flags={"merged"}`). La separación requiere el tracker (predicción) o lectura de dos marcadores en el mismo blob. No resuelto en esta propuesta.
6. **Footprint**: la corrección exacta requiere la pose de cámara (`Cz`); `h_eff` para blobs es una aproximación a calibrar.
7. **IDs falsos ArUco**: 0 con `recommended` en el sintético, pero DICT_4X4_50 tiene distancia mínima 4 → mantener lista blanca + votación.
8. **Fondo estático**: requiere capturar la pista vacía por cámara tras cada cambio de posición de cámara o de luz; la actualización selectiva puede "congelar" errores si un objeto ajeno queda quieto mucho tiempo (mitigación: comando de recaptura y alarma si un blob no-auto persiste).
9. Tiempos medidos en Xeon 2 vCPU; el 5700X debería ser más rápido, pero **no está verificado**.

---

## 8. Pruebas necesarias

### 8.1 Automatizables (CI, sintéticas — basadas en los scripts de este informe)
- `test_markers.py`: tasa ≥95 % para lado ≥24 px, tilt ≤45°, blur ≤2 px (JPEG q60); **0 IDs fuera de la lista blanca** en 2 000 imágenes de ruido/blur; error de esquinas mediano <0.6 px para lado ≥24 px.
- `test_background.py`: escenario de `exp_segment.py` → recall ≥99 %, FP/frame ≤0.01, auto negro detectado; caída de luz −25 % sin FP; auto detenido 60 s **sigue** siendo foreground (regresión del hallazgo §2.5).
- `test_footprint.py`: puntos 3D sintéticos a altura h proyectados con cámara conocida → error tras corrección <1 mm (exacto en pinhole).
- `test_vehicles.py`: intruso grande no genera `Detection`; blobs con elongación >5 descartados.

### 8.2 En la PC y pista del usuario (no ejecutables aquí)
1. **Nitidez**: `sharpness_score` en la ROI de pista en cada modo de IP Webcam (640x480, 960x720, 1280x960/1440x1080 4:3, 1920x1080 16:9) con foco bloqueado.
2. **Exposición real**: filmar un auto a velocidad conocida y medir el largo del arrastre en px → `t_exp` efectivo; probar si IP Webcam permite reducirla.
3. **Tamaño del marcador en px** en el punto más lejano de cada cámara; comparar con la tabla §2.2.
4. **Tasa ArUco real**: auto detenido en 20 posiciones/orientaciones × 3 IDs; luego a velocidad creciente (empujado/tirado a mano con velocidad medida).
5. **Footprint**: auto en posiciones medidas con regla (≥10 puntos por cámara) → error en mm con y sin corrección; ajustar `h_eff`.
6. **FP de 10 min** con pista vacía + persona moviéndose alrededor + encendido/apagado de luces.
7. `bench_detection.py` en el 5700X, 2 cámaras simultáneas, con decode MJPEG incluido.

---

## 9. Estado del trabajo

| Ítem | Estado |
|---|---|
| Investigación de métodos y fuentes | **Implementado** (este informe) |
| Experimento ArUco tamaño/tilt/blur/JPEG (16 000 + 2 880 img sintéticas) | **Probado** (sintético) |
| Experimento segmentación MOG2/KNN/fondo estático/HSV | **Probado** (sintético) |
| Experimento absorción de auto detenido | **Probado** (sintético) |
| Benchmarks CPU (960x720, 1920x1080) | **Probado** en workspace 2 vCPU; **Pendiente** en Ryzen 5700X |
| Análisis de captura real (nitidez, ArUco) | **Probado** (1 imagen) |
| Evaluación YOLO/ONNX | **Parcial** (solo referencia bibliográfica; no medido) |
| Corrección de paralaje / footprint | **Parcial** (formulada y justificada; no implementada ni medida) |
| Módulo `detection/` | **Pendiente** (fuera de alcance de esta fase) |
| Pruebas en pista real | **Pendiente** |

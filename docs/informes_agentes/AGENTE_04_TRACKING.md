# AGENTE 04 — TRACKING (MULTICAM VISION, Autódromo 1:24)

> Fase: **solo propuesta técnica**. El código de `agente04/` es un prototipo de experimento, **no** el módulo final.
> Todos los números de este informe salen de **simulación en coordenadas de mundo** (no hay video ni cámaras reales). No implican rendimiento real.

---

## 1. Objetivo

Definir el subsistema `tracking/` que, a partir de detecciones ya proyectadas al plano de la pista (mm) por las dos cámaras:

1. Mantenga **una sola lista de tracks globales** (sin duplicar autos en la zona de solape).
2. Asigne **IDs persistentes** entre cámaras y a través de pérdidas (oclusión, fallas de detección, salida/entrada de campo).
3. Gestione oclusiones con estados `TENTATIVE / CONFIRMED / LOST / DELETED` y coasting acotado.
4. Compense el **desfase temporal entre cámaras** (cada observación se procesa en su propio timestamp de captura).
5. Reconstruya trayectorias, velocidades y el **avance sobre la línea central** (longitud de arco) para ranking 1°/2°/3° y conteo de vueltas, cruzándolo con RFID como verdad de suelo en meta.

---

## 2. Investigación realizada (con fuentes)

| Tema | Conclusión aplicada | Fuente |
|---|---|---|
| SORT | Kalman de velocidad constante + Húngaro es suficiente cuando las detecciones son buenas; base simple y rápida. Sufre intercambios de ID sin apariencia. | Bewley et al., 2016, [arXiv:1602.00763](https://arxiv.org/pdf/1602.00763v1); resumen [papers.readthedocs.io](https://papers.readthedocs.io/en/latest/tracking/sort/) |
| ByteTrack | Su aporte es asociar también las detecciones de **score bajo** en una 2ª pasada (recupera oclusiones parciales). Aplicable si el detector entrega score; con 3 autos y ArUco, la 2ª pasada es opcional. | Zhang et al., ECCV 2022, [arXiv:2110.06864](https://arxiv.org/abs/2110.06864v2), [ECVA](https://www.ecva.net/papers/eccv_2022/papers_ECCV/html/315_ECCV_2022_paper.php) |
| IMM | Mezcla modelos (p. ej. crucero / maniobra) con probabilidades de Markov; estándar para blancos maniobrantes. Costo ~N_modelos. | [MathWorks – Tracking maneuvering targets](https://www.mathworks.com/help/fusion/ug/tracking-maneuvering-targets.html), [trackingIMM](https://www.mathworks.com/help/fusion/ref/trackingimm.html) |
| Medidas fuera de secuencia / sensores asíncronos | Con sensores asíncronos hay que procesar cada medida en su instante (predicción al timestamp) y, si llegan tarde, reordenar (buffer) o usar algoritmos OOSM. | [arXiv:2507.03381](https://arxiv.org/pdf/2507.03381) (fusión tardía BEV con ruido), trabajos de Mauthner et al. sobre fusión multi‑sensor asíncrona ([fusion07](https://mobile.aau.at/~welmenre/papers/mauthner-fusion07.pdf)) |
| Fusión multicámara en plano de suelo | Con homografía al plano común, la deduplicación se hace en coordenadas de mundo; los "fantasmas" aparecen cuando la proyección/tiempo no es consistente. | [Ghost pruning in overlapping multicamera (SciTePress 2014)](https://scitepress.org/Papers/2014/47413/pdf/index.html), [UC3M – correspondencia en redes con solape](https://researchportal.uc3m.es/display/act459113) |
| Asignación | `scipy.optimize.linear_sum_assignment` resuelve asignación rectangular de costo mínimo (variante Jonker‑Volgenant), apto para matrices con costos "infinitos" grandes. | [SciPy docs](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.linear_sum_assignment.html) |
| Velocidad robusta | `scipy.signal.savgol_filter(..., deriv=1, delta=dt)` da derivada suavizada por polinomio local; requiere muestreo uniforme y es no causal (latencia = media ventana). | [SciPy `_savitzky_golay.py`](https://github.com/scipy/scipy/blob/v1.9.3/scipy/signal/_savitzky_golay.py) |

### 2.1 Comparativas de diseño

**Tracking en mundo vs. por imagen.**
- *Por imagen* (un tracker por cámara y luego unir): obliga a resolver la identidad dos veces y a hacer "handover" entre cámaras; el tamaño/forma aparente cambia con la perspectiva oblicua (la cámara del usuario NO es cenital), lo que vuelve mal condicionado un modelo de movimiento en píxeles.
- *En mundo (plano de pista, mm)*: un único modelo físico (mm, mm/s), el ruido se puede expresar en mm por zona (propagando la incertidumbre de la homografía), la fusión entre cámaras es natural y el progreso sobre la pista se calcula directamente.
- **Medido** (C0 vs C2, abajo): la unión de trackers por cámara produce **~88 % de duplicados** en el solape; el tracker global en mundo, **0,2–0,3 %**.

**SORT / ByteTrack / centroid tracker.**
- *Centroid tracker* (vecino más cercano sin modelo de movimiento): sin predicción, falla con detecciones perdidas y autos a 2 m/s (66 mm por frame a 30 fps); no se recomienda.
- *SORT* (Kalman + Húngaro): es la base propuesta, pero en mundo y con gating de Mahalanobis en vez de IoU (el IoU en imagen oblicua no tiene sentido físico para fusionar cámaras).
- *ByteTrack*: se adopta la idea de **2 pasadas** solo si el detector (Agente de detección) entrega score; queda como extensión (no probada).

**Modelo de movimiento (CV vs CA vs IMM).** Medido abajo: CV ≈ IMM en todas las métricas, IMM cuesta ~3× CPU; CA es peor (más intercambios y más ruido de velocidad, el término de aceleración amplifica ruido a 30 fps).

---

## 3. Solución propuesta

### 3.1 Flujo

```
cámara A ─┐   (detección + homografía + Σ en mm, por agente de calibración/detección)
          ├─► WorldDetection(t_captura corregido) ─► ReorderBuffer (100 ms) ─► GlobalTracker.update(dets, t)
cámara B ─┘                                                              │
                                                                         ├─► TrajectoryStore (posición, v, fuente)
                                                                         ├─► TrackProgress (s sobre línea central, vueltas, ranking)
RFID (ESP32) ──────────────────────────────► LapReconciler ◄─────────────┘
```

1. **Fusión en el plano común antes del tracking, como lista única de tracks globales.** Cada frame de cada cámara se procesa en **su propio timestamp** contra la misma lista de tracks (fusión centralizada secuencial). La deduplicación ocurre porque las dos observaciones de un mismo auto se asocian al mismo track; además:
   - supresión de nacimientos a < 90 mm de un track CONFIRMED/LOST;
   - fusión de tracks duplicados (< 60 mm y velocidades compatibles, sin IDs ArUco contradictorios).
   - Se probó también la variante "pre‑fusionar pares de frames A/B y luego trackear" (C5): con timestamps correctos rinde igual, pero introduce el error de tratar como simultáneos frames separados hasta 16 ms y es más compleja; **se recomienda la secuencial**.
2. **Predicción al timestamp de cada observación** + **buffer de reordenamiento de 100 ms** (≥ desfase máximo esperado): la salida se publica con 100 ms de latencia fija, a cambio de procesar todo en orden temporal (sin algoritmos OOSM). Para un tablero de carrera 100 ms es irrelevante.
3. **Calibración del desfase entre cámaras** (`ClockOffsetCalibrator`): en lote, con datos de la zona de solape y marcadores ArUco iguales en ambas cámaras: recta por las detecciones de A (±150 ms) y residuo de B → `δ_i = −(r·v)/|v|²`, `δ = mediana`. Se guarda en la configuración y se re‑estima periódicamente (monitor de deriva). El estimador **online por gradiente** que probé (C7) **no alcanza**: deja error de 2–24 ms y 11–31 % de duplicados en el solape.
4. **Asociación**: Húngaro (`linear_sum_assignment`) sobre costo `d²_Mahalanobis + ln|S|`, gate χ²(2 gl, 99,9 %) = 13,82 y tope euclídeo 450 mm. Penalización +2 a tracks tentativos.
5. **Identidad fuerte (ArUco / color, solo 3 autos)**:
   - Cada track acumula votos de marcador; `car_id` se fija con ≥ 2 votos y ≥ 70 % del total.
   - En la matriz de costos: detección con marcador ≠ `car_id` del track → **inadmisible**; igual → bonus −6; marcador perteneciente a otro track vivo → +8.
   - **ID público = `car_id`** (1, 2, 3): si un track nuevo obtiene el marcador de un track LOST, el viejo cede la identidad y se elimina (persistencia entre pérdidas y entre cámaras).
   - Revocación si la evidencia acumulada contradice la etiqueta (votos del otro ID ≥ 2× + 3).
6. **Ciclo de vida**: TENTATIVE → CONFIRMED con 3 aciertos; TENTATIVE sin actualización 150 ms → DELETED; CONFIRMED sin actualización 100 ms → LOST (sigue publicándose como predicción, marcado); LOST > 1,0 s → DELETED. Valores a ajustar con video real.
7. **Filtro**: Kalman **CV** con ruido blanco de aceleración `q = 1e6 mm²/s³` (barrido: mejor que 4e6…2e8), actualización en forma de Joseph. IMM(CV 5e5 / CV 3e7) queda como opción configurable, no por defecto.
8. **Velocidad**: para tiempo real, la del Kalman; para registro/BD y análisis, **Savitzky‑Golay** (ventana 11, orden 2, `deriv=1`) sobre las posiciones filtradas re‑muestreadas a 30 Hz (latencia 5 frames ≈ 167 ms). Nunca diferencias finitas crudas.
9. **Progreso sobre la pista**: polilínea central cerrada (medida en la calibración, p. ej. puntos cada 10 mm sobre la línea blanca central en mm), parametrizada por longitud de arco `s ∈ [0, L)`, `s = 0` en la línea de meta. Proyección con ventana de búsqueda ±800 mm alrededor del `s` previo (evita saltos entre rectas paralelas), desenrollado `prog += wrap(s − s_prev)` → vueltas = `floor(prog/L)`, ranking = orden descendente de `prog`.
10. **Cruce con RFID**: el lector RC522 de cada carril es la **verdad de suelo del cruce de meta**; la visión da el instante estimado. `LapReconciler` empareja cada evento RFID (tag → `car_id` por tabla) con el cruce visual del mismo auto en una ventana de ±300 ms (el error visual medido máx. ≈ 175 ms con IDs correctos; 465 ms en el escenario de autos pegados). Reglas: RFID sin cruce visual → cuenta la vuelta y corrige `prog` al múltiplo de L; cruce visual sin RFID → vuelta "provisional" que se confirma si el siguiente RFID es coherente; RFID nunca bloquea el tracking (cola asíncrona). Esto también re‑sincroniza el desenrollado si un auto estuvo perdido > L/2 / v ≈ 3 s.

---

## 4. Tecnologías utilizadas

- Python 3.13, **NumPy 2.5.3**, **SciPy 1.18.1** (`optimize.linear_sum_assignment`, `signal.savgol_filter`) — verificados en el workspace.
- Sin OpenCV en el tracking (solo recibe mm). Sin GPU, sin redes neuronales.
- `heapq` para el buffer de reordenamiento; `dataclasses` / `enum`.

---

## 5. Experimento realizado (simulación) — SOLO LO MEDIDO

### 5.1 Configuración del simulador (`agente04/sim.py`)
- Pista ovalada: rectas 3000 mm, radio 900 mm, **L = 11 655 mm**; 3 carriles a −150/0/+150 mm (escenario "stress": −100/0/+100 mm, autos a ~20 mm de distancia lateral).
- 3 autos, velocidad base 1,7/1,85/2,0 m/s (±5 %), frenado en curva −35 %, ruido OU de velocidad, **incidentes** (trompo: 20 % de velocidad durante 0,6 s, tasa 1/20 s), cambios de carril, **derrape lateral** (centrífugo + "patadas" 30–80 mm en curva). Velocidades p1/p50/p99: 0,49/1,33/2,05 m/s.
- Cámara A: x ∈ [−2600, 400]; cámara B: x ∈ [−400, 2600] → **solape de 800 mm** en el centro de ambas rectas. 30 fps, jitter 2 ms, fase de B aleatoria.
- Medición: σ = 6–12 mm por eje (crece lejos del centro de cámara), **sesgo residual de calibración en B (12, −9) mm**, pérdida independiente **p_miss = 10 % y 20 %**, ráfagas de oclusión por cámara (0,1/s, 0,2–0,8 s), oclusión global por auto (0,03/s, 0,3–1,0 s), blobs fusionados si dos autos < 110 mm (sin ID), falsos positivos Poisson 0,05/frame, ArUco legible 45–75 % (1 % ID erróneo).
- **Desfase de timestamp de B: δ ~ U(30, 80) ms** por corrida.
- 6 semillas × 60 s por configuración y p_miss (5,9 min evaluados, grilla de 30 Hz). Métrica: Húngaro tracks↔verdad con umbral 200 mm.

### 5.2 Configuraciones
| ID | Descripción |
|---|---|
| C0 | Un tracker por cámara (CV+ArUco), salida = unión (equivale a "tracking por imagen y luego juntar") |
| C1 | Global en mundo, CV, timestamps correctos, **sin** ArUco |
| C2 | Global, CV, timestamps correctos, **con** ArUco (**propuesta**) |
| C3 | C2 con Kalman CA |
| C4 | C2 con IMM (CV bajo + CV alto) |
| C5 | Pre‑fusión de pares de frames A/B y luego tracker (timestamps correctos) |
| C5n | C5 tomando los timestamps reportados (desfase sin compensar) — "frames simultáneos" ingenuo |
| C6 | Global con timestamps reportados (desfase sin compensar) |
| C7 | Global con desfase estimado online por gradiente |
| C8 | Global con desfase **calibrado en lote** (primeros 20 s, ArUco en solape, mediana) |

### 5.3 Resultados (escenario base, carriles a 150 mm)

`IDSW_track` = cambios del track interno asignado a un auto real (incluye re‑creación tras oclusión); `IDSW_pub` = cambios del ID publicado (car_id o track sin etiqueta); `IDSW_car` = cambios de un car_id a **otro** car_id (solo medido en la 2ª tanda, C2/C8). RMS_conf = error de tracks actualizados; RMS_lost = error durante coasting.

**p_miss = 10 %**

| Cfg | IDSW_track | IDSW_pub | Dup total % | Dup solape % | RMS_conf mm | RMS_lost mm | p95 mm | Ranking OK % (sep>100 mm) | v RMS KF / SG / dif. mm/s | Vueltas OK | Err. cruce meta medio / máx ms | µs/update |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| C0 | 1033 | 179 | 29,7 | **88,1** | 17,6 | 88 | 36,7 | 98,9 (99,6) | 105 / 58 / 183 | 18/18 | 13 / 74 (4 cruces extra) | 217 |
| C1 | 22 | 22 | 0,40 | 0,29 | 17,5 | 96 | 36,4 | — | 105 / 56 / 182 | — | — | 323 |
| **C2** | 26 | 21 | 0,45 | **0,29** | **17,7** | 97 | 36,7 | **99,2 (99,9)** | **105 / 58** / 181 | **18/18** | **14 / 74** | 237 |
| C3 (CA) | 62 | 53 | 0,54 | 0,17 | 17,9 | 103 | 36,3 | 98,3 (99,1) | 157 / 86 / 214 | 18/18 | 15 / 107 | 284 |
| C4 (IMM) | 27 | 23 | 0,43 | 0,20 | 17,8 | 96 | 36,8 | 99,2 (99,7) | 104 / 57 / 178 | 18/18 | 14 / 74 | 743 |
| C5 | 32 | 27 | 0,43 | 0,15 | 18,8 | 98 | 38,2 | 99,2 (99,8) | 112 / 59 / 196 | 18/18 | 16 / 107 | 395 |
| C5n | 185 | 210 | 5,3 | 27,9 | 63,9 | 112 | 127 | 98,8 (99,8) | 154 / 112 / 287 | 18/18 | 42 / 390 | 417 |
| C6 | 309 | 326 | 9,7 | **54,5** | **62,6** | 109 | 125 | 98,9 (99,7) | 141 / 117 / 274 | 18/18 | 32 / 290 | 291 |
| C7 | 184 | 198 | 5,2 | 28,0 | 45,6 | 102 | 111 | 99,0 (99,8) | 122 / 90 / 230 | 18/18 | 23 / 190 | 270 |
| **C8** | 26 | 21 (car→car: 5) | 0,45 | 0,29 | 18,3 | 98 | 37,9 | 99,2 (99,8) | 105 / 58 / 183 | 18/18 | 15 / 74 | 361* |

\* C8 se midió en una 2ª tanda, con otro proceso compartiendo las 2 CPU; en esa misma tanda C2 dio 243–348 µs, así que su µs/update no se puede comparar directamente con el de la 1ª tanda.

Error de δ estimado: **C7 = 19,7 ms**, **C8 = 2,8 ms** (medio absoluto).

**p_miss = 20 %**

| Cfg | IDSW_track | IDSW_pub | Dup solape % | RMS_conf mm | RMS_lost mm | Ranking OK % (sep) | v RMS KF / SG mm/s | Vueltas | Cruce medio / máx ms |
|---|---|---|---|---|---|---|---|---|---|
| C0 | 1074 | 189 | 84,4 | 18,3 | 83 | 98,9 (99,7) | 109 / 62 | 18/18 | 15 / 207 |
| C1 | 31 | 31 | 0,29 | 18,4 | 90 | — | 108 / 60 | — | — |
| **C2** | 33 | 27 (car→car: 11) | 0,20 | 18,5 | 90 | 99,4 (99,8) | 109 / 60 | 18/18 | 16 / 174 |
| C3 | 52 | 42 | 0,20 | 18,6 | 95 | 98,6 (99,6) | 164 / 87 | 18/18 | 15 / 159 |
| C4 | 27 | 20 | 0,20 | 18,5 | 91 | 99,4 (99,8) | 109 / 63 | 18/18 | 16 / 174 |
| C5 | 27 | 19 | 0,23 | 19,6 | 92 | 99,3 (99,7) | 116 / 64 | 18/18 | 16 / 161 |
| C5n | 212 | 225 | 27,9 | 64,4 | 107 | 99,0 (99,8) | 163 / 119 | 18/18 | 42 / 290 |
| C6 | 326 | 329 | 50,9 | 62,7 | 99 | 99,0 (99,8) | 151 / 120 | 18/18 | 38 / 341 |
| C7 | 194 | 204 | 30,8 | 48,5 | 96 | 99,0 (99,8) | 132 / 95 | 18/18 | 30 / 190 |
| **C8** | 27 | 20 (car→car: 6) | 0,17 | 18,4 | 91 | 99,4 (99,8) | 108 / 60 | 18/18 | 16 / 174 |

Error de δ: C7 = 23,9 ms, C8 = 2,7 ms.

### 5.4 Escenario "stress" (carriles a 100 mm, autos casi pegados, blobs fusionados)

| Cfg (p_miss) | IDSW_pub | car→car | Dup solape % | RMS_conf mm | Etiqueta errónea % | Ranking OK % (sep) | Cruce máx ms |
|---|---|---|---|---|---|---|---|
| C0 (10 %) | 217 | — | 87,5 | 17,5 | 0,26 | 98,1 (99,1) | 465 |
| C1 (10 %) | 39 | — | 0,00 | 17,2 | — | — | — |
| C2 (10 %) | 58 | 32 | 0,30 | 17,6 | 0,25 | 98,6 (99,5) | 465 |
| C4 (10 %) | 64 | — | 0,30 | 17,7 | 0,27 | 98,7 (99,5) | 465 |
| C6 (10 %) | 396 | — | 52,2 | 58,2 | 0,29 | 98,3 (99,4) | 531 |
| C7 (10 %) | 144 | — | 10,9 | 32,8 | 0,26 | 98,5 (99,5) | 465 |
| C8 (10 %) | 58 | 32 | 0,30 | 17,8 | 0,24 | 98,6 (99,5) | 465 |
| C2 (20 %) | 54 | 32 | 0,38 | 18,4 | 0,29 | 98,8 (99,7) | 265 |
| C8 (20 %) | 58 | 34 | 0,38 | 18,3 | 0,29 | 98,8 (99,7) | 265 |

δ estimado en stress: C7 = 1,8 / 11,2 ms; C8 = 1,3 / 2,4 ms.

### 5.5 Lectura de los resultados (solo lo medido)
1. **La fusión en mundo con lista única elimina los duplicados**: 84–88 % de frames duplicados en el solape con trackers por cámara (C0) vs 0,2–0,4 % con tracker global (C1/C2/C8).
2. **El desfase de 30–80 ms sin compensar es el peor problema medido**: triplica el error (17,7 → 62,6 mm RMS), produce 51–55 % de duplicados en el solape y ~12× más cambios de ID (C6). Tratar frames como simultáneos (C5n) tampoco lo arregla (28 %).
3. **Calibrar δ en lote con ArUco en el solape (C8) recupera el rendimiento de timestamps perfectos** (δ con 1,3–2,8 ms de error medio; métricas ≈ C2). El estimador online simple (C7) no converge bien en 60 s → **descartado como método principal**.
4. **CV ≈ IMM; CA peor.** IMM no mejoró ninguna métrica de forma apreciable y cuesta ~3× CPU (0,72–0,81 ms/update vs 0,24). CA duplicó los cambios de ID y subió el ruido de velocidad (157 vs 105 mm/s).
5. **ArUco**: con carriles separados (150 mm) el tracker sin ID ya casi no intercambia; el valor del ArUco es dar **identidad absoluta** para ranking/vueltas y para recuperar el mismo ID tras perder un auto > 1 s. Con autos pegados, persisten 32–34 cambios car→car en ~6 min y 0,25–0,36 % de frames con etiqueta errónea.
6. **Precisión de posición**: RMS de tracks actualizados 17–19 mm (el error crudo de detección simulado es 16,8 mm RMS 2D, incluido el sesgo de 15 mm de B): el Kalman CV a 30 fps **casi no reduce** el error con estas dinámicas; el piso lo pone la calibración. En coasting (4–5 % del tiempo) el error sube a 83–103 mm.
7. **Velocidad**: Savitzky‑Golay (offline) 56–64 mm/s RMS vs Kalman 104–110 mm/s vs diferencia central 177–200 mm/s (velocidades típicas 1,3 m/s → ~4–8 %).
8. **Ranking** correcto en 98,6–99,4 % de los frames; 99,5–99,9 % cuando los autos están separados > 100 mm en progreso. **Vueltas: 18/18** correctas en todas las configs; error medio de cruce de meta 14–20 ms, máximo 74–465 ms (el máximo ocurre con el auto en coasting justo en la meta).
9. **Costo**: 0,24 ms medio por `update()` (≈ 4 detecciones), p99 4–9 ms (medido con 2 procesos compitiendo por 2 CPU; el p99 está dominado por el planificador, no por el algoritmo). Con 2 cámaras a 30 fps = 60 updates/s → < 2 % de un núcleo.

---

## 6. Archivos que necesita crear

| Archivo | Contenido |
|---|---|
| `multicam_vision/tracking/types.py` | `WorldDetection`, `TrackState`, `TrajectoryPoint`, `Track`, `TrackOutput`, `LapEvent` |
| `multicam_vision/tracking/motion_models.py` | `KalmanCV`, `KalmanCA` (opcional), `IMMFilter` (opcional), interfaz `MotionFilter` |
| `multicam_vision/tracking/association.py` | construcción de matriz de costos (Mahalanobis + ln|S| + identidad), gating, `associate()` con `linear_sum_assignment` |
| `multicam_vision/tracking/tracker.py` | `GlobalTracker` (ciclo de vida, nacimientos, supresión, merge, identidades) |
| `multicam_vision/tracking/time_sync.py` | `ReorderBuffer`, `ClockOffsetCalibrator` (lote) y monitor de deriva |
| `multicam_vision/tracking/trajectories.py` | `TrajectoryStore` (anillo en memoria + volcado a BD/CSV), `smooth_velocity_savgol()` |
| `multicam_vision/tracking/track_progress.py` | `CenterlinePolyline`, `ProgressTracker` (desenrollado, vueltas, ranking) |
| `multicam_vision/tracking/lap_reconciler.py` | cruce visión ↔ RFID |
| `multicam_vision/config/tracking.yaml` (o sección en `settings.py`) | q, gates, tiempos de vida, ventana Savitzky‑Golay, δ por cámara, ruta de la polilínea |
| `multicam_vision/tests/test_tracking_sim.py` | simulador (portado de `agente04/sim.py`) + asserts de regresión sobre métricas |
| `multicam_vision/tests/test_progress.py` | proyección, desenrollado, cruces de meta, hacia atrás (trompo) |

---

## 7. Interfaces que necesita (firmas Python concretas)

```python
# tracking/types.py
from __future__ import annotations
import enum
from dataclasses import dataclass, field
import numpy as np

class TrackState(enum.Enum):
    TENTATIVE = "tentative"   # < confirm_hits aciertos
    CONFIRMED = "confirmed"   # actualizado hace <= lost_after s
    LOST = "lost"             # en coasting (se publica como predicción, marcado)
    DELETED = "deleted"       # se descarta

@dataclass(frozen=True)
class WorldDetection:
    xy_mm: np.ndarray                 # (2,) en el plano de pista
    cov_mm2: np.ndarray               # (2,2) incertidumbre propagada (detector + homografía)
    t_capture: float                  # s, reloj común, YA corregido por el offset de su cámara
    camera_id: str
    marker_id: int | None = None      # ArUco (1..3) o color resuelto a id
    score: float = 1.0
    heading_rad: float | None = None  # si el detector lo entrega (ArUco da orientación)

@dataclass(frozen=True)
class TrajectoryPoint:
    t: float
    xy_mm: tuple[float, float]
    v_mm_s: tuple[float, float]
    s_mm: float | None                # longitud de arco sobre línea central
    lap: int | None
    source: str                       # "A", "B" o "pred" (coasting)
    state: TrackState

@dataclass
class Track:
    track_id: int                     # interno, único, nunca se reutiliza
    car_id: int | None                # identidad pública persistente (1..3) por ArUco/color
    state: TrackState
    x: np.ndarray                     # estado del filtro [x, y, vx, vy]
    P: np.ndarray
    t_last_pred: float
    t_last_update: float
    t_created: float
    hits: int = 1
    id_votes: dict[int, int] = field(default_factory=dict)
    cameras_seen: set[str] = field(default_factory=set)
    history: list[TrajectoryPoint] = field(default_factory=list)

    @property
    def public_id(self) -> str: ...   # "car-2" o "trk-17" si aún no tiene identidad

@dataclass(frozen=True)
class LapEvent:
    car_id: int
    lap: int
    t_vision: float | None
    t_rfid: float | None
    source: str                       # "vision", "rfid", "both"
    confirmed: bool

# tracking/motion_models.py
class MotionFilter(Protocol):
    x: np.ndarray; P: np.ndarray
    def predict(self, dt: float) -> None: ...
    def innovation(self, z: np.ndarray, R: np.ndarray) -> tuple[np.ndarray, np.ndarray]: ...
    def update(self, z: np.ndarray, R: np.ndarray) -> None: ...
    def peek(self, dt: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]: ...  # pos, vel, Σpos

class KalmanCV(MotionFilter):
    def __init__(self, z: np.ndarray, R: np.ndarray, q_mm2_s3: float = 1e6, v0_sigma: float = 1500.0): ...

# tracking/association.py
def build_cost_matrix(tracks: list[Track], dets: list[WorldDetection], cfg: TrackerConfig,
                      owner_of: Callable[[int], Track | None]) -> np.ndarray: ...
def associate(cost: np.ndarray, infeasible: float = 1e6
              ) -> tuple[list[tuple[int, int]], list[int], list[int]]:  # matches, tracks sin det, dets sin track
    ...

# tracking/tracker.py
@dataclass
class TrackerConfig:
    model: str = "cv"; q_cv: float = 1e6
    gate_chi2: float = 13.82; gate_max_mm: float = 450.0
    confirm_hits: int = 3; tentative_timeout_s: float = 0.15
    lost_after_s: float = 0.10; max_coast_s: float = 1.0
    use_identity: bool = True; id_min_votes: int = 2; id_min_ratio: float = 0.7
    spawn_suppress_mm: float = 90.0; merge_mm: float = 60.0; r_floor_mm: float = 10.0
    max_cars: int = 3

class GlobalTracker:
    def __init__(self, cfg: TrackerConfig): ...
    def update(self, world_detections: list[WorldDetection], t: float) -> list[Track]:
        """Procesa UN frame de UNA cámara en su t de captura. Devuelve tracks CONFIRMED+LOST."""
    def outputs_at(self, t: float) -> list[Track]: ...      # predicción sin modificar estado
    def get_track(self, car_id: int) -> Track | None: ...
    def reset(self) -> None: ...

# tracking/time_sync.py
class ReorderBuffer:
    def __init__(self, latency_s: float = 0.10): ...
    def push(self, camera_id: str, t_capture: float, dets: list[WorldDetection]) -> None: ...
    def pop_ready(self, now: float) -> list[tuple[float, str, list[WorldDetection]]]: ...

class ClockOffsetCalibrator:
    def add_frame(self, camera_id: str, t_reported: float, dets: list[WorldDetection]) -> None: ...
    def estimate(self, ref: str = "A", other: str = "B", min_samples: int = 10) -> tuple[float, float, int]:
        """(delta_s, mad_s, n_muestras): t_real_other = t_reportado_other - delta."""

# tracking/trajectories.py
class TrajectoryStore:
    def append(self, track: Track, point: TrajectoryPoint) -> None: ...
    def get(self, car_id: int, t0: float, t1: float) -> list[TrajectoryPoint]: ...
    def flush(self, sink: Callable[[list[TrajectoryPoint]], None]) -> None: ...
def smooth_velocity_savgol(t: np.ndarray, xy: np.ndarray, window: int = 11, order: int = 2,
                           fs: float = 30.0) -> np.ndarray: ...   # re-muestrea a fs y deriva

# tracking/track_progress.py
class CenterlinePolyline:
    def __init__(self, pts_mm: np.ndarray): ...                   # cerrada, pts[0] = meta
    L: float
    def project(self, xy_mm: np.ndarray, s_hint: float | None = None,
                window_mm: float = 800.0) -> tuple[float, float]: ...   # (s, offset lateral)
class ProgressTracker:
    def update(self, car_id: int, xy_mm: np.ndarray, t: float) -> float: ...  # progreso desenrollado
    def laps(self, car_id: int) -> int: ...
    def ranking(self) -> list[int]: ...                           # car_ids 1°, 2°, 3°
    def correct_to_lap(self, car_id: int, lap: int) -> None: ...  # usado por RFID

# tracking/lap_reconciler.py
class LapReconciler:
    def __init__(self, progress: ProgressTracker, tag_to_car: dict[str, int], window_s: float = 0.3): ...
    def on_vision_crossing(self, car_id: int, t: float) -> None: ...
    def on_rfid(self, tag_uid: str, lane: int, t: float) -> LapEvent: ...
    def poll(self, now: float) -> list[LapEvent]: ...
```

---

## 8. Riesgos y limitaciones

- **Todo es simulación en mundo**: no modela errores del detector en imagen (sombras, reflejos, blobs partidos), ni la distorsión real de la homografía oblicua del Galaxy J7, ni la latencia real de MJPEG por HTTP. El RMS de 17–19 mm es un piso con σ y sesgo **supuestos**.
- **Timestamps**: con IP Webcam/MJPEG el timestamp es el de llegada a la PC; la latencia por cámara puede no ser constante (buffering, Wi‑Fi). La calibración en lote asume desfase casi constante; si deriva, hay que re‑estimar periódicamente (monitor). Recomendación cruzada con el agente de cámaras: `BUFFERSIZE=1`, descartar frames viejos y medir la latencia con un evento visible por ambas cámaras (LED del ESP32 parpadeando en el solape).
- **Sesgo de calibración entre cámaras** (15 mm simulado): si en el sistema real es > ~45 mm (≈ la mitad de la supresión de nacimientos + gating), aparecerán duplicados en el solape aunque el tiempo esté bien. La métrica de residuos A↔B en el solape debe ser parte de la validación de la calibración.
- **Autos pegados** (stress): 32–34 cambios car→car en ~6 min y 0,3 % de frames con etiqueta errónea; depende de que el ArUco sea legible. Si el marcador es poco legible en la vista oblicua, el color del auto debe aportar identidad.
- **Coasting**: con > 1 s sin detección el track muere; el `car_id` se recupera con 2 lecturas ArUco, pero hasta entonces el auto aparece con ID provisional. Si un auto está perdido > ~3 s, el desenrollado de progreso puede quedar desfasado una vuelta → lo corrige RFID.
- **Trompos / marcha atrás**: el contador de vueltas solo cuenta cruces hacia adelante; un retroceso sobre la meta y re‑cruce generaría una vuelta extra (observado: 0–2 cruces extra en 114). Mitigación: histéresis (exigir `prog` > múltiplo de L + 300 mm) y confirmación RFID.
- **Latencia**: buffer 100 ms + Savitzky‑Golay 167 ms (solo para la velocidad registrada).
- Pérdida de los JSON de la 1ª tanda (sobrescritos por la 2ª); los valores de la 1ª tanda quedan en `out_main.txt` / `out_stress.txt`.

---

## 9. Pruebas necesarias

1. **Unitarias**: Kalman CV (predicción con dt variable y dt negativo), gating (detección fuera de gate no se asocia), restricciones de identidad (marcador incompatible nunca se asigna), ciclo de vida (tiempos exactos de transición), polilínea (proyección, wrap en s=0, ventana), desenrollado y cruces.
2. **Regresión con el simulador** (`tests/test_tracking_sim.py`, semillas fijas): asserts sobre duplicados en solape (< 1 %), RMS_conf, ranking (> 98 %), vueltas exactas, δ calibrado (< 5 ms de error).
3. **Con video real grabado** (con timestamps de llegada) de ambas cámaras: (a) residuos A↔B en el solape antes/después de la calibración de δ; (b) conteo manual de duplicados e intercambios en un clip etiquetado; (c) comparación de cruces visuales vs RFID (distribución del error → ajustar `window_s`).
4. **Prueba de desfase**: LED del ESP32 en el solape visible por ambas cámaras → medir δ directo y comparar con `ClockOffsetCalibrator`.
5. **Prueba de desconexión**: cortar una cámara; el tracker debe seguir con la otra (los autos fuera de su campo pasan a LOST y luego DELETED; al reconectar se recupera `car_id` por ArUco).
6. **Rendimiento en la PC del usuario** (Ryzen 7 5700X, Windows): µs por `update()` con 2 cámaras a 30 fps.

---

## 10. Estado del trabajo

| Ítem | Estado |
|---|---|
| Investigación y comparativa (mundo vs imagen, SORT/ByteTrack/centroid, CV/CA/IMM, OOSM, Savitzky‑Golay) | Implementado (este informe) |
| Simulador 3 autos / 2 cámaras / desfase / pérdidas (`agente04/sim.py`) | Implementado y **Probado** (solo simulación) |
| Prototipo tracker global CV/CA/IMM + Húngaro + Mahalanobis + ArUco + estados (`agente04/tracker_proto.py`) | **Parcial** (prototipo de experimento, no es el módulo final) — Probado en simulación |
| Calibración de desfase en lote (C8, en `run_experiments.py`) | Parcial — Probado en simulación (error 1,3–2,8 ms) |
| Estimador de desfase online (C7) | Probado en simulación — **descartado** (no converge suficiente) |
| Progreso por longitud de arco, ranking, vueltas (`agente04/progress.py`) | Parcial — Probado en simulación (18/18 vueltas) |
| Reconciliación con RFID (`LapReconciler`) | **Pendiente** (solo diseño; no simulado) |
| 2ª pasada estilo ByteTrack con score bajo | Pendiente (no probado) |
| Módulos finales en `multicam_vision/tracking/` | Pendiente |
| Validación con video real / cámaras del usuario | Pendiente |

### Archivos del experimento
- `/tmp/claude-0/-home-claude/0ca3fc1e-d1b0-54d9-9f08-b03b5d26a0a9/scratchpad/agente04/sim.py`
- `/tmp/claude-0/-home-claude/0ca3fc1e-d1b0-54d9-9f08-b03b5d26a0a9/scratchpad/agente04/tracker_proto.py`
- `/tmp/claude-0/-home-claude/0ca3fc1e-d1b0-54d9-9f08-b03b5d26a0a9/scratchpad/agente04/progress.py`
- `/tmp/claude-0/-home-claude/0ca3fc1e-d1b0-54d9-9f08-b03b5d26a0a9/scratchpad/agente04/run_experiments.py` (uso: `python3 run_experiments.py 6 60 all [--stress]`)
- `/tmp/claude-0/-home-claude/0ca3fc1e-d1b0-54d9-9f08-b03b5d26a0a9/scratchpad/agente04/sweep_q.py` (barrido de q)
- Salidas: `out_main.txt`, `out_stress.txt`, `out_c8.txt`, `out_c8s.txt`, `results_c8.json`, `results_c8_stress.json` en la misma carpeta.

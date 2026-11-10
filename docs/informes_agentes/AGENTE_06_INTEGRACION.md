# AGENTE 06 — INTEGRACIÓN Y TESTING (ESP32 + RC522, eventos, pruebas, rendimiento)

> Fase: **solo propuesta técnica**. Lo único ejecutado son dos experimentos de prototipo en el workspace en la nube (Python 3.13.18, OpenCV 5.0.0, NumPy 2.5.3, 2 CPU). Todo número marcado **[MEDIDO]** sale de esos scripts; lo marcado **[CALCULADO]** es aritmética sobre supuestos explícitos; lo marcado **[FUENTE]** viene de la bibliografía citada; lo marcado **[A MEDIR]** no se conoce todavía y no se debe asumir.

---

## 1. Objetivo

1. Definir cómo se comunican el ESP32 (3× RC522, semáforo) y la PC, con un protocolo versionado, robusto ante desconexiones y con marcas de tiempo útiles para cronometrar vueltas.
2. Definir la arquitectura de eventos en la PC de forma que **el RFID nunca bloquee el pipeline de video**, con eventos tipados (RFID ≠ visual), fusión (RFID = verdad del cruce de meta; visión = posición continua), persistencia en el SQLite existente y emisión a la web Flask‑SocketIO existente.
3. Cuantificar si un RC522 alcanza a leer una etiqueta que pasa a 1–5 m/s y decidir dónde va la etiqueta y dónde el lector.
4. Definir el plan de pruebas por niveles (unitarias → componentes → integración offline con video grabado → hardware en el lazo → sistema) y cómo medir rendimiento sin inventar números.

---

## 2. Investigación realizada (con fuentes)

### 2.1 RC522 (MFRC522): tiempos y alcance

| Dato | Valor | Tipo / fuente |
|---|---|---|
| `PICC_IsNewCardPresent()` sin tarjeta (librería Arduino MFRC522) | 26–28 ms por llamada | [FUENTE] [Arduino forum — "PICC_IsNewCardPresent() command delay is 27"](https://forum.arduino.cc/t/rfid-rc522-picc_isnewcardpresent-command-delay-is-27/625691) |
| Origen de esos ~25 ms | Timer interno configurado en `PCD_Init` (TPrescaler 0xA9 → tick ≈ 25 µs; TReload 0x03E8 = 1000 ticks ≈ 25 ms). Es el **timeout de "no hay tarjeta"**, no el tiempo de lectura. | [CALCULADO] desde los valores de registro de la librería; coincide con ESPHome, que también fija ~25 ms de timer hardware y un watchdog de 40 ms: [ESPHome rc522.cpp](https://api-docs.esphome.io/rc522_8cpp_source) |
| Driver no bloqueante de referencia | ESPHome divide la transacción en estados (REQA → anticolisión CL1/CL2/CL3 → SELECT) repartidos entre `update()`/`loop()`; espera mínima 2 ms antes de sondear resultados | [FUENTE] [ESPHome rc522.cpp](https://api-docs.esphome.io/rc522_8cpp_source) |
| Alcance NTAG213 (sticker) con MFRC522 | ≈ 2 cm (etiquetas Ultralight antiguas ≈ 5 cm) | [FUENTE] [NXP Community — MFRC522 NTAG213 range](https://community.nxp.com/t5/NFC/MFRC522-NTAG213-range-improvements/m-p/653895) |
| Tiempo RF de una lectura de UID con etiqueta presente (REQA + ATQA + anticolisión + SELECT, 106 kbit/s; UID de 7 bytes = 2 niveles de cascada) | ≈ 3–6 ms incluyendo sobrecarga SPI/librería | [CALCULADO] a partir de ISO 14443‑3 (bit ≈ 9.44 µs; tramas de 1–9 bytes + tiempos de guarda). **[A MEDIR]** en el ESP32 real con `micros()` |
| Lectores múltiples con RST compartido: "solo funciona el último inicializado" | Síntoma reportado con 3 RC522 en ESP32 y RST común; el hilo no tiene solución confirmada | [FUENTE] [Arduino forum (de)](https://forum.arduino.cc/t/esp32-und-multiple-rc522-nur-das-zuletzt-initialisierte-funktioniert/1021895), [element14 multi‑MFRC522](https://community.element14.com/products/arduino/f/arduino-forum/28599/using-multiple-mfrc522-rfid-readers-on-one-arduino), [Arduino forum — multiple RC522 on SPI won't work](https://forum.arduino.cc/t/multiple-rc522-on-an-spi-bus-wont-work/1161286) |

**Hipótesis técnica (a verificar en banco):** con RST compartido, cada `PCD_Init()` que pulsa RST resetea también a los lectores ya inicializados. Por eso se propone: **un único pulso de RST al arrancar** y luego inicializar cada lector con *soft reset* (constructor sin pin RST / `UNUSED_PIN`). Los MISO separados (D19, D34, D35) son coherentes con módulos RC522 que no liberan MISO (no lo ponen en alta impedancia) con NSS alto; D34/D35 son solo‑entrada en el ESP32, lo que sirve para MISO.

### 2.2 ¿Alcanza a leerse una etiqueta a 1–5 m/s? [CALCULADO]

Supuestos explícitos: ventana útil de lectura a lo largo de la trayectoria **W ≈ 30 mm** (antena del RC522 ≈ 40×50 mm, sticker NTAG213 con alcance ≈ 20 mm según la fuente; distancia vertical prevista 8–13 mm). Tiempo de permanencia = W / v.

| v (m/s) | Permanencia en la ventana (W=30 mm) | Librería por defecto: 3 lectores × ~27 ms ≈ **81 ms por ciclo** | Firmware optimizado: timeout 1 ms, ciclo ≈ 1.5 ms/lector ≈ **4.5 ms** + lectura 3–6 ms ≈ **8–11 ms** peor caso |
|---|---|---|---|
| 0.5 | 60 ms | NO garantizado | OK |
| 1 | 30 ms | **NO** (falla casi siempre) | OK (margen ×3) |
| 2 | 15 ms | NO | OK (margen ×1.4–1.9) |
| 3 | 10 ms | NO | **Marginal** |
| 5 | 6 ms | NO | **NO garantizado** |

Conclusiones:
- Con la librería tal cual (timeout 25 ms y sondeo secuencial de 3 lectores) **la lectura falla ya a ~0.4 m/s** (30 mm / 81 ms). Hay que bajar `TReloadReg` a ~1–2 ms (la ATQA llega en ~0.1 ms) o usar una máquina de estados no bloqueante.
- **Mejora clave:** cada MFRC522 ejecuta su transacción RF solo; el SPI solo sirve para cargar el comando y leer el resultado. El firmware puede **lanzar REQA en los 3 lectores casi a la vez** (por CS) y luego sondear `ComIrqReg` de cada uno: el ciclo pasa de "suma" a "máximo" (≈ 1.5–2 ms para los tres) [CALCULADO, A MEDIR].
- Etiquetas con **UID de 4 bytes** (MIFARE Classic S50 en sticker) requieren 1 nivel de cascada en vez de 2 → lectura más corta. NTAG213 = 7 bytes.
- Por encima de ~3 m/s se recomienda **no depender solo del RFID**: la visión detecta el cruce de la línea de meta como respaldo (ver §3.5), y se mide la tasa real de lectura vs velocidad (prueba HIL‑2).
- Error temporal inherente: la etiqueta puede leerse en cualquier punto de W → incertidumbre ≈ W/v (≈ 10 ms a 3 m/s) + ciclo de sondeo. La resolución de `millis()` (1 ms) no es el factor limitante.

### 2.3 Enlace ESP32 → PC: USB serial vs Wi‑Fi

| Criterio | USB serial (CP2102/CH340 + pyserial) | Wi‑Fi UDP | Wi‑Fi WebSocket (TCP) | MQTT (TCP + broker) |
|---|---|---|---|---|
| Latencia típica | ms; depende del puente USB‑UART. FTDI: timer de latencia por defecto **16 ms** en Windows/Linux (reducible a 1 ms) [FUENTE](https://www.projectgus.com/2011/10/notes-on-ftdi-latency-with-arduino/), [Zaber FTDI](https://software.zaber.com/motion-library/docs/guides/communication/ftdi_latency). CP210x/CH340: **[A MEDIR]** con ping/pong | Pocos ms en LAN, pero con *modem‑sleep* activo el ESP32 duerme entre beacons/DTIM → respuestas más lentas [FUENTE: Espressif, low‑power Wi‑Fi](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-guides/low-power-mode/low-power-mode-wifi.html). Hay que forzar `WIFI_PS_NONE` | Igual que UDP + retransmisiones TCP (bloqueo de cabecera de línea ante pérdida) | Igual que TCP + salto por broker (mosquitto) |
| Fiabilidad | Alta; sin dependencia de red/DHCP | Pérdidas posibles, sin orden garantizado (seq + ack obligatorio) | Entrega ordenada; reconexión manual | Entrega con QoS; requiere broker extra |
| Riesgos propios | Abrir el puerto puede **resetear el ESP32** (DTR/RTS → auto‑reset); el COM puede cambiar de número; driver CP210x/CH340 en Windows | **DHCP sin reserva** (ya ocurrió con los teléfonos: .103/.109/.110); interferencia 2.4 GHz con las cámaras IP Wi‑Fi | Ídem Wi‑Fi | Ídem + otro proceso que mantener |
| Dependencia Python | `pyserial` (**NO instalado en el workspace**; dependencia para la PC del usuario) | stdlib `socket` | `websockets`/similar (no verificado) | `paho-mqtt` (no verificado) |

**Decisión:** **USB serial como transporte primario** (determinista, alimenta al ESP32, no compite con el Wi‑Fi que ya usan las cámaras MJPEG, y no depende de DHCP). La capa de transporte se abstrae (`Transport`) para poder añadir **UDP** después sin tocar el resto. WebSocket/MQTT descartados por ahora: más piezas y más latencia sin ventaja para un único cliente en la misma mesa.

Ancho de banda serial [CALCULADO]: a 115200 baudios (≈ 11 520 B/s), una línea de 120 B tarda **≈ 10.4 ms** solo en transmitirse; a 921600 baudios ≈ 1.3 ms. Se propone **921600** (validar estabilidad con el puente USB real) y mensajes compactos. Como los tiempos de vuelta usan `t_esp`, este retardo **no afecta al cronometraje**, solo a la frescura en la web.

### 2.4 Sincronización de relojes

- **Tiempo de vuelta = diferencia de dos `t_esp` del mismo reloj** → no requiere sincronización con la PC. La deriva del cristal (decenas de ppm, supuesto) aporta < 0.1 ms en una vuelta de pocos segundos.
- La sincronización ESP32↔PC solo hace falta para **correlacionar** un cruce RFID con frames de video (sellados con el reloj de la PC). Se usa ping/pong estilo NTP simplificado (t1,t2,t3,t4), filtrando las muestras de **menor RTT** (mediana del 25 % con menor RTT de las últimas 32) y re‑sincronizando periódicamente (cada 5–10 s). A 40 ppm, sin re‑sincronizar, la deriva acumulada en 10 min sería ≈ 24 ms [CALCULADO] → re‑sync periódico + estimación lineal de deriva.
- La latencia de las cámaras MJPEG por Wi‑Fi (captura → PC) **no está medida** y probablemente supera a la incertidumbre de sync; la ventana de fusión debe ser tolerante (§3.5) y esa latencia se mide con la prueba del LED (§7.3).

### 2.5 Experimentos propios (workspace en la nube) [MEDIDO]

Scripts en `/tmp/claude-0/-home-claude/0ca3fc1e-d1b0-54d9-9f08-b03b5d26a0a9/scratchpad/agente06/`.

**Exp. 1 — `exp1_bus_jitter.py` (+ `exp1b_consumidor_dedicado.py`).** Bucle de "video" a 30 Hz con deadlines absolutos y trabajo real por frame (GaussianBlur + absdiff + threshold sobre 960×720 gris, `cv2.setNumThreads(1)`), 20 s (600 frames) por escenario. Productor "RFID" en otro hilo cuya lectura dura 1–5 ms (70 %), 20–80 ms (25 %) o 200–500 ms (5 %); publica en un `EventBus` de `queue.Queue` con `put_nowait`. Dos corridas (resultados en `exp1_resultados.txt`, `exp1_resultados_run2.txt`, `exp1b_resultados.txt`).

| Escenario | FPS efectivo | σ período (ms) | p99 período (ms) | máx. período (ms) | retraso vs deadline p99 / máx (ms) | frames > 50 ms | latencia evento p50 / p99 / máx (ms) |
|---|---|---|---|---|---|---|---|
| A. Solo video (referencia) | 29.97 / 29.98 | 1.38 / 0.39 | 37.1 / 34.8 | 40.8 / 36.6 | 3.85/7.64 · 1.55/3.44 | 0 / 0 | — |
| **B. Bus + productor bloqueante tipo I/O** (libera el GIL) | 29.99 / 29.99 | 0.68 / 0.34 | 34.6 / 34.4 | 39.3 / 37.1 | 1.42/6.18 · 1.15/3.90 | **0 / 0** | 16.8 / 33.2 / 34.1 · 17.4 / 33.5 / 34.0 (drenado 1 vez por frame) |
| C. Bus + productor que quema CPU en Python (retiene el GIL) | 29.94 / 29.94 | 2.54 / 4.41 | 40.2 / 48.5 | 51.7 / 55.4 | 11.5/23.5 · 19.0/29.1 | 1 / 4 | 17.7 / 50.4 / 62.4 · 19.1 / 59.3 / 64.0 |
| D. **Anti‑patrón**: lectura RFID dentro del bucle de video | **18.98 / 18.92** | 79.5 / 79.9 | 446 / 447 | **501 / 500** | 415/504 · 418/502 | **74 / 80** | — |
| E. Bus + **consumidor dedicado** (`queue.get` bloqueante en hilo de fusión) | 29.97 | 1.50 | 37.2 | 37.4 | 4.02/5.36 | 0 | **0.04 / 2.95 / 4.90** |

Lectura de resultados (solo lo medido):
- Con el productor en hilo propio esperando I/O (B), el bucle de video **mantiene 30 FPS con jitter igual al de referencia** (0 frames por encima de 50 ms en 1200 frames). Con la lectura dentro del bucle (D) cae a **≈ 19 FPS y hay frames de hasta 500 ms**.
- Si el hilo productor hace trabajo **CPU en Python puro** (C), el GIL sí afecta: el trabajo por frame (p50) fue 1.15 ms en una corrida y **21.4 ms** en la otra, con hasta 4 frames > 50 ms. ⇒ El hilo serial debe **solo leer bytes y parsear líneas** (operaciones que liberan el GIL o son cortas); nada de bucles de espera activa en Python.
- Drenar la cola una vez por frame añade ~½ período de latencia (p50 ≈ 17 ms). Con un consumidor dedicado bloqueado en `get()` (E) la latencia baja a **p50 0.04 ms / p99 2.95 ms**. Para el cronometraje no importa (se usa `t_esp`), pero sí para la web/semaforo.
- En ninguna corrida se descartaron eventos (`bus_descartados = 0`, cola de 1024).

**Exp. 2 — `exp2_pty_ndjson_sync.py`.** ESP32 simulado sobre un pseudo‑terminal Linux en modo crudo (`os.openpty` + `tty.setraw`; sustituto de pyserial, que no está instalado). El simulador tiene un reloj `millis()` con offset desconocido y deriva +40 ppm, emite 5000 `rfid_read`, `heartbeat` cada 1 s y responde 64 pings con retardo asimétrico aleatorio (0.2–3 ms ida). Resultado (`exp2_resultados.txt`):

| Métrica | Valor medido |
|---|---|
| Líneas recibidas | 5000 rfid_read, 64 pong, 4 heartbeat; **0 líneas inválidas, 0 huecos de `seq`** |
| Tasa | 1479 rfid_read/s (limitada por el `sleep(0.5 ms)` del simulador, **no** por el parser) |
| Latencia escritura → parseo por el pty | p50 0.18 ms, p99 3.33 ms, máx 6.54 ms |
| RTT ping/pong (simulado) | mín 0.41 ms, máx 6.28 ms |
| Error del offset estimado vs verdad | final **0.061 ms**; máx de las últimas 10 estimaciones **0.905 ms** (incluye cuantización de 1 ms de `millis()`) |

Esto valida la **lógica** (framing NDJSON, detección de huecos, estimador de offset por mínimo RTT) con un retardo simulado; **no** mide el USB real del ESP32 (eso es [A MEDIR] en HIL‑1).

---

## 3. Solución propuesta

### 3.1 Ubicación de etiquetas y lectores

**Etiqueta en el auto (cara inferior, centrada), lector RC522 bajo la pista en la línea de meta, uno por carril.**
- El lector necesita alimentación, SPI y cableado al ESP32: debe ser fijo. La etiqueta es pasiva y casi sin masa: va en el auto. La opción inversa exigiría un lector + MCU + radio en cada auto 1:24: descartada.
- Chapadur (fibra de madera, no metálico) de 3–5 mm + altura libre del auto (**[A MEDIR]**, estimada 5–8 mm) → separación 8–13 mm: dentro del alcance ≈ 20 mm de NTAG213, con poco margen. Rebajar el chapadur sobre el lector (cajeado) si hace falta.
- Mantener la etiqueta **alejada de metal, motor y batería** (desintonizan la antena); si no se puede, usar etiquetas *on‑metal* o un separador de espuma de 3–5 mm.
- Separación entre lectores de carriles vecinos: si un auto se desvía, el lector vecino puede leerlo. El **UID identifica al auto**; el número de lector solo indica carril. La PC desduplica por UID (ventana de 500 ms) para no contar dos veces.
- Las 5 etiquetas disponibles: 3 en los autos + 2 de repuesto/prueba de banco.

### 3.2 Firmware ESP32 (especificación, no implementación)

- **Un único pulso de RST (D17)** al arrancar (LOW 2 µs → HIGH, esperar 50 ms) y luego `PCD_Init` de cada lector **sin** RST (soft reset). Verificar `VersionReg` (0x91/0x92) de los 3 lectores y reportarlo en `hello`.
- **MISO conmutado**: antes de hablar con el lector *i*, reasignar MISO al pin del lector *i* (re‑`begin` del SPI o `gpio_matrix_in`) y bajar su CS. Costo de la conmutación **[A MEDIR]** con `micros()`.
- **Bajar el timeout** del timer interno (`TReloadReg`) a 1–2 ms para REQA; `RxGain` al máximo (48 dB).
- **Máquina de estados no bloqueante** por lector (patrón ESPHome): ningún `delay()`; `loop()` avanza estados y atiende el serial. Opcional (recomendado): lanzar REQA en los 3 lectores y luego sondear.
- **Detección de flanco**: emitir `rfid_read` al **primer** UID leído tras ≥ 300 ms sin verlo en ese lector (no repetir mientras la etiqueta está encima). `t_esp` = `millis()` del instante de la lectura del UID (incluir `t_us` opcional con `micros()` para análisis).
- Telemetría en `heartbeat`: tiempo máximo de `loop()` (µs), lecturas por lector, errores de CRC/timeout, mensajes descartados por buffer lleno.
- Semáforo: aplicar `light_cmd` y responder `ack` con el `seq` del comando.

### 3.3 Protocolo JSON v1 (NDJSON)

**Framing**: UTF‑8, un objeto JSON por línea, terminado en `\n` (se tolera `\r\n`), **máximo 256 bytes** por línea (líneas más largas se descartan y se cuentan). Sin espacios (`separators=(",",":")`). Campos desconocidos se ignoran (compatibilidad hacia adelante); `v` distinto de 1 → se registra y se descarta.

**Campos comunes**

| Campo | Tipo | Dirección | Semántica |
|---|---|---|---|
| `v` | int | ambas | versión de protocolo (=1) |
| `type` | str | ambas | tipo de mensaje |
| `seq` | uint32 | ambas | contador por emisor, +1 por mensaje, envuelve en 2³²; huecos ⇒ pérdida |
| `t_esp` | uint32 ms | ESP→PC | `millis()` del ESP32 (envuelve a los ~49.7 días; ignorable) |
| `t_pc` | float s | PC→ESP | `time.monotonic()` de la PC (solo se usa en ping, se devuelve en pong) |
| `boot` | uint32 | ESP→PC (`hello`, `heartbeat`) | id aleatorio de arranque: si cambia, el ESP32 se reinició (millis y seq vuelven a 0) |

**ESP32 → PC**

```json
{"v":1,"type":"hello","seq":0,"t_esp":812,"boot":2734419921,"fw":"autodromo-rfid 0.1.0","readers":[{"id":1,"ver":"0x92"},{"id":2,"ver":"0x92"},{"id":3,"ver":"0x00"}],"baud":921600}
{"v":1,"type":"rfid_read","seq":57,"t_esp":183420,"reader":2,"uid":"04A1B2C3D4E5F6","t_us":183420511}
{"v":1,"type":"heartbeat","seq":58,"t_esp":184000,"boot":2734419921,"readers_ok":[1,1,0],"reads":[12,9,0],"loop_us_max":3810,"tx_drop":0}
{"v":1,"type":"pong","seq":59,"t_esp":184003,"ping_seq":41,"t1":5021.337712,"t2":184002,"t3":184003}
{"v":1,"type":"ack","seq":60,"t_esp":184010,"ack_seq":42,"ok":true}
{"v":1,"type":"ack","seq":61,"t_esp":184011,"ack_seq":43,"ok":false,"err":"bad_state"}
{"v":1,"type":"error","seq":62,"t_esp":184500,"code":"reader_timeout","reader":3,"msg":"VersionReg=0x00"}
```

- `rfid_read.reader`: 1..3 = carril. `uid`: hex mayúsculas sin separadores (8 o 14 caracteres).
- `heartbeat`: cada 1 s. La PC declara el enlace caído tras 3 s sin ningún mensaje.

**PC → ESP32**

```json
{"v":1,"type":"ping","seq":41,"t_pc":5021.337712}
{"v":1,"type":"light_cmd","seq":42,"target":"semaforo","state":"countdown","step_ms":1000}
{"v":1,"type":"light_cmd","seq":43,"target":"lane","lane":2,"color":"green","mode":"blink","period_ms":250}
{"v":1,"type":"light_cmd","seq":44,"target":"all","color":"off"}
```

- `light_cmd.target`: `semaforo` (`state`: `off|red|yellow|green|countdown`), `lane` (LED tricolor del carril 1..3: `color`: `off|red|green|blue|yellow|white`, `mode`: `solid|blink`), `all`.
- Todo `light_cmd` exige `ack`; si no llega en 200 ms se reintenta hasta 3 veces con el **mismo `seq`** (el ESP32 ignora duplicados por `seq` pero responde el `ack`).
- `ping`: cada 1 s durante los primeros 10 s tras `hello`, luego cada 5 s.

**Integridad**: USB ya tiene CRC por paquete; JSON inválido + huecos de `seq` detectan corrupción/pérdida en la UART. No se agrega checksum en v1 (si se pasa a UDP, se evalúa `crc` en v2).

### 3.4 Arquitectura de eventos en la PC

```
  [Hilo Esp32Link.reader]  bytes→líneas→dict→RfidEvent/HeartbeatEvent  ─┐
  (reconexión con backoff 0.5→5 s; nunca toca el video)                  │ publish (put_nowait, nunca bloquea)
  [Hilos de cámara] → [Pipeline visión] → VisualEvent (track_id, x_mm…) ─┤
                                                                         ▼
                                                                   EventBus (pub‑sub; 1 cola acotada por suscriptor)
                                    ┌────────────────────┬───────────────┼──────────────────────┐
                                    ▼                    ▼               ▼                      ▼
                           [Hilo Fusión/Carrera]  [Hilo Persistencia]  [Hilo Web emitter]  [Logger NDJSON crudo]
                           RFID=vuelta, visión=   SQLite WAL, batch    Flask‑SocketIO,      (para reproducir
                           posición → LapEvent,   cada 200 ms, conexión throttle 10 Hz      sesiones offline)
                           StandingsEvent         propia del hilo
```

Reglas:
- `publish()` usa `put_nowait`; si una cola está llena se **descarta y se cuenta por suscriptor** (métrica visible). Nunca bloquea al productor (ni a la visión).
- El pipeline de visión **no consume** eventos RFID; publica `VisualEvent` y sigue. Quien necesita ambos es la fusión (consumidor dedicado, Exp. E).
- El hilo serial solo hace `read()` + `split(b"\n")` + `json.loads` (Exp. C mostró que trabajo CPU en Python en otro hilo sí puede degradar el video vía GIL).
- Si el proceso de visión llega a saturar el GIL, la opción de reserva es mover `Esp32Link` a un **proceso** separado (`multiprocessing`) que entregue por `multiprocessing.Queue`; la interfaz `EventBus` no cambia.

### 3.5 Fusión RFID + visión

- **Identidad canónica**: `vehiculo_id ∈ {1,2,3}` = ID ArUco del auto. Tabla `vehiculos` con `uid_rfid` único → `RfidEvent.uid` se traduce a `vehiculo_id`. UID desconocido ⇒ `UnknownTagEvent` (registro, no vuelta).
- **Vuelta (verdad)**: un `RfidEvent` válido de `vehiculo_id` cuenta una vuelta si `t_esp − t_esp_última ≥ min_lap_ms` (configurable, p. ej. 1500 ms) y desduplicando entre lectores por UID (500 ms). `duracion_ms = t_esp − t_esp_anterior` (mismo reloj ESP32).
- **Respaldo visual**: si el tracker cruza la línea de meta (definida en mm en el plano común) y no llega RFID de ese auto dentro de ±`fusion_window_ms` (inicial 300 ms, ajustar con la latencia de cámara medida), se registra una vuelta con `fuente='vision'` y `estado='sin_rfid'`; configurable si cuenta para la clasificación.
- **Validación cruzada**: RFID sin cruce visual en la ventana → vuelta válida pero `estado='sin_vision'` (diagnóstico). Las tasas de ambos casos son métricas de salud.
- **Posiciones 1°/2°/3°**: orden por (vueltas completadas desc, progreso `s` a lo largo de la línea central de la pista desc, `t_esp` del último cruce asc). El progreso `s` viene de visión (agente de tracking); entre cruces, la visión manda; en el cruce, el RFID corrige.
- Al reiniciarse el ESP32 (`boot` distinto): se reinicia la referencia `t_esp` de cada auto y se resincroniza el reloj; la vuelta en curso se marca `estado='reset_esp'`.

### 3.6 Persistencia SQLite (corrección de IDs 1–3 vs 10–12)

Esquema propuesto (migración con copia de seguridad previa del archivo `.db`):

```sql
CREATE TABLE IF NOT EXISTS vehiculos (
  id INTEGER PRIMARY KEY CHECK (id BETWEEN 1 AND 3),  -- = ID ArUco, única identidad del sistema
  nombre TEXT NOT NULL,
  uid_rfid TEXT UNIQUE,                               -- hex mayúsculas
  color TEXT
);
CREATE TABLE IF NOT EXISTS carreras (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  inicio_unix REAL NOT NULL, fin_unix REAL, vueltas_objetivo INTEGER, notas TEXT
);
CREATE TABLE IF NOT EXISTS vueltas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  carrera_id INTEGER NOT NULL REFERENCES carreras(id),
  vehiculo_id INTEGER NOT NULL REFERENCES vehiculos(id),
  numero INTEGER NOT NULL,
  t_esp_ms INTEGER, boot_id INTEGER,                  -- reloj ESP32 (NULL si fuente=vision)
  t_pc_unix REAL NOT NULL,                            -- t_esp convertido con ClockSync, o timestamp del frame
  duracion_ms INTEGER,
  lector INTEGER CHECK (lector BETWEEN 1 AND 3),
  fuente TEXT NOT NULL CHECK (fuente IN ('rfid','vision')),
  estado TEXT NOT NULL DEFAULT 'ok',                  -- ok | sin_rfid | sin_vision | reset_esp
  UNIQUE (carrera_id, vehiculo_id, numero)
);
CREATE TABLE IF NOT EXISTS eventos_rfid (              -- log crudo para auditoría/reproducción
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  t_pc_rx REAL NOT NULL, boot_id INTEGER, seq INTEGER, t_esp_ms INTEGER, lector INTEGER, uid TEXT
);
-- Migración de la inconsistencia: si existen filas con id 10..12, mapear 10→1, 11→2, 12→3
-- en vehiculos y en todas las FK (vueltas.vehiculo_id) dentro de UNA transacción, tras verificar
-- con SELECT que no hay colisión con ids 1..3 ya existentes. El código Python (vision.py, flassk.py,
-- database.py) debe leer los IDs de config, nunca literales.
```

- `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;` y **una sola conexión en el hilo de persistencia** (los objetos `sqlite3.Connection` no deben compartirse entre hilos por defecto), con inserciones en lote cada 200 ms.
- Posiciones continuas (x,y por frame) **no** van a SQLite a 30 Hz por auto: se guardan en un archivo NDJSON/CSV por sesión (o tabla `posiciones` submuestreada a 5–10 Hz si se requiere consulta).

### 3.7 Emisión a la web (Flask‑SocketIO existente)

- Hilo `WebEmitter` suscripto al bus; emite `lap` (inmediato), `standings` (máx. 10 Hz), `link_status` (cambio de estado del ESP32/cámaras). Usar `socketio.emit(...)` desde el hilo de fondo con `async_mode="threading"` (verificar el modo que usa `flassk.py`; con eventlet/gevent la regla cambia).
- La web no consulta SQLite en cada evento; recibe el estado por socket y usa SQLite solo para históricos.

---

## 4. Tecnologías utilizadas

- Python 3.13 (workspace) — versión en la PC del usuario **no verificada**.
- stdlib: `threading`, `queue`, `json`, `sqlite3`, `dataclasses`, `time.monotonic/perf_counter_ns`, `logging`; `os.openpty`/`tty` para simulación en Linux.
- **pyserial ≥ 3.5** — **dependencia nueva; NO instalada en el workspace** (no se puede instalar aquí). Debe ir a `requirements.txt`. Aporta `serial.tools.list_ports` (detección por VID:PID: CP2102 `10C4:EA60`, CH340 `1A86:7523`) y `serial_for_url("loop://")` para pruebas.
- Windows: driver CP210x o CH340 según la placa; `com0com` (par de COM virtuales) para el simulador en Windows.
- pytest 9.1.1 (disponible). FFmpeg 6.1.1 (workspace; versión en la PC a verificar).
- Firmware: Arduino‑ESP32 + librería MFRC522 (miguelbalboa) con timer reducido, o driver propio de máquina de estados basado en el de ESPHome; ArduinoJson para serializar (opcional; se puede usar `snprintf`).

---

## 5. Archivos que necesita crear

| Archivo | Contenido |
|---|---|
| `multicam_vision/integration/events.py` | dataclasses de eventos + `EventBus` |
| `multicam_vision/integration/protocol.py` | *(nuevo, justificado: separar el protocolo puro de la E/S para testearlo sin hardware)* `encode()`, `decode()`, validación v1, `ProtocolError` |
| `multicam_vision/integration/transport.py` | *(nuevo)* `Transport` (Protocol), `SerialTransport` (pyserial), `UdpTransport`, `FakeTransport` |
| `multicam_vision/integration/esp32.py` | `Esp32Link` (hilo lector, reconexión, ping/pong, ack/reintentos), `ClockSync` |
| `multicam_vision/integration/rfid.py` | `RfidResolver` (UID→vehiculo_id, desduplicado, `min_lap_ms`) |
| `multicam_vision/integration/fusion.py` | *(nuevo)* `RaceFusion` (vueltas, respaldo visual, standings) |
| `multicam_vision/integration/storage.py` | *(nuevo)* `RaceRepository` (SQLite WAL, hilo propio, migración IDs) |
| `multicam_vision/visualization/web_bridge.py` | *(nuevo)* `WebEmitter` hacia Flask‑SocketIO existente |
| `multicam_vision/tools/record_session.py` | grabación sincronizada de ambas cámaras + log NDJSON del ESP32 |
| `multicam_vision/tools/esp32_sim.py` | simulador de ESP32 (pty/com0com/loop://, guiones de vueltas) |
| `multicam_vision/tools/replay.py` | reproduce una sesión grabada (video + NDJSON) con su temporización original o "lo más rápido posible" |
| `multicam_vision/metrics/profiler.py` | `StageTimer` (perf_counter_ns por etapa, histogramas p50/p95/p99) |
| `firmware/esp32_rfid/esp32_rfid.ino` | firmware (fase de implementación) |
| `tests/unit/…`, `tests/integration/…`, `tests/data/` | ver §7 |
| `docs/PROTOCOLO_ESP32_v1.md` | especificación de §3.3 |

---

## 6. Interfaces que necesita (firmas Python concretas)

```python
# integration/events.py
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Literal, Protocol
import queue, threading, time

@dataclass(frozen=True, slots=True)
class Event:
    t_pc: float                      # time.monotonic() de la PC al crear/recibir el evento

@dataclass(frozen=True, slots=True)
class RfidEvent(Event):
    reader: int                      # 1..3 (carril)
    uid: str                         # hex mayúsculas
    t_esp_ms: int                    # millis() del ESP32
    seq: int
    boot_id: int
    t_esp_as_pc: float | None = None # t_esp convertido a reloj PC (None si aún no hay sync)

@dataclass(frozen=True, slots=True)
class VisualEvent(Event):
    track_id: int                    # = vehiculo_id 1..3 si está identificado, si no id temporal >= 100
    vehiculo_id: int | None
    x_mm: float
    y_mm: float
    s_mm: float | None               # progreso a lo largo de la línea central
    v_mm_s: float | None
    camera_ids: tuple[int, ...]      # cámaras que lo vieron en este instante
    frame_t_pc: float                # timestamp de captura del frame (reloj PC)
    confidence: float

@dataclass(frozen=True, slots=True)
class FinishLineCrossVisual(Event):
    vehiculo_id: int | None
    track_id: int
    frame_t_pc: float

@dataclass(frozen=True, slots=True)
class HeartbeatEvent(Event):
    t_esp_ms: int
    boot_id: int
    readers_ok: tuple[bool, bool, bool]
    loop_us_max: int
    tx_drop: int

class LinkState(Enum):
    DISCONNECTED = "disconnected"; CONNECTING = "connecting"; CONNECTED = "connected"; STALE = "stale"

@dataclass(frozen=True, slots=True)
class LinkStatusEvent(Event):
    source: str                      # "esp32", "cam1", "cam2"
    state: LinkState
    detail: str = ""

@dataclass(frozen=True, slots=True)
class LapEvent(Event):
    carrera_id: int
    vehiculo_id: int
    numero: int
    duracion_ms: int | None
    t_esp_ms: int | None
    lector: int | None
    fuente: Literal["rfid", "vision"]
    estado: Literal["ok", "sin_rfid", "sin_vision", "reset_esp"]

@dataclass(frozen=True, slots=True)
class StandingsEvent(Event):
    order: tuple[int, ...]           # vehiculo_id en orden 1°, 2°, 3°
    laps: dict[int, int]
    gaps_ms: dict[int, int | None]

class EventBus:
    """Pub‑sub en proceso. publish() nunca bloquea; colas acotadas por suscriptor."""
    def __init__(self, default_maxsize: int = 1024) -> None: ...
    def subscribe(self, name: str, types: tuple[type[Event], ...] | None = None,
                  maxsize: int | None = None) -> "queue.Queue[Event]": ...
    def unsubscribe(self, name: str) -> None: ...
    def publish(self, event: Event) -> None: ...
    def stats(self) -> dict[str, dict[str, int]]: ...   # {sub: {"published":…, "dropped":…, "qsize":…}}

# integration/protocol.py
PROTOCOL_VERSION: int = 1
MAX_LINE_BYTES: int = 256
class ProtocolError(ValueError): ...
def encode(msg: dict) -> bytes: ...                       # json compacto + b"\n"; valida tamaño
def decode(line: bytes) -> dict: ...                      # valida v, type, seq y campos por tipo
class LineFramer:
    def feed(self, data: bytes) -> list[bytes]: ...       # acumula y devuelve líneas completas
    oversize_dropped: int

# integration/transport.py
class Transport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def read(self, max_bytes: int, timeout_s: float) -> bytes: ...   # b"" si timeout
    def write(self, data: bytes) -> None: ...
    @property
    def is_open(self) -> bool: ...

@dataclass
class SerialConfig:
    port: str | None = None                 # None = autodetectar por VID:PID
    vid_pid: tuple[tuple[int, int], ...] = ((0x10C4, 0xEA60), (0x1A86, 0x7523))
    baud: int = 921600
    read_timeout_s: float = 0.05
    avoid_reset_on_open: bool = True        # dtr=False, rts=False antes de abrir

class SerialTransport: ...                  # implementa Transport con pyserial
class UdpTransport: ...                     # (host, port) — alternativa futura
class FakeTransport: ...                    # alimentado por lista/guion o par pty, para tests

# integration/esp32.py
class ClockSync:
    def __init__(self, keep: int = 32, best_fraction: float = 0.25) -> None: ...
    def add_sample(self, t1_pc: float, t2_esp_ms: int, t3_esp_ms: int, t4_pc: float) -> None: ...
    def reset(self) -> None: ...                         # al cambiar boot_id
    @property
    def ready(self) -> bool: ...
    def offset_s(self) -> float: ...                     # t_esp - t_pc
    def drift_ppm(self) -> float | None: ...             # regresión sobre muestras de bajo RTT
    def esp_to_pc(self, t_esp_ms: int) -> float: ...
    def uncertainty_s(self) -> float: ...                # ~ RTT_min/2

@dataclass
class Esp32LinkConfig:
    serial: SerialConfig
    heartbeat_timeout_s: float = 3.0
    reconnect_backoff_s: tuple[float, float] = (0.5, 5.0)
    ping_period_s: float = 5.0
    ack_timeout_s: float = 0.2
    ack_retries: int = 3

class Esp32Link:
    def __init__(self, bus: EventBus, transport: Transport, cfg: Esp32LinkConfig,
                 raw_log_path: str | None = None) -> None: ...
    def start(self) -> None: ...                         # lanza hilo lector daemon; retorna enseguida
    def stop(self, timeout_s: float = 2.0) -> None: ...
    def send_light_cmd(self, target: Literal["semaforo", "lane", "all"], *,
                       state: str | None = None, lane: int | None = None,
                       color: str | None = None, mode: str = "solid",
                       period_ms: int | None = None, step_ms: int | None = None) -> int: ...  # devuelve seq; no bloquea
    @property
    def state(self) -> LinkState: ...
    @property
    def clock(self) -> ClockSync: ...
    def stats(self) -> dict[str, int]: ...   # lines_ok, lines_bad, seq_gaps, reconnects, acks_failed

# integration/rfid.py
class RfidResolver:
    def __init__(self, uid_to_vehicle: dict[str, int], dedup_ms: int = 500, min_lap_ms: int = 1500) -> None: ...
    def resolve(self, ev: RfidEvent) -> int | None: ...       # vehiculo_id o None (desconocido/duplicado)

# integration/fusion.py
class RaceFusion:
    def __init__(self, bus: EventBus, resolver: RfidResolver, fusion_window_ms: int = 300,
                 count_vision_only_laps: bool = False) -> None: ...
    def start(self, carrera_id: int) -> None: ...
    def stop(self) -> None: ...
    def on_event(self, ev: Event) -> list[Event]: ...         # puro/determinista → testeable sin hilos

# integration/storage.py
class RaceRepository:
    def __init__(self, db_path: str, bus: EventBus, batch_ms: int = 200) -> None: ...
    def migrate(self, backup: bool = True) -> dict[str, int]: ...   # incluye 10..12 → 1..3
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def new_race(self, vueltas_objetivo: int | None = None) -> int: ...

# metrics/profiler.py
class StageTimer:
    def __init__(self, name: str, maxlen: int = 10_000) -> None: ...
    def __enter__(self) -> "StageTimer": ...
    def __exit__(self, *exc) -> None: ...
    def summary(self) -> dict[str, float]: ...   # n, fps, p50_ms, p95_ms, p99_ms, max_ms
```

`RaceFusion.on_event` es una función pura (estado interno + evento → eventos de salida) para poder testearla con secuencias guionadas sin hilos ni tiempo real.

---

## 7. Plan de pruebas por niveles

### 7.1 Nivel 0 — Unitarias (pytest, sin hardware, < 5 s en total)

| Prueba | Qué verifica |
|---|---|
| `test_protocol_roundtrip` | encode/decode de todos los tipos de §3.3; ejemplos del documento como fixtures |
| `test_protocol_rejects` | `v≠1`, falta de campos, `reader∉1..3`, UID no hex, línea > 256 B, JSON roto |
| `test_line_framer` | líneas partidas en varios `feed`, `\r\n`, basura entre líneas, líneas gigantes |
| `test_seq_gaps_and_wrap` | detección de huecos y envoltura 2³²−1→0; cambio de `boot` |
| `test_clock_sync` | datos sintéticos con offset/deriva/RTT asimétrico conocido → error < tolerancia (portar Exp. 2) |
| `test_event_bus_nonblocking` | `publish` con cola llena retorna en < 1 ms y cuenta `dropped`; filtro por tipo |
| `test_rfid_resolver` | UID desconocido, desduplicado entre lectores, `min_lap_ms` |
| `test_fusion_rules` | guiones: RFID+visión, solo RFID, solo visión, reinicio de ESP32, standings con empate |
| `test_repository_memory` | `:memory:`: esquema, unicidad, migración 10..12→1..3 con datos de ejemplo |

### 7.2 Nivel 1 — Componentes con simulador de ESP32

- **Fuente de eventos fake**: `FakeTransport` con guion (`[(t, línea), …]`) → `Esp32Link` → `EventBus`.
- **Puerto virtual**: Linux: par pty (como Exp. 2); Windows: `com0com`; multiplataforma con pyserial: `serial_for_url("loop://")`.
- `tools/esp32_sim.py` reproduce el comportamiento del firmware: `hello`, heartbeats, vueltas guionadas (p. ej. 3 autos, 20 vueltas, ruido de doble lectura y lector vecino), respuestas a ping con retardo configurable, `ack` y fallos inyectados.
- Casos: desconexión a mitad de carrera (cerrar el pty) → `LinkStatusEvent(DISCONNECTED)` y reconexión con backoff; reinicio del ESP32 (`boot` nuevo); líneas corruptas; ráfaga de 1000 eventos; `light_cmd` sin `ack` → 3 reintentos y error.
- **Prueba de no‑bloqueo** (portar Exp. 1 como test con umbrales): bucle de 30 Hz + `Esp32Link` contra simulador con lecturas lentas → 0 frames > 1.5 × período en 600 frames.

### 7.3 Nivel 2 — Integración offline con video grabado (clave para la reproducibilidad)

**Grabación de sesiones reales** (una vez, en la pista real, con las dos cámaras y el ESP32):

```bash
# Opción A: copia del MJPEG original sin recodificar (cada JPEG queda idéntico a lo que ve el sistema)
ffmpeg -hide_banner -use_wallclock_as_timestamps 1 -f mjpeg -i http://<IP_CAM1>:8080/video \
       -c:v copy -f matroska cam1_<sesion>.mkv
ffmpeg -hide_banner -use_wallclock_as_timestamps 1 -f mjpeg -i http://<IP_CAM2>:8080/video \
       -c:v copy -f matroska cam2_<sesion>.mkv
# Extraer los timestamps por frame para el replay:
ffprobe -v error -select_streams v:0 -show_entries packet=pts_time -of csv=p=0 cam1_<sesion>.mkv > cam1_<sesion>.ts.csv
```

- **Opción B (preferida para el sistema)**: `tools/record_session.py` graba con el mismo módulo de captura del sistema: guarda cada JPEG tal cual llega + CSV `frame_idx, t_pc_monotonic, t_pc_unix, bytes`, y en paralelo el log NDJSON crudo del ESP32 con `t_pc_rx`. Así el replay usa **exactamente** los timestamps que vio el sistema en vivo.
- Sesiones a grabar (mínimo): 1 auto lento, 3 autos en carrera, cruces en la zona compartida entre cámaras, iluminación con sombras de la tira LED, oclusiones (mano), una desconexión de cámara.
- **Verdad de terreno**: anotación manual (CSV) de (a) frame del cruce de meta por auto, (b) posición de cada auto cada N frames en una sub‑muestra, (c) identidad. Herramienta mínima de anotación con OpenCV (`tools/annotate.py`). El ArUco del techo sirve como referencia de posición, validado contra puntos medidos con cinta métrica.
- `tools/replay.py` reproduce video + NDJSON en tiempo original o acelerado; los tests de integración corren el pipeline completo y comparan contra la anotación. **Los clips grabados se versionan como datos de prueba** (con hash SHA‑256 en un manifiesto) para que cada cambio de algoritmo se evalúe sobre las mismas entradas.

### 7.4 Nivel 3 — Hardware en el lazo (ESP32 real)

| ID | Prueba | Método | Criterio |
|---|---|---|---|
| HIL‑1 | Latencia y fiabilidad USB | 10 000 ping/pong; RTT p50/p99/máx; 1 h de heartbeats; contar huecos de `seq` | 0 huecos; reportar RTT medido |
| HIL‑2 | **Tasa de lectura RFID vs velocidad** | ≥ 50 pasadas por velocidad (≈ 0.5, 1, 2, 3, 4, 5 m/s) por carril; velocidad medida por video (dos marcas a distancia conocida / frames) o por dos lectores separados una distancia fija | % lecturas por velocidad (con intervalo de confianza); decide si hace falta el respaldo visual |
| HIL‑3 | Tiempos del firmware | `loop_us_max`, duración REQA/anticolisión/SELECT con `micros()`, costo de conmutar MISO | Ciclo de 3 lectores medido |
| HIL‑4 | Interferencia entre carriles | Etiqueta en el borde de cada carril → qué lector la lee | Tabla de lecturas cruzadas |
| HIL‑5 | Altura / material | Distancia etiqueta‑lector 5–20 mm, a través de chapadur, con/sin batería encima | Distancia máxima fiable |
| HIL‑6 | Robustez | Desenchufar USB, reiniciar ESP32, abrir/cerrar el puerto (¿auto‑reset por DTR?) | Reconexión automática sin perder el resto del sistema |

### 7.5 Nivel 4 — Sistema

- Carrera completa de 10 min con 3 autos + soak de 1 h (memoria, colas, FPS estables).
- Desconexión de una cámara y del ESP32 durante la carrera: el sistema sigue y lo registra.
- Comparación de tiempos de vuelta RFID vs anotación de video vs cronómetro.

### 7.6 Métricas de rendimiento y cómo medirlas (sin inventar)

| Métrica | Cómo se mide | Herramienta |
|---|---|---|
| FPS por etapa (captura, decodificación, rectificación, detección, tracking, fusión, render) | `StageTimer` con `perf_counter_ns` alrededor de cada etapa; p50/p95/p99/máx y FPS sostenido por cámara | `metrics/profiler.py`, volcado a CSV cada 10 s |
| Frames descartados / obsoletos | contadores en captura (frames sustituidos antes de procesarse) | captura |
| Latencia de pipeline (frame recibido → evento publicado) | `VisualEvent.t_pc − frame_t_pc` | eventos |
| **Latencia extremo a extremo (fotón → evento)** | El ESP32 enciende un LED visible por ambas cámaras con `light_cmd`; el ESP32 reporta `t_esp` del encendido (ack). Se detecta el primer frame con el LED encendido; latencia = `t_evento_detectado − esp_to_pc(t_esp_encendido)`, ± incertidumbre de sync (medida) + ½ período de cámara | prueba dedicada; incluye la latencia de la cámara IP por Wi‑Fi, hoy desconocida |
| Latencia RFID → web | `t_pc` de recepción → timestamp del `emit` (y en el navegador, `Date.now()` con reloj de la misma PC) | logs |
| % detección | frames con el auto detectado / frames donde la anotación dice que está visible, por cámara y en zona compartida | replay + anotación |
| Intercambios de ID (IDSW) | nº de cambios de identidad sobre las trayectorias anotadas (estilo MOTA/IDF1) | replay + anotación |
| Error en mm | puntos de control medidos con cinta (±1 mm) y ArUco colocados en posiciones conocidas; error = proyección por homografía − posición medida; RMSE y máx. por zona, por cámara y en la zona compartida | calibración + replay |
| % lectura RFID y vueltas | HIL‑2; vueltas RFID vs anotación (faltantes, dobles) | HIL / sistema |
| Jitter del bucle de video bajo carga RFID | portado de Exp. 1 (frames > 1.5 × período) | test nivel 1 |

Regla del equipo: un número solo entra a la documentación con el script, la sesión grabada (hash) y la máquina que lo produjeron.

---

## 8. Riesgos y limitaciones

1. **Lectura RFID a > 3 m/s no garantizada** con RC522 + NTAG213 (cálculo §2.2). Mitigación: timeout de 1–2 ms, REQA simultáneo, UID de 4 bytes, ganancia máxima, respaldo visual de cruce. Se decide con datos de HIL‑2.
2. **Alcance ≈ 2 cm** del NTAG213 con RC522: poco margen con chapadur + altura del auto; metal/batería cerca de la etiqueta lo empeora.
3. **RST compartido** (D17): causa plausible del síntoma "solo funciona el último lector"; la solución propuesta (un solo pulso + soft reset) es hipótesis hasta probarla en banco.
4. **MISO separados**: requiere conmutar la entrada MISO por lector; costo y estabilidad a medir.
5. **pyserial no está instalado en el workspace** → `SerialTransport` no se puede probar aquí; solo con pty/`FakeTransport`. Dependencia obligatoria en la PC del usuario.
6. **Auto‑reset del ESP32 al abrir el puerto** (DTR/RTS): puede reiniciar el firmware en cada reconexión; el protocolo lo detecta (`boot`), pero conviene evitarlo (dtr/rts=False).
7. **GIL**: trabajo CPU en Python en hilos auxiliares degrada el video (Exp. C: trabajo/frame p50 hasta 21.4 ms). Mantener los hilos auxiliares livianos; reserva: proceso separado.
8. **Latencia de las cámaras IP Wi‑Fi desconocida** → la ventana de fusión visual/RFID es provisoria hasta medirla con la prueba del LED.
9. Los experimentos se corrieron en un workspace Linux de 2 CPU con video sintético; **no** representan la PC Ryzen/Windows ni el USB real.
10. Si se migra a Wi‑Fi: DHCP sin reserva (ya visto con las cámaras) y *modem‑sleep* → reserva DHCP/IP fija y `WIFI_PS_NONE`.

---

## 9. Pruebas necesarias (resumen ejecutable)

1. Nivel 0 completo en pytest (workspace y PC del usuario).
2. Simulador ESP32 + test de no‑bloqueo (nivel 1) — ejecutable en el workspace con pty.
3. Grabar ≥ 6 sesiones reales con ambas cámaras + log ESP32 (nivel 2), anotarlas y versionarlas con hash.
4. HIL‑1…HIL‑6 en banco con el ESP32 real (en especial **HIL‑2: tasa de lectura vs velocidad**).
5. Prueba del LED para latencia extremo a extremo.
6. Carrera de 10 min + soak de 1 h.

---

## 10. Estado del trabajo

| Elemento | Estado |
|---|---|
| Investigación enlace serial/Wi‑Fi, RC522, sincronización | **Implementado** (este informe) |
| Protocolo JSON v1 (especificación y ejemplos) | **Implementado** como especificación; no implementado en código |
| Prototipo `EventBus` + productor RFID lento + video 30 Hz (Exp. 1, 1b) | **Probado** en el workspace (2 corridas; números en §2.5) |
| Simulador ESP32 por pty + parser NDJSON + `ClockSync` (Exp. 2) | **Probado** en el workspace con retardo simulado; **no** con USB real |
| Cálculo de lectura RFID vs velocidad | **Parcial** (cálculo con supuestos; falta HIL‑2) |
| Hipótesis RST compartido / conmutación MISO | **Pendiente** (verificar en banco) |
| Firmware ESP32 | **Pendiente** |
| Módulos `integration/*`, `storage`, `web_bridge`, herramientas de grabación/replay | **Pendiente** |
| Migración SQLite 10..12 → 1..3 | **Pendiente** (esquema propuesto; no se tuvo acceso al `.db` real) |
| Grabación de clips reales con FFmpeg | **Pendiente** (requiere acceso a las cámaras; no hay red a ellas desde el workspace) |
| `SerialTransport` con pyserial | **Pendiente** (pyserial no instalado en el workspace) |

### Fuentes

- [Arduino forum — RFID‑RC522, PICC_IsNewCardPresent() command delay is 27](https://forum.arduino.cc/t/rfid-rc522-picc_isnewcardpresent-command-delay-is-27/625691)
- [ESPHome — rc522.cpp (driver no bloqueante)](https://api-docs.esphome.io/rc522_8cpp_source)
- [NXP Community — MFRC522 NTAG213 range improvements](https://community.nxp.com/t5/NFC/MFRC522-NTAG213-range-improvements/m-p/653895)
- [Arduino forum — ESP32 und multiple RC522: nur das zuletzt initialisierte funktioniert](https://forum.arduino.cc/t/esp32-und-multiple-rc522-nur-das-zuletzt-initialisierte-funktioniert/1021895)
- [element14 — Using multiple MFRC522 RFID readers on one Arduino](https://community.element14.com/products/arduino/f/arduino-forum/28599/using-multiple-mfrc522-rfid-readers-on-one-arduino)
- [Arduino forum — Multiple RC522 on an SPI bus won't work](https://forum.arduino.cc/t/multiple-rc522-on-an-spi-bus-wont-work/1161286)
- [Espressif ESP‑IDF — Low Power Mode in Wi‑Fi Scenarios](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-guides/low-power-mode/low-power-mode-wifi.html)
- [Project Gus — Notes on FTDI latency with Arduino](https://www.projectgus.com/2011/10/notes-on-ftdi-latency-with-arduino/)
- [Zaber — Improving performance of FTDI adapters (latency timer)](https://software.zaber.com/motion-library/docs/guides/communication/ftdi_latency)
- [ESP32 forum — UDP packet delays](https://esp32.com/viewtopic.php?p=97001) (consultado; contenido no accesible por verificación anti‑bot, no se usan sus cifras)

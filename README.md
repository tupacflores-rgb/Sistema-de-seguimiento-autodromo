# MultiCam_Vision — Autódromo 1:24

Estado general:
- **Arquitectura del sistema completo:** propuesta, pendiente de aprobación formal (ver `docs/ARQUITECTURA_Y_VIABILIDAD.md`).
- **Página de administrador:** implementada y probada con cámaras IP Webcam **simuladas** (79 pruebas automáticas + prueba en navegador). Incluye cámaras, video, configuración, pista con dibujo vinculado, vehículos, tracking en vivo (con un **modo demo** que simula autos y una carrera sin cámaras ni hardware), semáforo de salida + RFID, y una pantalla de solo lectura (`/display`) para mostrar posiciones y velocidad en un teléfono o tablet aparte. Guarda un respaldo automático antes de cada cambio (botón **Respaldos**, arriba, con historial filtrable por tipo) y se puede medir en mm o cm. Las plantillas de tamaño de plancha se pueden guardar propias, además de las predefinidas. **Falta probarla con los teléfonos, los LED, la pista y el ESP32 reales, y en Windows.**
- **ESP32 (semáforo de salida + 3 lectores RFID):** implementado y probado **en simulación** (sin hardware ni pyserial). El firmware real (`firmware/esp32_race_control/`) está escrito pero **no se probó en una placa física todavía**.

---

## 1. Página de administrador: qué es

Es una página web **local**: un pequeño servidor (Flask) que corre en tu PC y que abrís con el navegador. Tiene siete secciones separadas, más el botón **Tracking en vivo** arriba a la derecha:

| Sección | Para qué sirve |
|---|---|
| **1 · Cámaras** | Cargar la IP, el puerto y la ruta de cada teléfono con IP Webcam (también se puede pegar la URL completa). Usuario y contraseña son opcionales. Cada cámara se habilita o deshabilita por separado y tiene un botón **Probar conexión**. |
| **2 · Video en vivo** | Ver todas las cámaras habilitadas con las métricas que llegan realmente a la PC: FPS recibidos, resolución, edad del último frame, KB por frame, reconexiones y último error. |
| **3 · Configuración** | Cambiar los ajustes de cada teléfono (resolución, calidad, foco, orientación…), el zoom, la linterna y el botón **Aplicar recomendado** (zoom 1x + 4:3). También los parámetros del sistema: edad máxima de frame, corrección de latencia y FPS objetivo. |
| **4 · Pista** | Dibujar la pista. Capas: línea central, borde exterior, **islas interiores** (un circuito en 8 tiene dos) y zonas a ignorar. El plano puede mostrar de fondo la **foto enderezada** de una cámara calibrada. Tres vistas: **Plano (mm)**; **Vinculada**, donde se marcan puntos de referencia, se dibuja una sola vez y el dibujo aparece en el plano y en las fotos de las dos cámaras a la vez; y **Solo cámara (px)**, para capturar la foto de referencia y dibujar zonas propias de esa cámara. |
| **5 · Vehículos** | Elegir cuántos autos hay (1 a 4) y el **color del LED RGB** de cada uno. El color se puede **tomar desde la cámara** con un clic sobre el LED. Hay tolerancias de tono, saturación y brillo, áreas mínima y máxima, y una **vista previa** de lo que detecta cada cámara. Avisa si dos colores se parecen demasiado. También se puede cargar el **UID de la etiqueta RFID** de cada auto (sección 7). |
| **6 · Tracking** | Seguimiento **en tiempo real** por color + movimiento: plano con cada auto, su estela y su velocidad, tabla de posiciones (1°, 2°…), vueltas y tiempos, y video de cada cámara con las detecciones marcadas. Botón **🧪 Modo demo**: simula autos y una carrera sin cámaras ni hardware (no necesita OpenCV/NumPy), para probar esta sección y `/display` antes de tener todo armado. |
| **7 · ESP32 y semáforo** | Semáforo de salida (6 LEDs) y 3 lectores RFID por USB Serial. Ver la sección 9 de abajo. |

El navegador nunca se conecta directo a los teléfonos: el servidor de la PC recibe el video y lo reenvía. Así cada teléfono tiene **una sola conexión** aunque haya varias pestañas abiertas. Si nadie mira el video durante 30 s, la conexión se cierra para no gastar Wi‑Fi.

---

## 2. Dependencias

### Para usar la página (obligatorio)

| Dependencia | Versión | Para qué | Cómo se instala |
|---|---|---|---|
| **Python** | 3.10 o superior | Ejecuta el servidor | https://www.python.org/downloads/ (al instalar, marcar **"Add python.exe to PATH"**) |
| **Flask** | 3.0 o superior | Servidor web | `python -m pip install flask` |

Flask instala solo sus propias dependencias: Werkzeug, Jinja2, MarkupSafe, itsdangerous, click y blinker (y colorama en Windows).

El resto usa solo la librería estándar de Python. El navegador no descarga nada de internet (no usa CDN), así que funciona sin conexión a internet, solo con la red local.

### Para el tracking y la vista previa de detección (secciones 5 y 6)

| Dependencia | Versión | Cómo se instala |
|---|---|---|
| **OpenCV** (contrib) | 4.8 o superior (tu código anterior usaba 5.0.0) | `python -m pip install opencv-contrib-python` |
| **NumPy** | 1.26 o superior | `python -m pip install numpy` |

Sin OpenCV la página igual funciona: secciones 1 a 4 completas, y en 5 se pueden elegir colores y tomarlos desde la cámara, porque eso se calcula en el navegador. Lo único que no anda es el tracking y la vista previa de detección; la página muestra un aviso con el comando de instalación.

### Para correr las pruebas (opcional)

| Dependencia | Para qué |
|---|---|
| **pytest** ≥ 8 | Ejecutar `tests/test_admin.py` |
| **opencv-contrib-python** ≥ 4.8 y **numpy** ≥ 1.26 | Generar el video de la cámara simulada (`tests/fake_ipwebcam.py`) |

Para instalar todo junto: `python -m pip install -r requirements.txt`

### En los teléfonos

- App **IP Webcam** (Android) con el servidor iniciado (botón **"Iniciar servidor"** dentro de la app).
- El teléfono y la PC tienen que estar en la **misma red Wi‑Fi**.
- Recomendado: reservar una IP fija para cada teléfono en el router (la IP cambió entre pruebas: .103, .109, .110).

### Para verificar lo instalado

```
python 00_inspeccion_entorno.py
```

Muestra las versiones de Python, Flask, OpenCV, NumPy, FFmpeg y pyserial, y guarda el informe en `logs/entorno_<fecha>.json`.

---

## 3. Dónde está

Todo está en esta carpeta:

```
C:\Users\Rolando\Desktop\Arreglo de electronica-Autodromo\MultiCam_Vision\
```

```
MultiCam_Vision\
├── INICIAR_ADMIN.bat          ← doble clic para iniciar la página
├── requirements.txt           ← dependencias
├── README.md                  ← este archivo
├── 00_inspeccion_entorno.py   ← verifica versiones instaladas y FPS de las cámaras
├── admin\                     ← código de la página de administrador
│   ├── __main__.py            ← punto de entrada (python -m admin)
│   ├── server.py              ← rutas web y API
│   ├── mjpeg.py               ← lector de video de cada cámara (un hilo por cámara, reconexión automática)
│   ├── ipwebcam.py            ← control de los ajustes de IP Webcam
│   ├── config_store.py        ← guardado y validación de cámaras, pista, calibración y vehículos
│   ├── homography.py          ← calibración preliminar: homografía foto ↔ plano (Python puro)
│   ├── tracking.py            ← seguimiento por color + movimiento (OpenCV), fusión de cámaras en mm
│   ├── templates\admin.html   ← la página
│   └── static\                ← estilos (admin.css) y lógica (admin.js, track_editor.js)
├── tests\
│   ├── test_admin.py          ← pruebas de cámaras, video, configuración y pista
│   ├── test_tracking.py       ← pruebas de calibración, vehículos y tracking (miden el error en mm)
│   ├── test_esp32.py          ← pruebas del semáforo de salida y los lectores RFID (simulados)
│   ├── test_demo_mode.py      ← pruebas del modo demo (no necesitan OpenCV/NumPy)
│   └── fake_ipwebcam.py       ← IP Webcam simulada (también una escena de 2 cámaras con autos con LED)
├── config\cameras.json        ← (se crea solo) cámaras configuradas
├── config\vehicles.json       ← (se crea solo) cantidad de autos y color de cada LED
├── data\track\                ← (se crea solo) pista e imágenes de referencia
└── docs\                      ← arquitectura, registro central, informes y experimentos
```

---

## 4. Cómo iniciar la página

### Primera vez (una sola vez)

1. Abrí una terminal (**cmd** o **PowerShell**) y verificá Python:
   ```
   python --version
   ```
   Si dice *"no se reconoce como un comando"*, probá con `py --version`. En ese caso, en todos los comandos de abajo usá `py` en lugar de `python`.
2. Instalá Flask:
   ```
   python -m pip install flask
   ```

### Cada vez que la quieras usar

**Opción A — doble clic (la más simple)**

Hacé doble clic en `INICIAR_ADMIN.bat`. Se abre una ventana negra (el servidor) y a los 2 segundos el navegador en la página. **No cierres la ventana negra mientras uses la página.**

**Opción B — desde la terminal**

```
cd "C:\Users\Rolando\Desktop\Arreglo de electronica-Autodromo\MultiCam_Vision"
python -m admin
```

Después abrí en el navegador: **http://127.0.0.1:5050**

> Importante: el comando `python -m admin` se ejecuta **desde la carpeta `MultiCam_Vision`**. Si lo ejecutás desde otra carpeta aparece el error *"No module named admin"*.

**Para cerrarla:** en la ventana negra, apretá **Ctrl + C** (o cerrá la ventana).

**Si ya había una página abierta** (por ejemplo la versión anterior que quedó en otra ventana negra), al iniciar se **cierra sola** y la reemplaza la nueva. En la ventana aparece *"Había otra página de administrador abierta… Cerrándola… Listo."*. Esto es importante en Windows: si quedaban dos servidores en el mismo puerto, el navegador seguía hablando con el viejo y los cambios no se veían.

**Versión:** la versión de la página se ve arriba a la izquierda (por ejemplo `v0.3.0`). Si se actualizan los archivos con la página abierta, aparece una franja naranja:
- *"Hay una versión nueva de la página"* → tocá **Recargar ahora**.
- *"Se actualizó el programa: cerrá la ventana negra…"* → cerrá la ventana del servidor y abrí `INICIAR_ADMIN.bat` de nuevo.

### Opciones

| Comando | Qué hace |
|---|---|
| `python -m admin` | Solo accesible desde esta PC (`http://127.0.0.1:5050`) |
| `python -m admin --port 8090` | Usa otro puerto si el 5050 está ocupado |
| `python -m admin --host 0.0.0.0` | Accesible desde el celular u otra PC de la red: `http://IP-DE-LA-PC:5050`. **No tiene contraseña**: usalo solo en tu red de casa. Windows puede pedir permiso en el Firewall: aceptá "Redes privadas". |
| `python -m admin --debug` | Muestra mensajes detallados en la ventana negra |
| `python -m admin --help` | Lista todas las opciones |

Las mismas opciones se pueden pasar al `.bat`, por ejemplo `INICIAR_ADMIN.bat --port 8090`. Ojo: el `.bat` siempre abre el navegador en el puerto 5050.

---

## 5. Primer uso paso a paso

1. En el teléfono, abrí **IP Webcam** → **Iniciar servidor**. La app muestra la dirección, por ejemplo `http://192.168.1.110:8080`.
2. En la página, entrá a **1 · Cámaras**, cargá esa IP en *Cámara 1*, dejá el puerto en `8080` y la ruta en `/video`. Activá el interruptor y tocá **Guardar**.
3. Tocá **Probar conexión**. Tienen que aparecer dos líneas OK: *Control* (ajustes) y *Video*.
4. En **3 · Configuración**, tocá **Aplicar recomendado**: zoom mínimo y resolución 4:3, para usar todo el sensor.
5. En **2 · Video en vivo**, revisá FPS y retraso.
6. En **4 · Pista**:
   - **Plano (mm)**: cargá el largo y el ancho reales de la plancha y dibujá la línea central, los bordes y las zonas a ignorar.
   - **Solo Cámara 1 (px)**: tocá **Capturar imagen actual** (hacelo con cada cámara).
   - **Vinculada**: marcá los puntos de referencia y dibujá una sola vez para las dos cámaras (ver más abajo).
   - Tocá **Guardar pista** (o **Ctrl + S**).
7. En **5 · Vehículos**: elegí cuántos autos hay, tomá el color del LED de cada uno desde la cámara y guardá.
8. Tocá **Tracking en vivo** (arriba a la derecha).

### Controles del editor de pista

| Acción | Cómo |
|---|---|
| Agregar punto (al final de la capa activa) | Clic |
| Mover punto | Arrastrar |
| Borrar punto | Clic derecho sobre el punto |
| Insertar punto en un tramo | Shift + clic sobre el tramo |
| Zoom | Rueda del mouse |
| Desplazar la vista | Espacio + arrastrar, o botón central del mouse |
| Deshacer / Guardar | Ctrl + Z / Ctrl + S |

El **primer punto** de la línea central es la **largada**, y el orden de los puntos (la flecha) marca el **sentido de carrera**.

En el plano, el origen (0,0) es la **esquina superior izquierda** de la plancha: X va a lo largo e Y hacia abajo, a lo ancho.

### Dibujar sobre las dos cámaras a la vez (vista «Vinculada»)

Para que lo dibujado aparezca en las dos fotos, la página tiene que saber qué punto de cada foto corresponde a qué punto de la plancha. Para eso se marcan **puntos de referencia**:

1. **Preparación:** pegá en la plancha 5 a 8 marcas fijas fáciles de ver (cruces de cinta, esquinas, círculos). Medí con cinta métrica la posición de cada una en mm, desde la esquina superior izquierda. Algunas deberían quedar en la zona que ven **las dos** cámaras.
2. **Foto de referencia:** en **Pista → Solo Cámara X (px)**, tocá **Capturar imagen actual** para cada cámara.
3. **Puntos:** en **Pista → Vinculada**, elegí el modo **Puntos de referencia**. Si 4 de las marcas son las esquinas de la plancha (lo más común), tocá **+ 4 esquinas**: crea los 4 puntos con su posición en el plano ya cargada, y solo falta marcarlos en las fotos. Para una marca que no sea una esquina:
   - tocá **+ Punto suelto** y escribí su X e Y en mm (o hacé clic sobre el plano);
   - hacé clic sobre esa misma marca en la foto de cada cámara que la vea.
4. **Calibración:** con **4 o más** puntos por cámara, esa cámara queda calibrada. El panel muestra el **error medio en mm**, que solo sirve como control con 5 o más puntos. En las fotos, una línea roja corta une cada clic con donde la calibración "cree" que está el punto: si es larga, ese punto está mal medido o mal marcado.
5. **Dibujo:** pasá al modo **Dibujar pista** y dibujá sobre **cualquiera** de los tres lienzos (foto A, foto B o plano). Lo dibujado se guarda en mm y aparece en los tres.
6. Tocá **Guardar pista**.

Si movés una cámara, cambiás el zoom o cambiás la resolución, recapturá su foto y volvé a marcar sus puntos. Si cambia la resolución, los puntos de esa cámara se borran solos.

### Pista en forma de 8 y cámaras en ángulo

Las cámaras en ángulo ven toda la pista pero **deformada**: lo lejano se ve más chico y las rectas convergen. La calibración con puntos de referencia corrige esa deformación. Todas las posiciones se calculan en milímetros reales sobre el plano, así que la deformación no afecta las medidas. Para el 8:

1. **Puntos de referencia:** cuando las dos cámaras ven toda la pista, conviene que **todos** los puntos se vean en las dos fotos. Repartilos: 4 esquinas, el centro de los lados largos y uno en el medio de cada lazo.
2. **Foto enderezada:** después de guardar la calibración, en **Pista → Plano (mm)** elegí **Fondo del plano: Foto enderezada · Cámara A**. Vas a ver la pista desde arriba, sin deformación, y podés calcar el 8 encima.
3. **Capas:**
   - **Línea central:** recorré el 8 completo por el medio del carril, pasando **dos veces por el cruce**, en el orden en que corren los autos. Marcala como **cerrada**. El primer punto es la largada.
   - **Borde exterior:** el contorno de toda la pista.
   - **Islas interiores:** tocá **+ Nueva isla** dos veces y dibujá el interior de cada lazo, lo que **no** es pista.
4. **Cruce del 8:** el seguimiento recuerda por qué lazo venía cada auto, así que no se confunde en el cruce y las vueltas se cuentan bien.

**Altura del LED (importante con cámaras en ángulo):** la calibración ubica los puntos **del piso**. El LED está arriba del auto, así que cada cámara lo "ve" corrido hacia atrás, y en direcciones distintas desde cada punta. En la simulación, con cámaras a 1,4 m, sin corregir el error fue de 36 a 197 mm. Por eso:
- en **5 · Vehículos → Detección**, cargá la **Altura del LED sobre la pista (mm)**, medida con regla;
- la posición y la altura de cada cámara **se calculan solas** al guardar la calibración, y se muestran en el panel de puntos de referencia (por ejemplo *"Cámara estimada a 1,41 m de altura…"*). Compará ese número con la altura real: si es muy distinto, algún punto está mal marcado.
- Con la corrección, en la simulación el error bajó a **11–15 mm** de media.

**LED lejanos:** del lado opuesto a cada cámara el LED se ve chico (pocos píxeles). Si un auto se pierde en la punta lejana, bajá el **Área mínima** del vehículo (por ejemplo a 4–8 px), usá un LED más grande o con difusor, o procesá a 1280 px. Igual, la otra cámara lo ve de cerca.

### Vehículos y color del LED

1. **Colores:** en **5 · Vehículos**, elegí la cantidad de autos y un color por auto. Usá tonos bien separados: rojo, verde, azul y magenta funcionan bien.
2. **Tomar color desde la cámara:** con el auto quieto y el LED encendido, tocá **Tomar color desde cámara** y hacé clic sobre el **borde de color** del LED, no sobre el centro, que la cámara suele ver blanco. Se ajustan solos el color y los mínimos de saturación y brillo.
3. **Ver detección:** muestra en vivo qué píxeles reconoce cada cámara como ese auto. Tiene que aparecer **1 blob válido** donde está el auto y nada más. Si aparecen manchas en otros lugares, subí la saturación o el brillo mínimos, bajá la tolerancia de tono, o dibujá una **zona a ignorar**.
4. Tocá **Guardar vehículos**.

Consejos para los LED: un difusor (cinta blanca o papel manteca encima) hace el punto más grande y estable. Conviene que el LED apunte hacia arriba, hacia las cámaras. Si la cámara lo ve "quemado" (blanco), bajá el brillo del LED o la exposición del teléfono.

### Tracking en vivo

Tocá **Tracking en vivo** (arriba a la derecha) o entrá a **6 · Tracking** e **Iniciar tracking**. Cómo funciona:

- **Por cámara:** detecta las manchas del color de cada auto, solo dentro de la pista si los bordes están dibujados, y descarta las zonas a ignorar. Para **empezar** a seguir un auto exige que la mancha se esté **moviendo**, así no confunde objetos quietos del mismo color. Una vez que lo sigue, alcanza con el color, aunque el auto frene.
- **Fusión:** pasa cada detección a mm con la calibración y combina ambas cámaras en un solo punto por auto (en la zona compartida no hay duplicados). Suaviza la posición y calcula la velocidad con un filtro de Kalman.
- **Carrera:** con la **línea central** cerrada calcula el avance de cada auto, cuenta vueltas al pasar por el **INICIO** en el sentido de la flecha y arma las posiciones 1°, 2°, 3°… **Reiniciar vueltas** pone todo en cero.
- **Video:** «Ver video con las detecciones» muestra lo que procesa cada cámara: en naranja lo que se mueve, el contorno de la pista y un círculo por cada auto detectado.

Si cambiás la pista o los vehículos con el tracking activo, se reinicia solo con la configuración nueva.

**🧪 Modo demo:** el botón **Modo demo** simula autos y una carrera, sin cámaras ni hardware y **sin necesitar OpenCV ni NumPy instalados**. Sirve para probar esta sección y la pantalla `/display` (ver sección 10) antes de tener el hardware armado, o para mostrar el sistema sin depender de la conexión de los teléfonos. Si ya dibujaste la línea central real en **4 · Pista**, el modo demo la usa (así también sirve para revisar que la pista dibujada se vea bien); si todavía no hay ninguna, usa una **pista ovalada de ejemplo** del tamaño de la plancha configurada, y lo avisa con un cartel. Cada auto simulado anda a una velocidad que varía solita (acelera y frena) para que la carrera se vea viva. Todo lo demás (vueltas, posiciones, velocidad) se calcula exactamente igual que con cámaras reales — no es una pantalla aparte, es el mismo motor de carrera. El modo demo y el seguimiento real **no corren a la vez**: empezar uno corta el otro automáticamente. Mientras el modo demo está activo, tanto esta sección como `/display` muestran un cartel **DEMO** bien visible para que no se confunda con una carrera real.

**Limitaciones actuales (importante):**
- **Calibración preliminar** (homografía + posición de cámara estimada): corrige la perspectiva y, con la **altura del LED** cargada, el corrimiento por altura. La posición de la cámara se estima con cierta incertidumbre, que es mayor si la cámara mira derecho a lo largo de la plancha. En la simulación, el error residual fue de 11–15 mm.
- No se corrige la **distorsión de la lente**.
- La detección depende de cómo el teléfono "ve" el color del LED: luz ambiente, exposición y balance de blancos.
- Con dos autos pegados del **mismo** color no se pueden distinguir: cada auto debe tener un color propio.
- Los números de precisión medidos hasta ahora son **de simulación**: pista en 8, 2 cámaras en ángulo a 1,4 m, LED a 55 mm, 8 puntos de referencia con 1 px de error de clic. El error medio fue de 11–15 mm y las vueltas se contaron bien. Todavía no se midió con la pista real.

---

## 6. Dónde se guardan los datos

| Archivo | Contenido |
|---|---|
| `config\cameras.json` | Cámaras: IP, puerto, ruta, usuario, contraseña y parámetros |
| `data\track\track_layout.json` | Pista: plano en mm y dibujos de cada cámara en px |
| `data\track\snapshots\<cam>.jpg` | Imagen de referencia de cada cámara |
| `config\vehicles.json` | Cantidad de autos, color de cada LED, tolerancias y opciones de detección |
| `data\backups\*.json` | Copias automáticas de los 3 archivos de arriba, hechas justo antes de cada guardado |
| `config\track_presets.json` | Plantillas de tamaño de plancha guardadas por el usuario (desde Pista → **Guardar como plantilla**), además de las predefinidas |
| `config\hardware.json` | Modo (simulación/hardware), puerto COM, velocidad y pinout informativo del ESP32 + duración de la cuenta regresiva del semáforo |

El UID de la tarjeta RFID de cada auto se guarda junto al resto de sus datos, en `config\vehicles.json` (campo `rfid_uid`).

La calibración (puntos de referencia y homografía de cada cámara) se guarda dentro de `track_layout.json`, en `calibration`.

- Se crean solos la primera vez. Se guardan de forma segura: si se corta la luz a mitad de un guardado, no quedan archivos rotos.
- Si un archivo se daña, la página guarda una copia `*.danado_<fecha>` y arranca con valores por defecto.
- **Respaldos (desde v0.5.0):** antes de cada «Guardar» de Cámaras, Pista o Vehículos se copia la versión anterior a `data\backups\` (hasta 25 copias por archivo). El botón **Respaldos**, en la barra superior, abre un historial con pestañas **Todo / Cámaras / Pista / Vehículos** para filtrar, cada copia con fecha y hora y un botón para restaurarla con un clic; restaurar también respalda la versión actual, así que nunca se pierde nada. Es la forma de volver atrás si un guardado dejó algo mal.
- **Plantillas propias (desde v0.6.0):** en Pista → Medidas de la plancha, además de las predefinidas se puede **Guardar como plantilla** el largo/ancho actual con un nombre propio (hasta 40 plantillas); aparecen agrupadas como "Mis plantillas" en el mismo selector y se pueden borrar con el botón **Borrar plantilla** al elegirlas.
- **La contraseña de las cámaras queda en texto plano** en `cameras.json`. La página nunca la muestra ni la envía al navegador.
- Si cambiás la resolución o el zoom de una cámara, los dibujos hechos sobre esa cámara pueden quedar desalineados: recapturá la imagen de referencia.

---

## 7. Problemas comunes

| Síntoma | Causa probable / solución |
|---|---|
| `"python" no se reconoce...` | Python no está en el PATH: usá `py` o reinstalá Python marcando "Add to PATH". |
| `Falta una dependencia: flask` | `python -m pip install flask` |
| `No module named admin` | Ejecutaste el comando fuera de la carpeta `MultiCam_Vision`: hacé `cd` a esa carpeta. |
| **Los cambios / secciones nuevas no aparecen** | 1) Cerrá **todas** las ventanas negras del servidor y abrí `INICIAR_ADMIN.bat` (desde la v0.3.0 cierra solo la anterior). 2) En el navegador apretá **Ctrl + F5**. 3) Fijate que arriba a la izquierda diga la versión nueva. Si sigue igual, abrí el Administrador de tareas y finalizá los procesos **python.exe**. |
| La página no carga nada, o todo da error de conexión, y la barra del navegador muestra **`https://`** | El servidor local no tiene certificado: tiene que ser `http://`, no `https://`. Desde la v0.5.0 la página se corrige sola si esto pasa; si usás una versión más vieja, escribí la dirección a mano empezando con `http://`. |
| Se perdió un cambio de cámaras, pista o vehículos | Desde la v0.5.0 cada «Guardar» respalda antes la versión anterior: abrí **Respaldos** (arriba a la derecha) y restaurala. Si estabas editando la pista y se cerró el navegador sin guardar, al volver a la pestaña **Pista** aparece un aviso para recuperar el borrador. |
| `Address already in use` / el puerto no abre | El 5050 está ocupado o ya hay otra página abierta: cerrala, o usá `--port 8090`. |
| *Probar conexión* → `Control FALLA · No se pudo conectar` | El teléfono no está en la misma red, la IP cambió, o el servidor de IP Webcam no está iniciado o el teléfono se bloqueó. Revisá la IP que muestra la app. |
| `HTTP 401` | IP Webcam tiene usuario y contraseña configurados: cargalos en la tarjeta de la cámara. |
| Video "Reintentando" | Se cortó el Wi‑Fi o el teléfono pasó a ahorro de energía. Se reconecta solo; mirá *Reconexiones* y *Último error*. |
| Un ajuste de **Configuración** da error | Ese teléfono o esa versión de la app puede no aceptar ese ajuste. Anotá el mensaje y avisá. |
| Desde el celular no abre `http://IP-DE-LA-PC:5050` | Iniciá con `--host 0.0.0.0` y permití Python en el Firewall de Windows para redes privadas. |
| Tracking: «El seguimiento necesita OpenCV y NumPy» | `python -m pip install opencv-contrib-python numpy` y reiniciá la página. |
| Tracking: «Cámara X sin calibrar» | Faltan 4 puntos de referencia en esa cámara (Pista → Vinculada → Puntos de referencia). |
| Tracking: un auto queda «buscando» | No se ve su color o no se movió todavía. Revisá **Ver detección** en Vehículos, que el LED esté encendido, que esté dentro de la pista dibujada, y movelo un poco. |
| Tracking: un auto salta a otro lugar | Hay otra cosa de su color en la imagen: dibujá una zona a ignorar ahí, o subí saturación/brillo mínimos. |
| «La cámara envía AxB pero se calibró con CxD» | Cambió la resolución del teléfono: recapturá la foto de referencia y volvé a marcar los puntos. |
| ESP32: «Hardware necesita pyserial» | `python -m pip install pyserial` (no hace falta en modo "Simulación"). |
| ESP32: «Iniciar carrera» deshabilitado en modo Hardware | El ESP32 no conectó por ese puerto COM: revisá el puerto/cable, que el firmware esté cargado, y que ningún otro programa (el monitor serial del IDE de Arduino, por ejemplo) tenga el puerto abierto. |
| ESP32: una tarjeta RFID aparece como «etiqueta desconocida» | Cargale el UID a ese auto en Vehículos → "UID de la etiqueta RFID" (lo mostró el lector al acercarla). |

---

## 8. Probar sin teléfonos

```
python -m pip install -r requirements.txt
python tests\fake_ipwebcam.py --port 8081
```

En la página, configurá una cámara con IP `127.0.0.1` y puerto `8081`.

**Escena simulada: pista en 8 con 2 cámaras en ángulo y autos con LED a 55 mm** (para probar calibración, vehículos y tracking):

```
python tests\fake_ipwebcam.py --escena --port 8081
```

Cámara 1 en `127.0.0.1:8081` y cámara 2 en `127.0.0.1:8082`. La consola muestra la posición en mm de las marcas de referencia (cuadrados blancos) para cargarlas como puntos de referencia. Los autos son rojo, verde y azul. Hay un objeto rojo quieto fuera de la pista que **no** debe detectarse.

Pruebas automáticas (70):

```
python -m pytest tests -v
```

---

## 9. ESP32 y semáforo de salida (sección 7)

Semáforo de salida (6 LEDs rojos sobre un 74HC595N) y 3 lectores RFID RC522, conectados por USB
Serial a un ESP32 (el firmware está en `firmware\esp32_race_control\`). **No reemplaza el
tracking por cámaras**: su única función es dar la salida — al llegar a cero, pone en cero las
vueltas y los tiempos del tracking (como si apretaras "Reiniciar vueltas" en la sección 6), nada
más. Si el tracking de la sección 6 no está corriendo, igual podés usar el semáforo: vas a ver
"¡Carrera en curso!" ahí, pero las vueltas no se cuentan hasta que inicies el tracking aparte.

### Dos modos

- **Simulación** (por defecto, sin hardware ni pyserial): las lecturas RFID de cada lector se
  generan a mano con el botón **Simular lectura** (podés escribir un UID o dejarlo vacío para que
  invente uno). Sirve para probar el semáforo y la cuenta regresiva sin tener la placa armada.
- **Hardware**: elegí el puerto COM y la velocidad (115200 por defecto, igual que el firmware) y
  guardá. Si el ESP32 no conecta, el panel lo muestra en rojo y **"Iniciar carrera" queda
  deshabilitado** — no pasa a simulación solo.

### Asignar la tarjeta RFID de cada auto

En **Vehículos**, cada auto tiene un campo **"UID de la etiqueta RFID"** (opcional). Acercá la
tarjeta a un lector (real o, en simulación, con "Simular lectura" escribiendo el UID real si ya
lo sabés) para ver el UID que lee, y cargalo ahí. Una tarjeta con un UID que no coincide con
ningún auto se muestra como **"etiqueta desconocida"**: nunca se asigna a un auto al azar.

### Semáforo

**Iniciar carrera** enciende los 6 LEDs y arranca una cuenta regresiva de 6 segundos (configurable
en "Modo y conexión" → ajustes del semáforo, en `config\hardware.json`): se apaga un LED por
segundo y, al llegar a cero, se considera arrancada la carrera. **Cancelar cuenta** apaga los LEDs
y no llega a arrancar. **Restablecer** vuelve el semáforo a reposo **sin tocar** las vueltas o
tiempos que ya se hayan corrido (para eso está "Reiniciar vueltas" en la sección 6).

### Firmware

`firmware\esp32_race_control\esp32_race_control.ino` + su propio `README.md` con el cableado
completo, la librería necesaria (MFRC522) y cómo verificarlo antes de conectarlo a la pista. **No
se probó todavía en una placa física**: antes de cargarlo, confirmá el pinout real de tu ESP32 (el
propuesto es para un DevKit/WROOM-32 típico).

---

## 10. Modo visualización (pantalla para el público)

Pantalla **de solo lectura**, sin ningún control de edición, pensada para abrir en un teléfono o
una tablet aparte y mostrarla al público mientras corre la carrera: posición, auto (nombre y
color), velocidad grande en km/h y vueltas (última y mejor). No es una sección más del panel de
administrador: es su propia página, en `/display`.

### Cómo abrirla

1. Desde la PC, en la página de administrador, hacé clic en **"Modo visualización ↗"** (arriba a
   la derecha) para abrirla en una pestaña nueva y confirmar que anda.
2. Desde el teléfono o la tablet (en la misma red Wi‑Fi que la PC), abrí
   `http://IP-DE-LA-PC:5050/display` — la misma IP que usás para la página de administrador (ver
   sección 4, "Cómo iniciar la página", para el modo `--host 0.0.0.0` y el permiso de Firewall).
3. Dejá esa pestaña abierta: se actualiza sola cada medio segundo, no hace falta recargarla ni
   tocar nada más. El teléfono/tablet puede quedar en un atril o pegado a la pared.

### Qué muestra

- Mientras el tracking **no está corriendo** (sección 6 del panel), muestra un aviso de espera en
  vez de una tabla vacía o desactualizada.
- Una vez corriendo: los autos ordenados por posición (1°, 2°…), con la velocidad **a escala real
  equivalente** en km/h bien grande (el mismo cálculo que ya se usaba en la sección Tracking,
  `speed_scale_kmh`: la velocidad del autito 1:24 llevada a lo que sería un auto de tamaño real).
  Un auto sin señal ("buscando") aparece atenuado al final, no desaparece.
- Los mismos avisos que ya muestra la sección Tracking (por ejemplo, "Cámara 1 sin calibrar")
  también aparecen acá, para que no parezca que algo se "colgó" si falta terminar el seteo.
- Si la sección Tracking tiene el **Modo demo** activo, acá arriba aparece un cartel **🧪 DEMO**
  bien visible, para que nadie confunda una carrera simulada con una real.

No duplica nada: usa la misma `/api/tracking/state` que ya alimenta la sección 6, así que
cualquier cosa que se vea mal ahí (una cámara sin calibrar, un color mal ajustado) se ve igual acá.

---

## 11. Otros documentos

- `docs\ARQUITECTURA_Y_VIABILIDAD.md`: arquitectura del sistema completo, viabilidad y riesgos.
- `docs\REGISTRO_CENTRAL.md`: decisiones, conflictos, tareas pendientes y avances.
- `docs\informes_agentes\` y `docs\experimentos\`: informes técnicos y experimentos de la fase 1.
- `firmware\esp32_race_control\`: firmware del ESP32 (semáforo + RFID) y su propio README.

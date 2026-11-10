# Firmware ESP32: semáforo de salida + 3 lectores RFID

Carga `esp32_race_control.ino` con el IDE de Arduino (o `arduino-cli`) en la placa ESP32.

## Antes de cargar

1. **Confirmá el pinout real de tu placa.** "ESP32 + CH340C" identifica el conversor
   USB-serial, no el modelo exacto del microcontrolador: revisá la serigrafía antes de
   cablear. Los pines de este firmware (`PIN_RST`, `READER_CS`, `PIN_SHIFT_*`) son una
   propuesta para un ESP32 clásico DevKit/WROOM-32; si tu placa es otra, ajustalos arriba
   del archivo antes de compilar.
2. **Cableado**: ver el comentario al principio de `esp32_race_control.ino` (coincide con
   `Instrucciones_para_Claude_MultiCamVision.md` y con `docs/REGISTRO_CENTRAL.md`).
   Los RC522 van a **3V3, nunca a 5V**.
3. **Librería**: instalar "MFRC522" (de miguelbalboa) desde el Library Manager del IDE de
   Arduino. No hace falta ninguna otra librería (el NDJSON se arma/parsea a mano).

## Selección de placa en el IDE

Board: "ESP32 Dev Module" (o la que corresponda a tu placa real). Velocidad del puerto:
115200 baudios, igual que `baudrate` en `config/hardware.json` del lado PC.

## Verificar antes de conectar a la pista

- Con un multímetro, confirmar que la línea 3V3 no cae por debajo de ~3,1 V con los 3 RC522
  y el 74HC595 conectados; si cae, usar una fuente 3.3V regulada aparte (GND común).
- Abrir el monitor serial del IDE a 115200 baudios: al encender debería aparecer
  `{"v":1,"type":"boot",...}` seguido de un `{"v":1,"type":"hb",...}` cada 1 segundo.
- Acercar una tarjeta/llavero a cada lector por separado y confirmar que el `lane` del
  mensaje `rfid` coincide con el lector físico usado (1, 2 o 3).
- Desde la página de administrador (sección "ESP32 y semáforo", modo `hardware`), probar
  "Iniciar carrera": las 6 líneas Q0..Q5 del 74HC595 deberían apagarse una por segundo.

## Protocolo

Ver `admin/esp32.py` (lado PC) y el docstring del propio `.ino`: una línea JSON por mensaje
(NDJSON v1) a 115200 baudios. Mensajes ESP32→PC: `boot`, `hb`, `rfid`, `pong`, `led_state`.
Mensajes PC→ESP32: `leds` (`{"type":"leds","mask":0..63}`) y `ping`.

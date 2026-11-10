/* ESP32 + 3× RC522 (SPI compartido) + 74HC595N (6 LEDs del semáforo) — MultiCam Vision
 * ============================================================================
 *
 * Protocolo: una línea JSON por mensaje (NDJSON v1) sobre USB Serial a 115200 baudios,
 * exactamente como describe `docs/REGISTRO_CENTRAL.md` / `admin/esp32.py` en el lado PC.
 *
 * IMPORTANTE (ver Instrucciones_para_Claude_MultiCamVision.md): "CH340C" identifica el
 * conversor USB-serial de la placa, NO el modelo exacto de microcontrolador. El pinout de
 * abajo es una PROPUESTA para un ESP32 clásico tipo DevKit/WROOM-32. Antes de cablear o
 * cargar este firmware, confirmá la serigrafía/pinout real de tu placa: no todos los
 * módulos "ESP32 + CH340C" tienen los mismos GPIO disponibles.
 *
 * Pinout propuesto
 * ----------------
 *   Los 3 RC522 comparten el mismo bus SPI (SCK/MISO/MOSI) y el mismo RST; cada uno tiene
 *   su propio SS/SDA (chip-select) para que el firmware seleccione un lector a la vez:
 *     SCK  -> GPIO18   MISO -> GPIO19   MOSI -> GPIO23   RST (compartido) -> GPIO33
 *     Lector 1 (lane 1): SS/SDA -> GPIO21
 *     Lector 2 (lane 2): SS/SDA -> GPIO22
 *     Lector 3 (lane 3): SS/SDA -> GPIO32
 *     IRQ de los 3 módulos: sin conectar.
 *   Cada RC522 a 3V3 y GND (NUNCA 5V). Compartir GND con el ESP32.
 *
 *   74HC595N (6 LEDs rojos, uno por segundo de cuenta regresiva):
 *     SER/DS -> GPIO25   SH_CP/SRCLK -> GPIO26   ST_CP/RCLK -> GPIO27
 *     OE (activo bajo) -> GND (siempre habilitado)   MR (activo bajo) -> 3V3 (sin reset)
 *     Q0..Q5 -> resistencia individual -> ánodo de cada LED (cátodo a GND).
 *     Capacitor cerámico de 100 nF entre VCC (pin 16) y GND (pin 8), cerca del CI.
 *
 * Librería necesaria: "MFRC522" de miguelbalboa (Arduino Library Manager).
 * No se usa ArduinoJson a propósito (protocolo fijo y simple): el parseo de los pocos
 * mensajes PC->ESP32 se hace a mano más abajo.
 *
 * Diseño no bloqueante: el loop() nunca usa delay(); toda temporización usa millis().
 */

#include <SPI.h>
#include <MFRC522.h>

// ------------------------------------------------------------------ pines (ver cabecera)
constexpr uint8_t PIN_RST = 33;                              // reset compartido de los 3 RC522
constexpr uint8_t READER_CS[3] = {21, 22, 32};                // SS/SDA de cada lector (lane 1,2,3)
constexpr uint8_t PIN_SHIFT_DATA = 25;                        // 74HC595 SER/DS
constexpr uint8_t PIN_SHIFT_CLOCK = 26;                       // 74HC595 SH_CP/SRCLK
constexpr uint8_t PIN_SHIFT_LATCH = 27;                       // 74HC595 ST_CP/RCLK

constexpr uint32_t BAUDRATE = 115200;
constexpr uint32_t HEARTBEAT_MS = 1000;                       // {"type":"hb",...} cada 1 s
constexpr uint32_t RFID_DEBOUNCE_MS = 1500;                   // antirrebote: misma tarjeta "puesta"
constexpr uint32_t RFID_PRESENCE_TIMEOUT_MS = 600;            // sin verla este tiempo -> "se retiró"

MFRC522 readers[3] = {
  MFRC522(READER_CS[0], PIN_RST),
  MFRC522(READER_CS[1], PIN_RST),
  MFRC522(READER_CS[2], PIN_RST),
};

uint32_t seqCounter = 0;
uint32_t lastHeartbeat = 0;
uint8_t currentMask = 0;

// Estado de "presencia" por lector, para el antirrebote: una tarjeta nueva solo genera un
// evento "rfid" si (a) no es la misma UID que la última reportada en ese lector dentro de
// RFID_DEBOUNCE_MS, o (b) pasó RFID_PRESENCE_TIMEOUT_MS sin verla (se asume que se retiró),
// lo que permite una lectura nueva de la MISMA tarjeta si se vuelve a presentar.
struct ReaderState {
  String lastUid;
  uint32_t lastSeenMs = 0;
  uint32_t lastReportedMs = 0;
  bool present = false;
};
ReaderState readerState[3];

// ------------------------------------------------------------------ utilidades NDJSON

String uidToHex(MFRC522::Uid *uid) {
  String s;
  s.reserve(uid->size * 2);
  for (byte i = 0; i < uid->size; i++) {
    if (uid->uidByte[i] < 0x10) s += '0';
    s += String(uid->uidByte[i], HEX);
  }
  s.toUpperCase();
  return s;
}

void sendBoot() {
  Serial.print("{\"v\":1,\"type\":\"boot\",\"seq\":");
  Serial.print(seqCounter++);
  Serial.print(",\"ms\":");
  Serial.print(millis());
  Serial.println(",\"fw\":\"1.1\"}");
}

void sendHeartbeat() {
  Serial.print("{\"v\":1,\"type\":\"hb\",\"seq\":");
  Serial.print(seqCounter++);
  Serial.print(",\"ms\":");
  Serial.print(millis());
  Serial.println(",\"readers\":[1,2,3]}");
}

void sendRfid(uint8_t lane, const String &uid) {
  Serial.print("{\"v\":1,\"type\":\"rfid\",\"seq\":");
  Serial.print(seqCounter++);
  Serial.print(",\"ms\":");
  Serial.print(millis());
  Serial.print(",\"lane\":");
  Serial.print(lane);
  Serial.print(",\"uid\":\"");
  Serial.print(uid);
  Serial.println("\"}");
}

void sendPong(long id) {
  Serial.print("{\"v\":1,\"type\":\"pong\",\"seq\":");
  Serial.print(seqCounter++);
  Serial.print(",\"ms\":");
  Serial.print(millis());
  Serial.print(",\"id\":");
  Serial.print(id);
  Serial.println("}");
}

void sendLedState(uint8_t mask) {
  Serial.print("{\"v\":1,\"type\":\"led_state\",\"mask\":");
  Serial.print(mask);
  Serial.print(",\"ms\":");
  Serial.print(millis());
  Serial.print(",\"seq\":");
  Serial.print(seqCounter++);
  Serial.println("}");
}

// ------------------------------------------------------------------ 74HC595: 6 LEDs
// bit0 del mask -> Q0 (LED 1) ... bit5 -> Q5 (LED 6). Con shiftOut(..., MSBFIRST, mask) el
// último bit enviado (bit0) termina en Q0 (el más cercano a la entrada DS): es el orden
// correcto para que mask=0b111111 (63) encienda las 6 y mask=0 las apague todas.
void writeLeds(uint8_t mask) {
  currentMask = mask & 0x3F;  // solo Q0..Q5 están cableados a LEDs
  digitalWrite(PIN_SHIFT_LATCH, LOW);
  shiftOut(PIN_SHIFT_DATA, PIN_SHIFT_CLOCK, MSBFIRST, currentMask);
  digitalWrite(PIN_SHIFT_LATCH, HIGH);
  sendLedState(currentMask);
}

// ------------------------------------------------------------------ parseo mínimo de línea PC->ESP32
// Protocolo fijo y chico (ver docstring de arriba): no hace falta un parser JSON completo.
// Busca el valor entero que sigue a una clave "clave": y lo devuelve, o -1 si no está.
long jsonIntField(const String &line, const char *key) {
  String needle = String("\"") + key + "\":";
  int idx = line.indexOf(needle);
  if (idx < 0) return -1;
  idx += needle.length();
  while (idx < (int)line.length() && (line[idx] == ' ')) idx++;
  bool neg = false;
  if (idx < (int)line.length() && line[idx] == '-') { neg = true; idx++; }
  long val = 0;
  bool any = false;
  while (idx < (int)line.length() && isDigit(line[idx])) {
    val = val * 10 + (line[idx] - '0');
    idx++;
    any = true;
  }
  if (!any) return -1;
  return neg ? -val : val;
}

bool jsonHasType(const String &line, const char *type) {
  String needle = String("\"type\":\"") + type + "\"";
  return line.indexOf(needle) >= 0;
}

void handleIncomingLine(String line) {
  line.trim();
  if (line.length() == 0) return;
  if (jsonHasType(line, "leds")) {
    long mask = jsonIntField(line, "mask");
    if (mask >= 0 && mask <= 255) writeLeds((uint8_t)mask);
  } else if (jsonHasType(line, "ping")) {
    long id = jsonIntField(line, "id");
    sendPong(id);
  }
  // cualquier otro "type" desconocido se ignora (compatibilidad hacia adelante)
}

// ------------------------------------------------------------------ RFID: un lector a la vez
// MFRC522::PCD_Init() deja el propio _chipSelectPin en HIGH al terminar, así que después de
// inicializar los 3 (cada uno con su propio SS) las 3 líneas CS quedan en reposo (HIGH) y cada
// llamada de la librería baja/sube SU PROPIO CS alrededor de la transferencia SPI: nunca hay
// dos lectores "activos" al mismo tiempo en el bus, aunque lo compartan.
void pollReader(uint8_t lane) {
  MFRC522 &rdr = readers[lane];
  ReaderState &st = readerState[lane];
  uint32_t now = millis();

  if (!rdr.PICC_IsNewCardPresent() || !rdr.PICC_ReadCardSerial()) {
    // No se ve ninguna tarjeta en esta pasada. Si hace rato que no la vemos, se considera
    // retirada: la próxima vez que aparezca (aunque sea la misma UID) se vuelve a reportar.
    if (st.present && (now - st.lastSeenMs) > RFID_PRESENCE_TIMEOUT_MS) {
      st.present = false;
    }
    return;
  }

  String uid = uidToHex(&rdr.uid);
  st.lastSeenMs = now;
  st.present = true;

  bool sameAsLastReport = (uid == st.lastUid) && (now - st.lastReportedMs) < RFID_DEBOUNCE_MS;
  if (!sameAsLastReport) {
    sendRfid(lane + 1, uid);  // lane es 0-based acá; el protocolo usa 1,2,3
    st.lastUid = uid;
    st.lastReportedMs = now;
  }

  rdr.PICC_HaltA();
  rdr.PCD_StopCrypto1();
}

// ------------------------------------------------------------------ setup / loop

void setup() {
  Serial.begin(BAUDRATE);
  delay(50);  // margen corto para que el monitor serial del lado PC termine de abrir el puerto

  pinMode(PIN_SHIFT_DATA, OUTPUT);
  pinMode(PIN_SHIFT_CLOCK, OUTPUT);
  pinMode(PIN_SHIFT_LATCH, OUTPUT);
  writeLeds(0);  // arranca con los 6 LEDs apagados

  SPI.begin();  // SCK/MISO/MOSI del bus compartido (ver pines arriba)
  for (uint8_t i = 0; i < 3; i++) {
    readers[i].PCD_Init();
  }

  sendBoot();
  lastHeartbeat = millis();
}

void loop() {
  // 1) comandos entrantes de la PC (no bloqueante: solo si ya hay una línea completa)
  while (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    handleIncomingLine(line);
  }

  // 2) los 3 lectores, uno por vuelta de loop (nunca los 3 "a la vez")
  for (uint8_t lane = 0; lane < 3; lane++) {
    pollReader(lane);
  }

  // 3) heartbeat periódico (para que la PC sepa que seguimos vivos aunque no haya lecturas)
  uint32_t now = millis();
  if (now - lastHeartbeat >= HEARTBEAT_MS) {
    sendHeartbeat();
    lastHeartbeat = now;
  }
}

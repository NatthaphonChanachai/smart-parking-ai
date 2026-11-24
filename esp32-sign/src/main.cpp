// Smart Parking AI - floor sign (ESP32 + LCD2004 I2C) - PlatformIO / Wokwi VS Code
// Receives "Floor 1,1,3;Floor 2,1,2;Floor 3,2,3;Floor 4,5,12"  (name,free,total;...  free=-1 -> offline)
//
// Features
//  - LCD frame buffer: only changed characters are written (no more garbled text like "OFee")
//  - custom icons (car, arrow, check, cross, P) + bar graph per floor
//  - auto-rotating pages: FLOORS / GUIDE (recommended floor + animated arrows + marquee)
//  - NeoPixel per floor: green = plenty, amber = almost full, red = full, purple = offline
//  - buzzer: chime when a spot frees up after being full, alert when the lot becomes full
//  - entry gate demo: press the button ("car arrives") -> LCD guides the driver, servo gate opens
//  - NTP clock (Thailand time)
#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <time.h>
#include <PubSubClient.h>
#include <LiquidCrystal_I2C.h>
#include <Adafruit_NeoPixel.h>

const char* WIFI_SSID = "Wokwi-GUEST";
const char* WIFI_PASS = "";
const char* MQTT_HOST = "broker.hivemq.com";   // demo only; real device -> local Mosquitto IP
const uint16_t MQTT_PORT = 1883;
const char* TOPIC = "cpe447-happywheel-7f3a/parking/summary";  // must match Node-RED

// ---------- pins ----------
const int PIN_PIXELS = 13;   // NeoPixel chain, one LED per floor
const int PIN_BUZZER = 25;
const int PIN_BUTTON = 4;    // "car arrives" at the entrance (to GND)
const int PIN_SERVO  = 18;   // entry gate
const int NUM_PIXELS = 4;

// ---------- custom LCD characters ----------
enum { CH_CAR = 0, CH_ARROW, CH_EMPTY, CH_CHECK, CH_CROSS, CH_P, CH_DOT };
const uint8_t CH_FULL = 0xFF;  // built-in solid block
byte gCar[8]   = {0b00000, 0b01110, 0b10001, 0b11111, 0b11111, 0b01010, 0b00000, 0b00000};
byte gArrow[8] = {0b00000, 0b01000, 0b01100, 0b01110, 0b01100, 0b01000, 0b00000, 0b00000};
byte gEmpty[8] = {0b11111, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b11111, 0b00000};
byte gCheck[8] = {0b00000, 0b00001, 0b00011, 0b10110, 0b11100, 0b01000, 0b00000, 0b00000};
byte gCross[8] = {0b00000, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001, 0b00000, 0b00000};
byte gP[8]     = {0b11111, 0b10001, 0b10101, 0b10001, 0b10111, 0b10111, 0b11111, 0b00000};
byte gDot[8]   = {0b00000, 0b00000, 0b01110, 0b01110, 0b01110, 0b00000, 0b00000, 0b00000};

LiquidCrystal_I2C* lcd = nullptr;
Adafruit_NeoPixel pixels(NUM_PIXELS, PIN_PIXELS, NEO_GRB + NEO_KHZ800);
WiFiClient net;
PubSubClient mqtt(net);

// ---------- parking data ----------
struct Floor { char name[12]; int free; int total; };
Floor floors[6];
int nFloors = 0;
unsigned long lastMsg = 0;
bool haveData = false;
int lastTotalFree = -1;

int totalFree()  { int s = 0; for (int i = 0; i < nFloors; i++) if (floors[i].free > 0) s += floors[i].free; return s; }
int totalSlots() { int s = 0; for (int i = 0; i < nFloors; i++) s += floors[i].total; return s; }
int onlineFloors() { int s = 0; for (int i = 0; i < nFloors; i++) if (floors[i].free >= 0) s++; return s; }
int bestFloor() {  // most free spots among online floors, -1 if none
  int b = -1;
  for (int i = 0; i < nFloors; i++)
    if (floors[i].free > 0 && (b < 0 || floors[i].free > floors[b].free)) b = i;
  return b;
}
bool stale() { return !haveData || millis() - lastMsg > 15000; }

// ---------- LCD frame buffer (write only what changed) ----------
uint8_t want[4][20], have[4][20];

void clearFrame() { memset(want, ' ', sizeof want); }
void put(int r, int c, const char* s) { for (; *s && c < 20; s++, c++) if (c >= 0) want[r][c] = (uint8_t)*s; }
void putc_(int r, int c, uint8_t ch) { if (c >= 0 && c < 20) want[r][c] = ch; }
void putCenter(int r, const char* s) { put(r, (20 - (int)strlen(s)) / 2, s); }

void printFrame() {  // mirror to serial terminal (readable icons)
  for (int r = 0; r < 4; r++) {
    String s = "LCD" + String(r) + "| ";
    for (int c = 0; c < 20; c++) {
      uint8_t ch = want[r][c];
      s += ch == CH_FULL ? '#' : ch == CH_EMPTY ? '.' : ch == CH_CAR ? 'C' : ch == CH_ARROW ? '>' :
           ch == CH_CHECK ? 'v' : ch == CH_CROSS ? 'x' : ch == CH_P ? 'P' : ch == CH_DOT ? 'o' : (char)ch;
    }
    Serial.println(s);
  }
}

void flush() {
  if (!lcd) return;
  for (int r = 0; r < 4; r++) {
    int c = 0;
    while (c < 20) {
      if (want[r][c] == have[r][c]) { c++; continue; }
      lcd->setCursor(c, r);                       // one cursor move per run of changed chars
      while (c < 20 && want[r][c] != have[r][c]) { lcd->write(want[r][c]); have[r][c] = want[r][c]; c++; }
    }
  }
}

// ---------- buzzer (non-blocking melodies) ----------
struct Note { uint16_t f, ms; };
const Note M_BOOT[]  = {{523, 90}, {659, 90}, {784, 90}, {1047, 160}};
const Note M_FREE[]  = {{880, 120}, {0, 40}, {1175, 180}};
const Note M_FULL[]  = {{392, 220}, {0, 60}, {262, 320}};
const Note M_GATE[]  = {{1047, 80}, {0, 40}, {1319, 80}, {0, 40}, {1568, 140}};
const Note M_DENY[]  = {{220, 180}, {0, 80}, {220, 180}};
const Note M_ALERT[] = {{1800, 60}, {0, 60}, {1800, 60}};
const Note* mel = nullptr; int melLen = 0, melIdx = 0; unsigned long melNext = 0;

void play(const Note* m, int n) { mel = m; melLen = n; melIdx = 0; melNext = 0; }
#define PLAY(m) play(m, sizeof(m) / sizeof(m[0]))
void buzzerLoop() {
  if (!mel || millis() < melNext) return;
  if (melIdx >= melLen) { ledcWriteTone(PIN_BUZZER, 0); mel = nullptr; return; }
  ledcWriteTone(PIN_BUZZER, mel[melIdx].f);
  melNext = millis() + mel[melIdx].ms;
  melIdx++;
}

// ---------- servo gate (LEDC 50 Hz, 14-bit) ----------
void servoDeg(int deg) {
  uint32_t us = map(deg, 0, 180, 500, 2400);
  ledcWrite(PIN_SERVO, us * 16384UL / 20000UL);
}

// ---------- pages ----------
enum Page { P_FLOORS, P_GUIDE };
Page page = P_FLOORS;
unsigned long pageSince = 0, gateUntil = 0, frameTick = 0;
bool gateGranted = false;
int gateFloor = -1;
uint32_t anim = 0;   // animation counter (~4 per second)

void shortName(const Floor& f, char* out) {  // "Floor 3" -> "F3"
  const char* d = f.name + strlen(f.name);
  while (d > f.name && isdigit((unsigned char)d[-1])) d--;
  snprintf(out, 4, "F%s", *d ? d : "?");
}

void drawFloorRow(int r, const Floor& f) {
  char sn[4]; shortName(f, sn);
  put(r, 0, sn);
  if (f.free < 0) {
    put(r, 3, (anim / 2) % 2 ? "-- OFFLINE --" : "   OFFLINE   ");
    return;
  }
  int n = f.total ? (f.free * 8 + f.total / 2) / f.total : 0;   // bar = share of FREE spots
  for (int i = 0; i < 8; i++) putc_(r, 3 + i, i < n ? CH_FULL : CH_EMPTY);
  char buf[8]; snprintf(buf, sizeof buf, "%2d/%-2d", f.free, f.total);
  put(r, 12, buf);
  if (f.free == 0) putc_(r, 18, (anim / 2) % 2 ? CH_CROSS : ' ');
  else putc_(r, 18, CH_CHECK);
}

void drawFloors() {
  int row = 0;
  if (nFloors < 4) {  // room for a header line
    putc_(0, 0, CH_P);
    char buf[21]; snprintf(buf, sizeof buf, "FREE %d/%d", totalFree(), totalSlots());
    put(0, 2, buf);
    struct tm t;
    if (getLocalTime(&t, 0)) { strftime(buf, sizeof buf, (anim / 2) % 2 ? "%H:%M" : "%H %M", &t); put(0, 15, buf); }
    row = 1;
  }
  for (int i = 0; i < nFloors && row < 4; i++, row++) drawFloorRow(row, floors[i]);
}

void drawGuide() {
  putc_(0, 1, CH_CAR); put(0, 3, "SMART PARKING"); putc_(0, 18, CH_P);
  char buf[24];
  snprintf(buf, sizeof buf, "Free spots: %d/%d", totalFree(), totalSlots());
  putCenter(1, buf);
  int b = bestFloor();
  if (b >= 0) {
    put(2, 0, "GO ");
    put(2, 3, floors[b].name);
    for (int i = 0; i < 4; i++) putc_(2, 15 + i, ((anim + 4 - i) % 4) == 0 ? CH_FULL : CH_ARROW);
  } else if (onlineFloors() == 0) {
    putCenter(2, "ALL CAMERAS OFFLINE");
  } else if ((anim / 2) % 2) {
    putCenter(2, "*** LOT  FULL ***");
  }
  static const char* MARQ = "Welcome to SPU parking  -  AI vision by CiRA CORE  -  please drive slowly  -  ";
  int L = strlen(MARQ);
  for (int c = 0; c < 20; c++) want[3][c] = MARQ[(anim + c) % L];
}

void drawGate() {
  if (gateGranted) {
    putc_(0, 2, CH_CAR); put(0, 5, "WELCOME !"); putc_(0, 17, CH_CAR);
    put(1, 0, "Please go to:");
    char buf[24]; snprintf(buf, sizeof buf, "%s (%d free)", floors[gateFloor].name, floors[gateFloor].free);
    putc_(2, 0, CH_ARROW); put(2, 2, buf);
    put(3, 0, "Gate OPEN ");
    for (int i = 0; i < 5; i++) putc_(3, 12 + i, i <= (int)(anim % 6) ? CH_DOT : ' ');
  } else {
    putCenter(0, "SORRY");
    putCenter(1, "PARKING LOT FULL");
    putc_(2, 2, CH_CROSS); put(2, 4, "Please wait"); putc_(2, 17, CH_CROSS);
    putCenter(3, "Gate CLOSED");
  }
}

void drawStale() {
  if ((anim / 2) % 2) putCenter(0, "!!  NO DATA  !!");
  putCenter(1, "Check MQTT link /");
  putCenter(2, "CiRA CORE camera");
  char buf[21];
  if (haveData) snprintf(buf, sizeof buf, "last data %lus ago", (millis() - lastMsg) / 1000);
  else snprintf(buf, sizeof buf, "waiting for data...");
  putCenter(3, buf);
}

void render() {
  clearFrame();
  if (millis() < gateUntil) drawGate();
  else if (stale()) drawStale();
  else if (page == P_FLOORS) drawFloors();
  else drawGuide();
  flush();
}

// ---------- NeoPixels ----------
void pixelsLoop() {
  float breath = 0.55f + 0.45f * sinf(millis() / 600.0f);
  for (int i = 0; i < NUM_PIXELS; i++) {
    uint32_t c = 0;
    if (stale()) c = ((millis() / 500) % 2) ? pixels.Color(0, 0, 60) : 0;
    else if (i < nFloors) {
      const Floor& f = floors[i];
      if (f.free < 0) c = pixels.Color(40 * breath, 0, 60 * breath);              // offline: purple breathing
      else if (f.free == 0) c = pixels.Color(255, 0, 0);                          // full
      else if (f.free * 3 < f.total) c = pixels.Color(255, 120, 0);               // almost full
      else c = pixels.Color(0, 220, 60);                                          // plenty
      if (millis() < gateUntil && gateGranted && i == gateFloor && (millis() / 150) % 2) c = pixels.Color(255, 255, 255);
    }
    pixels.setPixelColor(i, c);
  }
  pixels.show();
}

// ---------- MQTT ----------
void onMessage(char* topic, byte* p, unsigned int len) {
  String s;
  for (unsigned int i = 0; i < len; i++) s += (char)p[i];
  Serial.println("MQTT <- " + s);
  lastMsg = millis();
  haveData = true;
  nFloors = 0;
  int i = 0;
  while (nFloors < 6 && i < (int)s.length()) {
    int semi = s.indexOf(';', i);
    if (semi < 0) semi = s.length();
    String item = s.substring(i, semi);
    int c1 = item.indexOf(',');
    int c2 = item.indexOf(',', c1 + 1);
    if (c1 > 0 && c2 > c1) {
      Floor& f = floors[nFloors++];
      strncpy(f.name, item.substring(0, c1).c_str(), sizeof f.name - 1);
      f.name[sizeof f.name - 1] = 0;
      f.free = item.substring(c1 + 1, c2).toInt();
      f.total = item.substring(c2 + 1).toInt();
    }
    i = semi + 1;
  }
  int tf = onlineFloors() ? totalFree() : -1;
  if (lastTotalFree == 0 && tf > 0) PLAY(M_FREE);         // a spot opened up after being full
  if (lastTotalFree > 0 && tf == 0) PLAY(M_FULL);         // lot just became full
  lastTotalFree = tf;
  render();
  printFrame();
}

void bootScreen(const char* l1, const char* l2) {
  clearFrame();
  putc_(0, 0, CH_P); put(0, 2, "SMART PARKING AI");
  putCenter(1, "Happy Wheel CPE447");
  putCenter(2, l1);
  putCenter(3, l2);
  flush();
  Serial.printf("[boot] %s %s\n", l1, l2);
}

void connectWifi() {
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  int dots = 0;
  while (WiFi.status() != WL_CONNECTED) {
    char b[21]; snprintf(b, sizeof b, "WiFi %.*s", dots % 4, "...");
    bootScreen(b, "");
    delay(300); dots++;
  }
  Serial.println("WiFi: connected " + WiFi.localIP().toString());
  configTime(7 * 3600, 0, "pool.ntp.org", "time.google.com");   // Thailand UTC+7
}

void connectMqtt() {
  while (!mqtt.connected()) {
    bootScreen("Connecting MQTT...", MQTT_HOST);
    String id = "esp32-sign-" + String((uint32_t)esp_random(), HEX);
    if (mqtt.connect(id.c_str())) {
      mqtt.subscribe(TOPIC);
      Serial.println("MQTT: connected, subscribed");
    } else {
      Serial.println("MQTT: failed rc=" + String(mqtt.state()) + ", retry");
      delay(1500);
    }
  }
}

// ---------- entry gate button ----------
void buttonLoop() {
  static bool last = HIGH;
  static unsigned long lastChange = 0;
  bool now = digitalRead(PIN_BUTTON);
  if (now != last && millis() - lastChange > 40) {
    lastChange = millis();
    last = now;
    if (now == LOW && millis() >= gateUntil) {
      gateFloor = stale() ? -1 : bestFloor();
      gateGranted = gateFloor >= 0;
      gateUntil = millis() + 5000;
      if (gateGranted) { servoDeg(90); PLAY(M_GATE); }
      else PLAY(M_DENY);
      Serial.printf("GATE: car arrived -> %s\n", gateGranted ? floors[gateFloor].name : "DENIED (full)");
      render();
      printFrame();
    }
  }
}

// ---------- setup / loop ----------
byte findLcd() {
  for (byte a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { Serial.printf("I2C found: 0x%02X\n", a); return a; }
  }
  return 0;
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("Smart Parking sign starting");
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  ledcAttach(PIN_BUZZER, 2000, 10);
  ledcAttach(PIN_SERVO, 50, 14);
  servoDeg(0);
  pixels.begin();
  pixels.setBrightness(80);
  pixels.clear(); pixels.show();

  Wire.begin(21, 22);  // SDA=21, SCL=22
  Wire.setClock(100000);
  byte addr = findLcd();
  if (addr) {
    lcd = new LiquidCrystal_I2C(addr, 20, 4);
    lcd->init();
    lcd->backlight();
    lcd->createChar(CH_CAR, gCar);   lcd->createChar(CH_ARROW, gArrow);
    lcd->createChar(CH_EMPTY, gEmpty); lcd->createChar(CH_CHECK, gCheck);
    lcd->createChar(CH_CROSS, gCross); lcd->createChar(CH_P, gP);
    lcd->createChar(CH_DOT, gDot);
    lcd->clear();
    memset(have, ' ', sizeof have);
    Serial.printf("LCD ready at 0x%02X\n", addr);
  } else {
    Serial.println("LCD NOT FOUND - check SDA(21) / SCL(22) / VCC / GND wires");
  }

  // boot animation: car drives across
  PLAY(M_BOOT);
  for (int x = -1; x < 21; x++) {
    clearFrame();
    putc_(0, 0, CH_P); put(0, 2, "SMART PARKING AI");
    putCenter(1, "Happy Wheel CPE447");
    for (int c = 0; c < 20; c++) want[3][c] = '_';
    putc_(2, x, CH_CAR);
    flush();
    buzzerLoop();
    pixels.setPixelColor(((x + 1) / 2) % NUM_PIXELS, pixels.Color(0, 120, 255));
    pixels.show();
    delay(70);
    pixels.clear();
  }
  connectWifi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  connectMqtt();
  pageSince = millis();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWifi();
  if (!mqtt.connected()) connectMqtt();
  mqtt.loop();
  buttonLoop();
  buzzerLoop();

  if (gateUntil && millis() >= gateUntil) { gateUntil = 0; servoDeg(0); }
  if (millis() - pageSince > (page == P_FLOORS ? 7000UL : 5000UL)) {
    page = page == P_FLOORS ? P_GUIDE : P_FLOORS;
    pageSince = millis();
  }
  static bool wasStale = false;
  if (stale() != wasStale) { wasStale = stale(); if (wasStale && haveData) PLAY(M_ALERT); }

  if (millis() - frameTick > 250) {   // ~4 fps animation; only changed characters go over I2C
    frameTick = millis();
    anim++;
    render();
    pixelsLoop();
  }
}

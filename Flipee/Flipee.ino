/*
  Flipee — a small ESP32-S3 device with some personality and freedom to
  roam, for the Waveshare ESP32-S3-AMOLED-1.91 (RM67162, 240x536 portrait,
  QMI8658 IMU). LiPo-powered through an SPST switch, so it's meant to be
  picked up and carried around.

  WHAT THIS DOES
  --------------
  On boot, Flipee scans for WiFi and FORAGES for the first open (no
  password) network it can find nearby, rather than being told where to
  connect — that's the point of it. If you've set HOME_SSID below, it'll
  prefer that when visible (handy for testing at your desk, and it's what
  unlocks the optional Claude relay for richer reflections — see below).

  Once connected to anything, it:
    1. Asks a free IP-geolocation service roughly where it is (no API key
       needed — this works over the open internet, not just your home LAN,
       since Flipee has no idea what network it'll land on).
    2. Uses that city to pull a handful of local news headlines from a
       public Google News RSS feed for that place.
    3. Shows the result on screen, themed after the amber flip-dot look
       from flipdot-sim (near-black panel, amber dots/text).

  INPUT IS PHYSICAL, NOT A KEYBOARD (same idea as AgentWatch):
    - Tilt the board away from you / toward you -> page through headlines
    - A firm shake                              -> refresh now (re-scan
                                                    wifi if disconnected,
                                                    else re-fetch news)
    - A fast, deliberate spin/flip of the device -> write a journal entry
      to the microSD card: what Flipee saw, where it thinks it is, and a
      short reflection (Claude-written if the optional relay answers,
      otherwise a templated one so this still works out in the wild)

  Every successful WiFi connection, location fix, and news fetch also gets
  a one-line timestamped note in a running daily log on the SD card —
  Flipee keeps its own diary whether or not you ever shake it.

  WHY THIS ARCHITECTURE: an LLM can't run on this chip, so the device does
  everything that only needs the open internet (geolocation, news,
  display, sensors, SD) entirely on its own, and only *opportunistically*
  reaches for a relay for the one thing that genuinely needs an LLM:
  turning the day's headlines into a written reflection. If the relay
  doesn't answer within RELAY_TIMEOUT_MS — unreachable, wrong network,
  whatever — Flipee falls back to a templated reflection and keeps going.
  Point SERVER_HOST at a small always-on relay (a VPS works well — see
  server/flipee_relay.py) rather than your own PC and this works from any
  network Flipee forages onto, not just one you control; do that and set
  RELAY_KEY to match the relay's configured secret, since an
  internet-reachable /reflect with no auth is an open invitation to burn
  your Anthropic credits.

  Pins for the display and IMU below are the same verified-working values
  used by AgentWatch/FluidTilt on this exact board. The microSD and
  battery-ADC pins are NOT verified against this specific board (only the
  display+IMU wiring was confirmed by the reference sketch this was built
  from) — fill those in from your own board before enabling those
  features. Everything else works fine without them.

  Required Arduino IDE settings: arduino-esp32 core v3.3.0+, USB CDC On
  Boot: Enabled, PSRAM: OPI PSRAM.

  Required libraries:
  - "GFX Library for Arduino" by moononournation
  - QMI8658 by Lahav Gahali
  - ArduinoJson by Benoit Blanchon (v7.x)
  - WiFi.h / WiFiClientSecure.h / HTTPClient.h — bundled with the esp32 core
  - SD.h / SPI.h — bundled with the esp32 core (only used if ENABLE_SD and
    the SD_* pins below are filled in)
*/

#include <Arduino_GFX_Library.h>
#include <QMI8658.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <SD.h>
#include <time.h>
#include <math.h>
#include <vector>
#include <esp_system.h>

// Local credentials (WiFi + relay), kept out of the repo. Copy
// secrets.example.h to secrets.h and fill it in. __has_include lets a
// fresh clone fail with that instruction instead of a bare "no such file".
#if defined(__has_include) && !__has_include("secrets.h")
#error "Missing Flipee/secrets.h - copy Flipee/secrets.example.h to Flipee/secrets.h and fill in your values."
#endif
#include "secrets.h"

// arduino-esp32's loop() runs in a FreeRTOS task with an 8KB stack by
// default. WiFiClientSecure (TLS) + HTTPClient + ArduinoJson stacked
// together during the news fetch is a well-known way to blow through
// that on this core — the failure mode looks exactly like "connects,
// then silently reboots back to foraging" rather than a clean error,
// since a stack overflow panics the whole task rather than returning a
// bad HTTP code. This weak-symbol override (recognized by the core's
// startup code) raises it defensively; harmless if it wasn't the cause.
// Must go through the core's own macro: Arduino.h declares this with C++
// linkage, so an `extern "C"` definition here is a hard compile error
// ("conflicting declaration ... with 'C' linkage") rather than an override.
SET_LOOP_TASK_STACK_SIZE(16 * 1024);

// ---------------------------------------------------------------------
// Pins — display + IMU are confirmed working (same as AgentWatch/
// FluidTilt-1.91 on this board). SD pins are NOT verified for this board;
// fill them in from your own schematic/silkscreen before setting
// ENABLE_SD — left at -1 they're simply skipped.
// ---------------------------------------------------------------------
#define LCD_SDIO0 18
#define LCD_SDIO1 7
#define LCD_SDIO2 48
#define LCD_SDIO3 5
#define LCD_SCLK  47
#define LCD_CS    6
#define LCD_RST   17
#define IIC_SDA   40
#define IIC_SCL   39
// Panel is native 240x536 portrait; DISPLAY_ROTATION below turns it landscape,
// so these reflect the rotated frame everything else in this sketch draws into.
static const int LCD_WIDTH  = 536;
static const int LCD_HEIGHT = 240;
// 1 = 90 deg clockwise by Adafruit_GFX/Arduino_GFX convention. AgentWatch only
// ever ran this board at the native rotation (0), so this exact direction
// wasn't verified in hardware — if it comes out mirrored/counter-clockwise on
// your unit, change this to 3 (the other 90 deg option) rather than 1.
static const uint8_t DISPLAY_ROTATION = 1;

// =======================================================================
// CUSTOMIZATION — everything you're likely to want to change lives here
// =======================================================================

// --- WiFi foraging ---
// Leave HOME_SSID blank for pure "find any open network" behavior. If set
// and visible in a scan, Flipee prefers it over foraging — mainly useful
// for testing at your desk. Actual values live in secrets.h (gitignored);
// see secrets.example.h.
const char    *HOME_SSID          = HOME_SSID_VAL;
const char    *HOME_PASSWORD      = HOME_PASSWORD_VAL;
const uint32_t WIFI_CONNECT_TIMEOUT_MS = 12000;
const uint32_t WIFI_RETRY_MS           = 20000; // how often to rescan after a failed forage

// --- Optional Claude relay ---
// See server/flipee_relay.py. Purely optional — Flipee writes a templated
// reflection on its own if this never answers. Point this at a small
// always-on host (a VPS, not your own PC) and it works from any network
// Flipee forages onto. RELAY_KEY must match FLIPEE_RELAY_KEY on the relay
// — required once this is reachable from the open internet, since an
// unauthenticated /reflect endpoint is an invitation to burn your
// Anthropic credits. RELAY_USE_TLS should stay true for anything but
// same-LAN testing against a plain `python flipee_relay.py` with no
// reverse proxy in front of it.
//
// SERVER_HOST and RELAY_KEY live in secrets.h (gitignored) — a public repo
// that pairs a relay's address with its shared secret hands over whatever
// that secret was protecting. A wrong/unreachable host just times out after
// RELAY_TIMEOUT_MS and falls back to a templated reflection, by design, so
// check secrets.h first if relay reflections quietly stop arriving.
const char* SERVER_HOST = SERVER_HOST_VAL;
const int SERVER_PORT = 5024;
const bool RELAY_USE_TLS = false;  // Local LAN, no TLS
const char* RELAY_KEY = RELAY_KEY_VAL;
const uint32_t RELAY_TIMEOUT_MS = 2500;

// --- microSD journal (NOT verified against this board — confirm first) ---
const bool ENABLE_SD = true;
const int  SD_SCK  = -1; // TODO
const int  SD_MISO = -1; // TODO
const int  SD_MOSI = -1; // TODO
const int  SD_CS   = -1; // TODO

// --- Battery ADC (optional — most builds won't have a wired divider) ---
const bool  HAS_BATTERY_ADC        = false; // set true once ADC_PIN + ratio are confirmed
const int   BATTERY_ADC_PIN        = -1;    // TODO
const float BATTERY_DIVIDER_RATIO  = 2.0f;  // Vbat = Vadc * ratio
const float BATTERY_EMPTY_V        = 3.3f;
const float BATTERY_FULL_V         = 4.2f;

// --- Clock ---
const bool CLOCK_24H = false;

// --- Behavior / feel ---
const uint32_t NEWS_REFRESH_MS       = 30UL * 60UL * 1000UL; // re-fetch headlines every 30 min
const uint32_t IDLE_SLEEP_MS         = 3UL * 60UL * 1000UL;  // dim to a quiet clock after 3 min untouched
const uint32_t MESSAGE_TIMEOUT_MS    = 60000;  // auto-return to idle after this long unread
const float    SHAKE_THRESHOLD_G     = 2.2f;   // total accel magnitude (g) that counts as a shake
const uint32_t SHAKE_COOLDOWN_MS     = 1200;
const float    PAGE_TILT_THRESHOLD   = 0.45f;  // pitch (sin of angle) that triggers a page turn
const uint32_t PAGE_TURN_COOLDOWN_MS = 500;
const float    FLIP_GYRO_THRESHOLD   = 3.0f;   // rad/s — tune once you have the board in hand
const uint32_t FLIP_SUSTAIN_MS       = 220;    // how long the spin must hold above threshold
const uint32_t FLIP_COOLDOWN_MS      = 3000;
const uint8_t  TEXT_SIZE             = 2;      // GFX text scale for body copy
const uint8_t  MARGIN                = 12;     // px margin around text regions
const int      MAX_HEADLINES         = 8;

// --- Theme — amber flip-dot look (from flipdot-sim), or pick an AgentWatch one ---
struct Theme { uint16_t bg, fg, accent, dim; };
const Theme THEME_FLIPDOT  = {0x0841, 0xF606, 0xF606, 0x5ACB}; // near-black / amber / amber / gray
const Theme THEME_MIDNIGHT = {0x0000, 0xFFFF, 0x07FF, 0x4208}; // black / white / cyan / gray
const Theme THEME_PAPER    = {0xFFFF, 0x0000, 0xF800, 0xC618}; // white / black / red / light gray
const Theme theme = THEME_FLIPDOT; // <-- pick your theme here

// =======================================================================

Arduino_DataBus *bus = new Arduino_ESP32QSPI(
    LCD_CS, LCD_SCLK, LCD_SDIO0, LCD_SDIO1, LCD_SDIO2, LCD_SDIO3);
Arduino_GFX *gfx = new Arduino_RM67162(bus, LCD_RST, 0);
QMI8658 imu;
SPIClass sdSPI(FSPI);

// ---------------------------------------------------------------------
// State
// ---------------------------------------------------------------------
enum WifiState { WIFI_UNCONNECTED, WIFI_CONNECTED_STATE, WIFI_FORAGE_FAILED };
WifiState wifiState = WIFI_UNCONNECTED;
bool wifiIsHome = false;
uint32_t lastScanAttemptMs = 0;

// Why the last WiFi attempt didn't stick, in a few words — surfaced on the
// idle screen (and Serial) so a failed forage isn't just a silent retry
// loop. Declared here rather than next to setup() because onWifiConnected()
// below reads bootResetReason, and .ino auto-prototyping hoists functions
// but never globals.
String bootResetReason = "";
String lastWifiFailReason = "";
volatile uint8_t lastDisconnectReason = 0; // raw esp_wifi reason code, set from the WiFi event task

bool sdReady = false;
bool locationKnown = false;
String city = "", region = "", country = "", publicIp = "";
float latitude = 0, longitude = 0;
long gmtOffsetSec = 0;

// Tracks the post-connection pipeline (locate -> fetch news) so the idle
// screen can say what's actually happening instead of just "connected".
enum NewsStage { NEWS_STAGE_IDLE, NEWS_STAGE_LOCATING, NEWS_STAGE_LOCATION_FAILED,
                  NEWS_STAGE_FETCHING, NEWS_STAGE_NEWS_FAILED, NEWS_STAGE_READY };
NewsStage newsStage = NEWS_STAGE_IDLE;
int lastLocationHttpCode = 0;
int lastNewsHttpCode = 0;

std::vector<String> headlines;
uint32_t lastNewsFetchMs = 0;

enum ViewState { VIEW_IDLE, VIEW_NEWS, VIEW_REFLECT, VIEW_SLEEP };
ViewState currentView = VIEW_IDLE;

std::vector<String> pageLines[24];
int pageCount = 0;
int currentPage = 0;

int lastDrawnMinute = -1;
uint32_t lastInteractionMs = 0;
uint32_t lastShakeMs = 0;
uint32_t lastPageTurnMs = 0;
uint32_t lastFlipMs = 0;
uint32_t reflectShownAtMs = 0;
String lastReflectPath = "";

bool rotateSustaining = false;
uint32_t rotateStartMs = 0;

float angleX = 0.0f;
uint32_t lastImuMicros = 0;
const float COMP_ALPHA = 0.98f;

// ---------------------------------------------------------------------
// IMU: fused pitch (for paging), accel magnitude (for shake), gyro
// magnitude (for the flip/spin gesture)
// ---------------------------------------------------------------------
void readImu(float &pitchSin, float &accelMagG, float &gyroMagRadS) {
  uint32_t now = micros();
  float dt = (lastImuMicros == 0) ? 0.01f : (now - lastImuMicros) / 1000000.0f;
  lastImuMicros = now;

  QMI8658_Data d;
  accelMagG = 1.0f;
  gyroMagRadS = 0.0f;
  if (imu.readSensorData(d)) {
    float accAngleX = atan2f(d.accelY, sqrtf(d.accelX * d.accelX + d.accelZ * d.accelZ));
    angleX = COMP_ALPHA * (angleX + d.gyroX * dt) + (1.0f - COMP_ALPHA) * accAngleX;
    float accMag = sqrtf(d.accelX * d.accelX + d.accelY * d.accelY + d.accelZ * d.accelZ);
    accelMagG = accMag / 9.81f;
    gyroMagRadS = sqrtf(d.gyroX * d.gyroX + d.gyroY * d.gyroY + d.gyroZ * d.gyroZ);
  }
  pitchSin = sinf(angleX);
}

bool detectFlip(float gyroMagRadS, uint32_t now) {
  if (gyroMagRadS > FLIP_GYRO_THRESHOLD) {
    if (!rotateSustaining) {
      rotateSustaining = true;
      rotateStartMs = now;
    } else if (now - rotateStartMs > FLIP_SUSTAIN_MS && now - lastFlipMs > FLIP_COOLDOWN_MS) {
      lastFlipMs = now;
      rotateSustaining = false;
      return true;
    }
  } else {
    rotateSustaining = false;
  }
  return false;
}

// ---------------------------------------------------------------------
// Battery (best-effort — only meaningful if HAS_BATTERY_ADC is true and
// BATTERY_DIVIDER_RATIO matches your actual divider)
// ---------------------------------------------------------------------
int batteryPercent() {
  if (!HAS_BATTERY_ADC) return -1;
  int raw = analogRead(BATTERY_ADC_PIN);
  float vAdc = (raw / 4095.0f) * 3.3f;
  float vBat = vAdc * BATTERY_DIVIDER_RATIO;
  float pct = (vBat - BATTERY_EMPTY_V) / (BATTERY_FULL_V - BATTERY_EMPTY_V) * 100.0f;
  if (pct < 0) pct = 0;
  if (pct > 100) pct = 100;
  return (int)roundf(pct);
}

// ---------------------------------------------------------------------
// SD journal
// ---------------------------------------------------------------------
void ensureDir(const char *path) {
  if (!SD.exists(path)) SD.mkdir(path);
}

bool dateStamp(char *out, size_t outLen, bool withTime) {
  struct tm t;
  if (!getLocalTime(&t, 1500)) return false; // generous timeout: only called on discrete events, not per-frame
  if (withTime) {
    snprintf(out, outLen, "%04d-%02d-%02d_%02d%02d%02d",
              t.tm_year + 1900, t.tm_mon + 1, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec);
  } else {
    snprintf(out, outLen, "%04d-%02d-%02d", t.tm_year + 1900, t.tm_mon + 1, t.tm_mday);
  }
  return true;
}

void logObservation(const String &line) {
  if (!sdReady) return;
  ensureDir("/flipee");
  char day[16];
  if (!dateStamp(day, sizeof(day), false)) return;
  String path = "/flipee/" + String(day) + ".md";
  bool isNew = !SD.exists(path);
  File f = SD.open(path, FILE_APPEND);
  if (!f) return;
  if (isNew) {
    f.printf("# Flipee log — %s\n\n", day);
  }
  struct tm t;
  getLocalTime(&t, 200);
  f.printf("- %02d:%02d %s\n", t.tm_hour, t.tm_min, line.c_str());
  f.close();
}

const char *REFLECTION_TEMPLATES[] = {
  "I found \"%s\" out here. Strange to think how far a signal travels before it reaches a little screen like mine.",
  "Foraged onto an open network and caught \"%s\" drifting by. I wonder who else is reading the same thing right now.",
  "\"%s\" — noted. I don't have strong opinions yet, just headlines and a battery that's %s.",
  "Another patch of free wifi, another headline: \"%s\". I like not knowing where I'll wake up next.",
  "\"%s\" is what the place nearest me is talking about today. I'm just passing through.",
};
const int REFLECTION_TEMPLATE_COUNT = sizeof(REFLECTION_TEMPLATES) / sizeof(REFLECTION_TEMPLATES[0]);

String batteryStatusText() {
  int pct = batteryPercent();
  if (pct < 0) return "unmeasured (no ADC divider configured)";
  return String(pct) + "%";
}

String templatedReflection() {
  String headline = headlines.empty() ? "nothing much yet" : headlines[millis() % headlines.size()];
  const char *tmpl = REFLECTION_TEMPLATES[millis() % REFLECTION_TEMPLATE_COUNT];
  char buf[256];
  // every template has one %s for the headline; template #2 (index 2) has a second %s for battery
  if (String(tmpl).indexOf("%s") != String(tmpl).lastIndexOf("%s")) {
    snprintf(buf, sizeof(buf), tmpl, headline.c_str(), batteryStatusText().c_str());
  } else {
    snprintf(buf, sizeof(buf), tmpl, headline.c_str());
  }
  return String(buf);
}

bool tryRelayReflection(String &outText) {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  String url = String(RELAY_USE_TLS ? "https://" : "http://") + SERVER_HOST + ":" + String(SERVER_PORT) + "/reflect";

  WiFiClientSecure secureClient;
  bool began;
  if (RELAY_USE_TLS) {
    secureClient.setInsecure(); // trade cert pinning for simplicity; payload is headlines/city/battery, nothing sensitive
    began = http.begin(secureClient, url);
  } else {
    began = http.begin(url);
  }
  if (!began) return false;

  http.addHeader("Content-Type", "application/json");
  if (strlen(RELAY_KEY) > 0) http.addHeader("X-Flipee-Key", RELAY_KEY);
  http.setTimeout(RELAY_TIMEOUT_MS);
  http.setConnectTimeout(RELAY_TIMEOUT_MS);

  JsonDocument doc;
  doc["city"] = city;
  doc["region"] = region;
  doc["country"] = country;
  doc["ssid"] = WiFi.SSID();
  doc["battery_pct"] = batteryPercent();
  doc["uptime_s"] = (uint32_t)(millis() / 1000);
  JsonArray arr = doc["headlines"].to<JsonArray>();
  for (auto &h : headlines) arr.add(h);
  String body;
  serializeJson(doc, body);

  // The fallback to a templated reflection is deliberately silent on the
  // device, which also means a genuinely broken relay (wrong host, blocked
  // port, bad key) is indistinguishable from "no relay configured" unless
  // it says so here. Negative codes are HTTPClient's own errors (-1 is the
  // connect refused/timed-out case a firewall drop produces).
  Serial.println("[relay] POST " + url);
  int code = http.POST(body);
  bool ok = false;
  if (code == 200) {
    JsonDocument resp;
    if (!deserializeJson(resp, http.getString())) {
      outText = String((const char *)(resp["text"] | ""));
      ok = outText.length() > 0;
    }
  }
  if (ok) {
    Serial.printf("[relay] ok, %u chars of Claude\n", (unsigned)outText.length());
  } else {
    Serial.printf("[relay] failed (http %d: %s) - using a templated reflection\n",
                  code, HTTPClient::errorToString(code).c_str());
  }
  http.end();
  return ok;
}

void writeReflection() {
  currentView = VIEW_REFLECT;
  drawReflecting();

  String reflectionText;
  bool fromRelay = tryRelayReflection(reflectionText);
  if (!fromRelay) reflectionText = templatedReflection();

  if (sdReady) {
    ensureDir("/flipee");
    ensureDir("/flipee/reflections");
    char stamp[24];
    if (dateStamp(stamp, sizeof(stamp), true)) {
      String path = "/flipee/reflections/" + String(stamp) + ".md";
      File f = SD.open(path, FILE_WRITE);
      if (f) {
        f.printf("# Flipee reflection — %s\n\n", stamp);
        String loc = city.length() ? (city + ", " + region + ", " + country) : "unknown (no location fix yet)";
        f.printf("Location (via IP, approximate): %s\n", loc.c_str());
        f.printf("Connected via: %s%s\n", WiFi.SSID().c_str(), wifiIsHome ? " (home base)" : " (foraged, open network)");
        f.printf("Battery: %s\n", batteryStatusText().c_str());
        f.printf("Uptime: %lu min\n\n", (unsigned long)(millis() / 60000));
        f.printf("## Headlines seen\n");
        if (headlines.empty()) {
          f.printf("(none fetched yet)\n");
        } else {
          for (auto &h : headlines) f.printf("- %s\n", h.c_str());
        }
        f.printf("\n## Reflection%s\n\n%s\n", fromRelay ? " (Claude, via relay)" : " (on-device)", reflectionText.c_str());
        f.close();
        lastReflectPath = path;
        logObservation("wrote a reflection -> " + path);
      }
    }
  }
  lastInteractionMs = millis();
  reflectShownAtMs = millis();
  drawReflectDone(); // the relay call + SD write above already took real time, so show the
                      // outcome now rather than trying to time a fake "in progress" window
}

// ---------------------------------------------------------------------
// Networking: geolocation + news
// ---------------------------------------------------------------------
bool fetchLocation() {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  http.begin("http://ip-api.com/json/?fields=status,message,country,regionName,city,lat,lon,timezone,offset,query");
  http.setTimeout(6000);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  int code = http.GET();
  lastLocationHttpCode = code;
  bool ok = false;
  String failReason;
  if (code == 200) {
    JsonDocument doc;
    if (!deserializeJson(doc, http.getString())) {
      String apiStatus = String((const char *)(doc["status"] | ""));
      if (apiStatus == "success") {
        city = String((const char *)(doc["city"] | ""));
        region = String((const char *)(doc["regionName"] | ""));
        country = String((const char *)(doc["country"] | ""));
        latitude = doc["lat"] | 0.0;
        longitude = doc["lon"] | 0.0;
        gmtOffsetSec = doc["offset"] | 0;
        publicIp = String((const char *)(doc["query"] | ""));
        ok = true;
      } else {
        failReason = "api said: " + String((const char *)(doc["message"] | "unknown"));
      }
    } else {
      failReason = "bad json in response";
    }
  } else {
    failReason = "http " + String(code);
  }
  http.end();
  if (ok) {
    locationKnown = true;
    newsStage = NEWS_STAGE_FETCHING;
    configTime(gmtOffsetSec, 0, "pool.ntp.org", "time.nist.gov");
    logObservation("fixed a location: " + city + ", " + region + ", " + country +
                    " (ip " + publicIp + ")");
  } else {
    newsStage = NEWS_STAGE_LOCATION_FAILED;
    Serial.println("fetchLocation failed: " + failReason);
    logObservation("couldn't fix a location (" + failReason + ")");
  }
  return ok;
}

bool extractTitles(const String &xml, std::vector<String> &out, int maxItems) {
  out.clear();
  int idx = xml.indexOf("<title>");
  bool first = true; // the first <title> in the feed is the channel title, not a story
  while (idx != -1 && (int)out.size() < maxItems) {
    int start = idx + 7;
    int end = xml.indexOf("</title>", start);
    if (end == -1) break;
    String t = xml.substring(start, end);
    t.replace("<![CDATA[", "");
    t.replace("]]>", "");
    t.trim();
    if (first) {
      first = false;
    } else if (t.length() > 0) {
      out.push_back(t);
    }
    idx = xml.indexOf("<title>", end);
  }
  return out.size() > 0;
}

bool fetchNews() {
  if (WiFi.status() != WL_CONNECTED || city.length() == 0) return false;
  newsStage = NEWS_STAGE_FETCHING;
  WiFiClientSecure client;
  client.setInsecure(); // public RSS, no sensitive data — skipping cert validation to avoid pinning a CA on-device
  HTTPClient http;
  String q = city;
  q.replace(" ", "%20");
  String url = "https://news.google.com/rss/headlines/section/geo/" + q + "?hl=en-US&gl=US&ceid=US:en";
  http.begin(client, url);
  http.setTimeout(8000);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS); // Google issues a redirect for some locales/consent flows
  int code = http.GET();
  lastNewsHttpCode = code;
  bool ok = false;
  String failReason;
  if (code == 200) {
    String body = http.getString();
    ok = extractTitles(body, headlines, MAX_HEADLINES);
    if (!ok) failReason = "no headlines in a " + String(body.length()) + "-byte response";
  } else {
    failReason = "http " + String(code);
  }
  http.end();
  if (ok) {
    lastNewsFetchMs = millis();
    newsStage = NEWS_STAGE_READY;
    logObservation("caught " + String(headlines.size()) + " headlines near " + city);
  } else {
    newsStage = NEWS_STAGE_NEWS_FAILED;
    Serial.println("fetchNews failed: " + failReason);
    logObservation("news fetch failed (" + failReason + ")");
  }
  return ok;
}

// ---------------------------------------------------------------------
// WiFi: forage for the first open network, or prefer HOME_SSID if visible
// ---------------------------------------------------------------------
void onWifiConnected(const String &ssid, bool isHome) {
  wifiState = WIFI_CONNECTED_STATE;
  wifiIsHome = isHome;
  currentView = VIEW_IDLE;
  lastInteractionMs = millis();

  // fetchLocation()/fetchNews() below each block for a few seconds on a
  // real HTTP round-trip, so redraw between them — otherwise the screen
  // would just sit on "connected..." the whole time with no sign of
  // whether it's still working or has actually gotten stuck somewhere.
  newsStage = NEWS_STAGE_LOCATING;
  drawIdle();
  fetchLocation(); // also starts NTP sync, so the log line below has a better chance of a real timestamp
  String connectLine = "connected to \"" + ssid + "\"" + (isHome ? " (home base)" : " (foraged, open network)") +
                        " — signal " + String(WiFi.RSSI()) + "dBm";
  static bool bootReasonLogged = false;
  if (!bootReasonLogged) {
    bootReasonLogged = true;
    connectLine += " [boot: " + bootResetReason + "]";
  }
  logObservation(connectLine);
  drawIdle();
  fetchNews();
  drawIdle();
}

// The station-side WiFi events carry the one piece of information that
// actually distinguishes "wrong password" from "AP never answered" from
// "router kicked us off later" — the 802.11 reason code. Without this, a
// failed WiFi.begin() is indistinguishable from any other failed
// WiFi.begin(), which is exactly the dead end a silent forage loop is.
void onWifiEvent(arduino_event_id_t event, arduino_event_info_t info) {
  if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    uint8_t reason = info.wifi_sta_disconnected.reason;
    lastDisconnectReason = reason;
    Serial.printf("[wifi] disconnected, reason %u (%s)\n", reason,
                  WiFi.disconnectReasonName((wifi_err_reason_t)reason));
  } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
    Serial.printf("[wifi] got ip %s, rssi %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  }
}

// AUTH_FAIL on join is ambiguous between "wrong passphrase" and "this AP
// only offers a mode this radio can't negotiate" (WPA3-SAE-only and
// WPA2-Enterprise both present as an ordinary secured network in a scan).
// Printing the advertised auth mode separates the two.
const char *authModeName(wifi_auth_mode_t m) {
  switch (m) {
    case WIFI_AUTH_OPEN:            return "open";
    case WIFI_AUTH_WEP:             return "WEP";
    case WIFI_AUTH_WPA_PSK:         return "WPA-PSK";
    case WIFI_AUTH_WPA2_PSK:        return "WPA2-PSK";
    case WIFI_AUTH_WPA_WPA2_PSK:    return "WPA/WPA2-PSK";
    case WIFI_AUTH_ENTERPRISE:      return "WPA2-Enterprise";
    case WIFI_AUTH_WPA3_PSK:        return "WPA3-PSK";
    case WIFI_AUTH_WPA2_WPA3_PSK:   return "WPA2/WPA3-PSK";
    case WIFI_AUTH_WAPI_PSK:        return "WAPI-PSK";
    default:                        return "other";
  }
}

void scanAndConnect() {
  drawScanning();
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false); // modem sleep makes association flaky on this board and buys nothing while USB-powered
  // A scan started while a previous association attempt is still unwinding
  // comes back as WIFI_SCAN_FAILED, which used to look identical to "no
  // networks nearby". Drop any half-open association first (keeping the
  // radio on) and give it a moment to settle.
  WiFi.disconnect(false, true);
  delay(150);

  int n = WiFi.scanNetworks();
  Serial.printf("[wifi] scan returned %d\n", n);
  if (n < 0) {
    wifiState = WIFI_FORAGE_FAILED;
    lastWifiFailReason = (n == WIFI_SCAN_RUNNING) ? "scan still running" : "scan failed";
    lastScanAttemptMs = millis();
    currentView = VIEW_IDLE;
    drawIdle();
    return;
  }

  String chosenSsid;
  bool chosenIsHome = false;
  int openCount = 0;

  for (int i = 0; i < n; i++) {
    bool isOpen = (WiFi.encryptionType(i) == WIFI_AUTH_OPEN);
    if (isOpen) openCount++;
    Serial.printf("  %2d: %-32s ch%-3d %4d dBm %s\n", i, WiFi.SSID(i).c_str(),
                  WiFi.channel(i), WiFi.RSSI(i), authModeName(WiFi.encryptionType(i)));
  }

  if (n > 0 && strlen(HOME_SSID) > 0) {
    for (int i = 0; i < n; i++) {
      if (WiFi.SSID(i) == String(HOME_SSID)) {
        chosenSsid = HOME_SSID;
        chosenIsHome = true;
        break;
      }
    }
  }
  if (n > 0 && chosenSsid.length() == 0) {
    int bestRssi = -1000;
    for (int i = 0; i < n; i++) {
      if (WiFi.encryptionType(i) == WIFI_AUTH_OPEN && WiFi.RSSI(i) > bestRssi) {
        bestRssi = WiFi.RSSI(i);
        chosenSsid = WiFi.SSID(i);
      }
    }
  }
  WiFi.scanDelete();

  if (chosenSsid.length() == 0) {
    wifiState = WIFI_FORAGE_FAILED;
    // "saw 14 APs, 0 open, no HOME_SSID" is a very different problem from
    // "saw 0 APs" — the first means the network list is fine and the
    // filter rejected everything (e.g. HOME_SSID is a 5GHz-only SSID this
    // radio physically cannot see), the second means the radio saw nothing.
    lastWifiFailReason = String(n) + " aps, " + String(openCount) + " open, no match";
    Serial.println("[wifi] nothing to join: " + lastWifiFailReason);
    lastScanAttemptMs = millis();
    currentView = VIEW_IDLE;
    drawIdle();
    return;
  }

  Serial.printf("[wifi] joining \"%s\" (%s)\n", chosenSsid.c_str(), chosenIsHome ? "home" : "foraged, open");
  lastDisconnectReason = 0;
  if (chosenIsHome) {
    WiFi.begin(HOME_SSID, HOME_PASSWORD);
  } else {
    WiFi.begin(chosenSsid.c_str());
  }
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_CONNECT_TIMEOUT_MS) {
    delay(200);
  }
  if (WiFi.status() == WL_CONNECTED) {
    lastWifiFailReason = "";
    onWifiConnected(chosenSsid, chosenIsHome);
  } else {
    wifiState = WIFI_FORAGE_FAILED;
    uint8_t reason = lastDisconnectReason;
    lastWifiFailReason = reason ? ("join failed: " + String(WiFi.disconnectReasonName((wifi_err_reason_t)reason)))
                                : ("join timed out (status " + String((int)WiFi.status()) + ")");
    Serial.printf("[wifi] %s after %lums\n", lastWifiFailReason.c_str(), (unsigned long)(millis() - start));
    lastScanAttemptMs = millis();
    currentView = VIEW_IDLE;
    drawIdle();
  }
}

// ---------------------------------------------------------------------
// Word-wrap + pagination for the news view (headline list -> pages)
// ---------------------------------------------------------------------
void layoutHeadlines() {
  const int charW = 6 * TEXT_SIZE;
  const int lineH  = 10 * TEXT_SIZE;
  const int usableW = LCD_WIDTH - 2 * MARGIN;
  const int headerH = 10 * TEXT_SIZE + MARGIN;
  const int usableH = LCD_HEIGHT - headerH - MARGIN;
  const int charsPerLine = max(1, usableW / charW);
  const int linesPerPage = max(1, usableH / lineH);

  for (int i = 0; i < pageCount; i++) pageLines[i].clear();
  pageCount = 0;

  std::vector<String> allLines;
  if (headlines.empty()) {
    allLines.push_back("No headlines yet.");
    allLines.push_back("Shake to refresh once");
    allLines.push_back("Flipee is connected.");
  }
  for (size_t h = 0; h < headlines.size(); h++) {
    String text = "- " + headlines[h];
    String line = "";
    int start = 0, n = text.length();
    while (start < n) {
      int spaceIdx = text.indexOf(' ', start);
      String word = (spaceIdx == -1) ? text.substring(start) : text.substring(start, spaceIdx);
      if (line.length() == 0) {
        line = word;
      } else if ((int)(line.length() + 1 + word.length()) <= charsPerLine) {
        line += " " + word;
      } else {
        allLines.push_back(line);
        line = word;
      }
      start = (spaceIdx == -1) ? n : spaceIdx + 1;
    }
    if (line.length() > 0) allLines.push_back(line);
    if (h != headlines.size() - 1) allLines.push_back("");
  }

  for (size_t i = 0; i < allLines.size() && pageCount < 24; i += linesPerPage) {
    for (size_t j = i; j < allLines.size() && j < i + linesPerPage; j++) {
      pageLines[pageCount].push_back(allLines[j]);
    }
    pageCount++;
  }
  if (pageCount == 0) pageCount = 1;
  currentPage = 0;
}

// ---------------------------------------------------------------------
// Rendering — amber flip-dot theme by default; a quick squash-bar flash
// on view changes nods to flipdot-sim's squash-and-swap flip without
// running a per-pixel animation loop on a battery-powered chip.
// ---------------------------------------------------------------------
void flipTransition() {
  int barY = LCD_HEIGHT / 2 - 2;
  gfx->fillRect(0, barY, LCD_WIDTH, 4, theme.accent);
  delay(45);
}

// ---------------------------------------------------------------------
// A little personality: what Flipee is doing right now, in a few words
// plus a small ASCII mood face, so "still waiting" is never a mystery —
// the idle screen always shows the real reason a step hasn't finished.
// ---------------------------------------------------------------------
String activityLine() {
  if (wifiState == WIFI_FORAGE_FAILED) {
    return lastWifiFailReason.length() ? lastWifiFailReason : "no open wifi nearby";
  }
  if (wifiState != WIFI_CONNECTED_STATE || WiFi.status() != WL_CONNECTED) return "foraging for wifi...";
  switch (newsStage) {
    case NEWS_STAGE_LOCATING:        return "locating...";
    case NEWS_STAGE_LOCATION_FAILED: return "locate failed (http " + String(lastLocationHttpCode) + ")";
    case NEWS_STAGE_FETCHING:        return "reading local news...";
    case NEWS_STAGE_NEWS_FAILED:     return "news failed (http " + String(lastNewsHttpCode) + ")";
    case NEWS_STAGE_READY: {
      String ssid = WiFi.SSID();
      if (ssid.length() > 16) ssid = ssid.substring(0, 15) + "-";
      return "on: " + ssid + (wifiIsHome ? " (home)" : " (foraged)");
    }
    default: return "connected...";
  }
}

const char *moodFace() {
  if (wifiState == WIFI_FORAGE_FAILED) return "(x_x)";
  if (wifiState != WIFI_CONNECTED_STATE || WiFi.status() != WL_CONNECTED) return "(o_o)?";
  switch (newsStage) {
    case NEWS_STAGE_LOCATING:        return "(o_o)~";
    case NEWS_STAGE_LOCATION_FAILED: return "(?_?)";
    case NEWS_STAGE_FETCHING:        return "(^_-)";
    case NEWS_STAGE_NEWS_FAILED:     return "(u_u)";
    case NEWS_STAGE_READY:           return "(^_^)";
    default:                         return "(o_o)";
  }
}

void drawScanning() {
  flipTransition();
  gfx->fillScreen(theme.bg);
  gfx->setTextColor(theme.fg);
  gfx->setTextSize(2);
  int16_t x1, y1; uint16_t w, h;
  const char *msg = "(o_o)? foraging for wifi...";
  gfx->getTextBounds(msg, 0, 0, &x1, &y1, &w, &h);
  gfx->setCursor((LCD_WIDTH - (int)w) / 2, LCD_HEIGHT / 2 - (int)h / 2);
  gfx->println(msg);
}

void drawReflecting() {
  flipTransition();
  gfx->fillScreen(theme.bg);
  gfx->setTextColor(theme.accent);
  gfx->setTextSize(2);
  int16_t x1, y1; uint16_t w, h;
  const char *msg = "(o_O)! writing to sd card...";
  gfx->getTextBounds(msg, 0, 0, &x1, &y1, &w, &h);
  gfx->setCursor((LCD_WIDTH - (int)w) / 2, LCD_HEIGHT / 2 - (int)h / 2);
  gfx->println(msg);
}

void drawIdle() {
  // Landscape layout: clock in the left column, status stacked in the
  // right column — the old single vertical stack was tuned for a tall
  // 536px-high portrait screen and would overflow this rotated 240px-high
  // frame if reused as-is.
  flipTransition();
  struct tm t;
  gfx->fillScreen(theme.bg);
  gfx->setTextColor(theme.fg);

  bool haveTime = getLocalTime(&t, 200);
  char timeStr[8];
  if (haveTime) {
    int h = t.tm_hour;
    if (!CLOCK_24H) { h = h % 12; if (h == 0) h = 12; }
    snprintf(timeStr, sizeof(timeStr), "%d:%02d", h, t.tm_min);
  } else {
    snprintf(timeStr, sizeof(timeStr), "--:--");
  }

  const int leftColW = (LCD_WIDTH * 55) / 100; // clock gets the left ~55%, status the rest

  gfx->setTextSize(6);
  int16_t x1, y1; uint16_t w, h;
  gfx->getTextBounds(timeStr, 0, 0, &x1, &y1, &w, &h);
  int timeY = (LCD_HEIGHT - (int)h) / 2 - 14; // shifted up a bit to leave room for the date below
  gfx->setCursor((leftColW - (int)w) / 2, timeY);
  gfx->println(timeStr);

  if (haveTime) {
    char dateStr[16];
    const char *wd[] = {"Sun","Mon","Tue","Wed","Thu","Fri","Sat"};
    snprintf(dateStr, sizeof(dateStr), "%s %d/%d", wd[t.tm_wday], t.tm_mon + 1, t.tm_mday);
    gfx->setTextSize(2);
    gfx->getTextBounds(dateStr, 0, 0, &x1, &y1, &w, &h);
    gfx->setCursor((leftColW - (int)w) / 2, timeY + 56);
    gfx->setTextColor(theme.dim);
    gfx->println(dateStr);
  }

  // status column, right side
  const int xStat = leftColW + MARGIN;
  const int lineH = 18;
  int y = MARGIN;
  gfx->setTextSize(1);
  gfx->setTextColor(theme.dim);
  gfx->setCursor(xStat, y);
  gfx->print(moodFace());
  gfx->print(" ");
  gfx->print(activityLine());
  y += lineH;

  gfx->setCursor(xStat, y);
  if (locationKnown) {
    String loc = city + ", " + (country.length() ? country : region);
    gfx->print(loc);
  } else {
    gfx->print("location: unknown");
  }
  y += lineH;

  if (!headlines.empty()) {
    gfx->setCursor(xStat, y);
    gfx->printf("%d headlines - tilt for news", (int)headlines.size());
    y += lineH;
  }

  gfx->setCursor(xStat, y);
  if (HAS_BATTERY_ADC) {
    gfx->printf("battery: %d%%", batteryPercent());
  } else {
    gfx->print("battery: unmeasured");
  }
  y += lineH;

  if (sdReady) {
    gfx->setCursor(xStat, y);
    gfx->print("sd: ready - flip to journal");
    y += lineH;
  }

  // An abnormal reset means the board is crash-looping, not merely failing
  // to connect — the two look identical from across the room ("foraging"
  // forever) but need completely different fixes. With ENABLE_SD pinless
  // and no USB attached, the screen is the only place this can surface.
  if (bootResetReason != "power-on" && bootResetReason != "external pin") {
    gfx->setCursor(xStat, y);
    gfx->setTextColor(theme.accent);
    gfx->print("last boot: " + bootResetReason);
    gfx->setTextColor(theme.dim);
  }

  // connection dot, bottom-right of the status column
  uint16_t dotColor = (WiFi.status() == WL_CONNECTED) ? theme.accent : theme.dim;
  gfx->fillCircle(LCD_WIDTH - MARGIN - 4, LCD_HEIGHT - MARGIN - 4, 4, dotColor);
}

void drawNewsPage() {
  gfx->fillScreen(theme.bg);

  gfx->setTextSize(1);
  gfx->setTextColor(theme.dim);
  gfx->setCursor(MARGIN, MARGIN);
  gfx->printf("news near %s  %d/%d", city.length() ? city.c_str() : "?", currentPage + 1, pageCount);

  gfx->setTextSize(TEXT_SIZE);
  gfx->setTextColor(theme.fg);
  int y = MARGIN + 10 * TEXT_SIZE + MARGIN;
  int lineH = 10 * TEXT_SIZE;
  for (auto &ln : pageLines[currentPage]) {
    gfx->setCursor(MARGIN, y);
    gfx->println(ln);
    y += lineH;
  }

  if (pageCount > 1) {
    gfx->setTextSize(1);
    gfx->setTextColor(theme.dim);
    gfx->setCursor(MARGIN, LCD_HEIGHT - MARGIN - 8);
    gfx->print("tilt to page - shake to refresh");
  }
}

void drawReflectDone() {
  flipTransition();
  gfx->fillScreen(theme.bg);
  gfx->setTextColor(theme.accent);
  gfx->setTextSize(2);
  int16_t x1, y1; uint16_t w, h;
  const char *msg = lastReflectPath.length() ? "(^_^)v saved to sd" : "(u_u) sd not ready";
  gfx->getTextBounds(msg, 0, 0, &x1, &y1, &w, &h);
  gfx->setCursor((LCD_WIDTH - (int)w) / 2, LCD_HEIGHT / 2 - (int)h / 2 - 10);
  gfx->println(msg);
  if (lastReflectPath.length()) {
    gfx->setTextSize(1);
    gfx->setTextColor(theme.dim);
    gfx->getTextBounds(lastReflectPath.c_str(), 0, 0, &x1, &y1, &w, &h);
    gfx->setCursor((LCD_WIDTH - w) / 2, LCD_HEIGHT / 2 + 20);
    gfx->println(lastReflectPath);
  }
}

void drawSleep() {
  flipTransition();
  struct tm t;
  gfx->fillScreen(theme.bg);
  gfx->setTextColor(theme.dim);
  bool haveTime = getLocalTime(&t, 200);
  char timeStr[8];
  if (haveTime) {
    int h = t.tm_hour;
    if (!CLOCK_24H) { h = h % 12; if (h == 0) h = 12; }
    snprintf(timeStr, sizeof(timeStr), "%d:%02d", h, t.tm_min);
  } else {
    snprintf(timeStr, sizeof(timeStr), "--:--");
  }
  gfx->setTextSize(4);
  int16_t x1, y1; uint16_t w, h;
  gfx->getTextBounds(timeStr, 0, 0, &x1, &y1, &w, &h);
  gfx->setCursor((LCD_WIDTH - (int)w) / 2, LCD_HEIGHT / 2 - (int)h / 2 - 12);
  gfx->println(timeStr);

  const char *zzz = "(-.-) zzz";
  gfx->setTextSize(1);
  gfx->getTextBounds(zzz, 0, 0, &x1, &y1, &w, &h);
  gfx->setCursor((LCD_WIDTH - (int)w) / 2, LCD_HEIGHT / 2 + 28);
  gfx->println(zzz);
}

// ---------------------------------------------------------------------
const char *resetReasonName(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON:  return "power-on";
    case ESP_RST_EXT:      return "external pin";
    case ESP_RST_SW:       return "software reset";
    case ESP_RST_PANIC:    return "panic/crash";
    case ESP_RST_INT_WDT:  return "interrupt watchdog";
    case ESP_RST_TASK_WDT: return "task watchdog";
    case ESP_RST_WDT:      return "other watchdog";
    case ESP_RST_DEEPSLEEP:return "deep sleep wake";
    case ESP_RST_BROWNOUT: return "brownout (power dip)";
    case ESP_RST_SDIO:     return "sdio";
    default:               return "unknown";
  }
}

void setup() {
  // Captured before anything else — if the board is actually crash-looping
  // (stack overflow, watchdog, brownout) rather than just cleanly failing
  // to connect, this shows up on the *next* successful connection's SD log
  // line, so it's diagnosable even when testing away from a USB cable.
  bootResetReason = resetReasonName(esp_reset_reason());

  Serial.begin(115200);
  uint32_t bootWait = millis();
  while (!Serial && millis() - bootWait < 2000) { delay(10); }
  Serial.printf("Flipee booting (reset reason: %s)\n", bootResetReason.c_str());

  if (!imu.begin(IIC_SDA, IIC_SCL)) {
    Serial.println("QMI8658 not found.");
  } else {
    imu.setAccelRange(QMI8658_ACCEL_RANGE_4G);
    imu.setGyroRange(QMI8658_GYRO_RANGE_256DPS);
    imu.setAccelUnit_mps2(true);
    imu.setGyroUnit_rads(true);
    imu.enableSensors(QMI8658_ENABLE_ACCEL | QMI8658_ENABLE_GYRO);
  }

  if (!gfx->begin()) {
    Serial.println("gfx->begin() failed!");
  }
  gfx->setRotation(DISPLAY_ROTATION);

  if (ENABLE_SD && SD_SCK != -1 && SD_MISO != -1 && SD_MOSI != -1 && SD_CS != -1) {
    sdSPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);
    sdReady = SD.begin(SD_CS, sdSPI);
    if (!sdReady) Serial.println("SD.begin() failed — check wiring/pins.");
  } else if (ENABLE_SD) {
    Serial.println("ENABLE_SD is true but SD_SCK/MISO/MOSI/CS are unset — skipping SD journal.");
  }

  if (HAS_BATTERY_ADC && BATTERY_ADC_PIN >= 0) {
    analogReadResolution(12);
  }

  lastInteractionMs = millis();
  currentView = VIEW_IDLE;
  WiFi.onEvent(onWifiEvent);
  scanAndConnect(); // draws its own "foraging" screen first
}

void loop() {
  float pitchSin, accelMagG, gyroMagRadS;
  readImu(pitchSin, accelMagG, gyroMagRadS);
  uint32_t now = millis();

  // --- WiFi: keep trying if not connected, notice if it drops ---
  if (WiFi.status() != WL_CONNECTED) {
    if (wifiState == WIFI_CONNECTED_STATE) {
      wifiState = WIFI_UNCONNECTED; // dropped — will re-forage below
    }
    if (now - lastScanAttemptMs > WIFI_RETRY_MS) {
      lastScanAttemptMs = now;
      scanAndConnect();
    }
  } else if (wifiState == WIFI_CONNECTED_STATE && now - lastNewsFetchMs > NEWS_REFRESH_MS) {
    fetchNews();
    if (currentView == VIEW_IDLE) drawIdle();
  }

  // --- shake: refresh now ---
  if (accelMagG > SHAKE_THRESHOLD_G && now - lastShakeMs > SHAKE_COOLDOWN_MS) {
    lastShakeMs = now;
    lastInteractionMs = now;
    if (WiFi.status() != WL_CONNECTED) {
      lastScanAttemptMs = 0; // force an immediate rescan on the next loop pass
    } else {
      fetchNews();
      if (currentView == VIEW_NEWS) { layoutHeadlines(); drawNewsPage(); }
      else if (currentView == VIEW_IDLE) drawIdle();
    }
  }

  // --- flip/spin: write a journal reflection ---
  if (detectFlip(gyroMagRadS, now)) {
    writeReflection();
  }

  if (currentView == VIEW_NEWS) {
    if (now - lastPageTurnMs > PAGE_TURN_COOLDOWN_MS) {
      if (pitchSin > PAGE_TILT_THRESHOLD && currentPage < pageCount - 1) {
        currentPage++;
        lastPageTurnMs = now;
        lastInteractionMs = now;
        drawNewsPage();
      } else if (pitchSin < -PAGE_TILT_THRESHOLD && currentPage > 0) {
        currentPage--;
        lastPageTurnMs = now;
        lastInteractionMs = now;
        drawNewsPage();
      }
    }
    if (now - lastInteractionMs > MESSAGE_TIMEOUT_MS) {
      currentView = VIEW_IDLE;
      drawIdle();
    }
  } else if (currentView == VIEW_IDLE) {
    // tilt forward from idle opens the news view, if there's any to show
    if (pitchSin > PAGE_TILT_THRESHOLD && now - lastPageTurnMs > PAGE_TURN_COOLDOWN_MS) {
      lastPageTurnMs = now;
      lastInteractionMs = now;
      layoutHeadlines();
      currentView = VIEW_NEWS;
      drawNewsPage();
    } else if (now - lastInteractionMs > IDLE_SLEEP_MS) {
      currentView = VIEW_SLEEP;
      drawSleep();
    } else {
      struct tm t;
      if (getLocalTime(&t, 10) && t.tm_min != lastDrawnMinute) {
        lastDrawnMinute = t.tm_min;
        drawIdle();
      }
    }
  } else if (currentView == VIEW_SLEEP) {
    struct tm t;
    if (getLocalTime(&t, 10) && t.tm_min != lastDrawnMinute) {
      lastDrawnMinute = t.tm_min;
      drawSleep();
    }
    // any real motion wakes it back to idle
    if (accelMagG > 1.3f || fabsf(pitchSin) > 0.2f) {
      lastInteractionMs = now;
      currentView = VIEW_IDLE;
      drawIdle();
    }
  } else if (currentView == VIEW_REFLECT) {
    if (now - reflectShownAtMs > 3000) {
      currentView = VIEW_IDLE;
      drawIdle();
    }
  }

  delay(50); // sensor-and-gesture paced, not an animation loop
}

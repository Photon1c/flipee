# Flipee

A small ESP32-S3 device with some personality and freedom to roam, for the
Waveshare ESP32-S3-AMOLED-1.91 (RM67162, 240×536 native panel run rotated
into a 536×240 landscape frame, QMI8658 IMU) — the same board AgentWatch
runs on, and themed after the amber flip-dot look from flipdot-sim.

Flipee wakes up, **forages for the first open WiFi network it can find**,
and once connected, tries to work out roughly where it's landed and what's
going on nearby — a rough location from its own public IP, then a handful
of local news headlines for that place. It's LiPo-powered through an SPST
switch, so it's meant to be picked up and carried somewhere new.

## Why this architecture

AgentWatch could assume its PC relay was always one WiFi hop away, because
its `.ino` connects to a fixed home network. Flipee can't make that
assumption — it might be sitting on a coffee shop's open WiFi with your PC
nowhere nearby. So the split here is different: everything that only needs
the *open internet* (geolocation, news, display, sensors, the SD journal)
runs entirely on the device. The one thing that genuinely needs an LLM —
turning a day's headlines into a written reflection — is *opportunistic*:
Flipee tries a relay with a short timeout and falls back to a templated
reflection if nothing answers.

That relay doesn't have to live on your PC. Point `SERVER_HOST_VAL` at a small
always-on host instead (a VPS, see below) and Claude-written reflections
work from whatever network Flipee forages onto, not just a network you
control — the templated fallback still covers you if the relay is ever
down or unreachable.

## Hardware

| Signal | GPIO | Status |
|---|---|---|
| LCD CS / SCLK / SDIO0-3 / RST | 6 / 47 / 18 / 7 / 48 / 5 / 17 | confirmed (same as AgentWatch/FluidTilt-1.91) |
| IMU (QMI8658) SDA / SCL | 40 / 39 | confirmed (same as AgentWatch/FluidTilt-1.91) |
| microSD SCK / MISO / MOSI / CS | — | **not verified** — fill in from your board before enabling |
| Battery ADC pin | — | **not verified** — most builds won't have a wired divider, and that's fine |

The display and IMU pins came from the same verified-working sketch
AgentWatch uses on this exact board. The microSD and battery-ADC wiring
weren't confirmed for this build, so both are off/unset by default —
Flipee runs fine without them, just without a journal or a battery
reading. Fill in `SD_SCK`/`SD_MISO`/`SD_MOSI`/`SD_CS` in the
customization block once you've confirmed them against your board, and
set `ENABLE_SD` (already `true`) will pick them up automatically. Same
idea for `HAS_BATTERY_ADC` / `BATTERY_ADC_PIN` / `BATTERY_DIVIDER_RATIO`.

## Setup

### 1. The sketch (on the board)

Required libraries:
- **GFX Library for Arduino** by moononournation
- **QMI8658** by Lahav Gahali
- **ArduinoJson** by Benoit Blanchon, v7.x
- `WiFi.h` / `WiFiClientSecure.h` / `HTTPClient.h` / `SD.h` / `SPI.h` — all
  bundled with the esp32 core, nothing extra to install

Board settings: arduino-esp32 core v3.3.0+, USB CDC On Boot enabled,
PSRAM set to OPI PSRAM.

**Credentials first.** They live in `Flipee/secrets.h`, which is
gitignored so a WiFi passphrase never ends up in a commit. Copy the
template and fill it in — the sketch won't compile without it:

```
copy Flipee\secrets.example.h Flipee\secrets.h
```

- `HOME_SSID_VAL` / `HOME_PASSWORD_VAL` — optional; leave both `""` for
  pure foraging
- `SERVER_HOST_VAL` — your relay's address (VPS domain/IP, or your PC's
  LAN IP for same-LAN testing), see below
- `RELAY_KEY_VAL` — must match `FLIPEE_RELAY_KEY` on the relay, see below

Everything else stays in the `CUSTOMIZATION` block near the top of
`Flipee/Flipee.ino`:
- `SERVER_PORT` — defaults to `5024`, match the relay
- `RELAY_USE_TLS` — `true` for a VPS behind a reverse proxy (the default),
  `false` only for same-LAN testing against a plain `python flipee_relay.py`
- `SD_SCK` / `SD_MISO` / `SD_MOSI` / `SD_CS` — once confirmed for your board
- `HAS_BATTERY_ADC` / `BATTERY_ADC_PIN` / `BATTERY_DIVIDER_RATIO` — optional

Flash it. It'll show "foraging for wifi..." briefly, then settle onto the
clock face once it finds something to join.

### When it won't connect

Flipee reports what actually went wrong rather than retrying in silence.
Open the serial monitor at 115200 and you'll get the full scan plus the
802.11 reason code for a failed join:

```
[wifi] scan returned 5
   0: MyHomeWiFi        ch1    -38 dBm WPA2/WPA3-PSK
[wifi] joining "MyHomeWiFi" (home)
[wifi] disconnected, reason 202 (AUTH_FAIL)
```

`AUTH_FAIL` is a wrong passphrase. `NO_AP_FOUND` means the SSID wasn't
visible — worth checking it isn't a 5GHz-only network, which this radio
physically cannot see. The same short reason also appears on the idle
screen, so the device stays diagnosable with no cable attached, as does
an abnormal reset reason (`panic/crash`, `brownout`) if it's actually
crash-looping rather than merely failing to connect.

### 2. The relay (optional)

Skip this entirely and Flipee still works — reflections just come from
its on-device templates instead of Claude.

**Same-LAN testing** (quick, on your own PC):

```
pip install -r server/requirements.txt
copy server\.env.example server\.env
```

Edit `server/.env`, set `ANTHROPIC_API_KEY` and `FLIPEE_RELAY_KEY` (any
long random string — it just needs to match `RELAY_KEY_VAL` in
`Flipee/secrets.h`). Then:

```
python server/flipee_relay.py
```

This binds plain HTTP on `0.0.0.0:5024`. Set `RELAY_USE_TLS = false` in
the `.ino` to match, and `HOME_SSID_VAL` to this machine's network — an
open network Flipee forages elsewhere won't reach a laptop on your LAN.

On Windows, binding the port isn't enough: the firewall blocks inbound
connections by default, so Flipee's POST is dropped before Flask sees it
and you get a templated reflection with no indication why. Run this once
(it self-elevates):

```
powershell -ExecutionPolicy Bypass -File server\allow-relay-firewall.ps1
```

It adds a single inbound TCP 5024 rule scoped to the **Private** profile
and the local subnet, so it never applies on a public network. Undo it
with the same script and `-Remove`.

To confirm the relay is actually being reached rather than silently
falling back, flip Flipee and watch the serial monitor:

```
[relay] POST http://10.0.0.x:5024/reflect
[relay] ok, 885 chars of Claude
```

A `[relay] failed (http -1: ...)` there means unreachable — wrong
`SERVER_HOST_VAL`, relay not running, or the firewall rule missing. A
`401` means `RELAY_KEY_VAL` doesn't match `FLIPEE_RELAY_KEY`.

**On a VPS** (recommended — works from any network Flipee forages onto):
run the same `flipee_relay.py`, but behind a real WSGI server and a TLS
reverse proxy rather than the command above. Full instructions, including
a gunicorn command and an nginx server block, are in the module docstring
at the top of `server/flipee_relay.py`. Once it's up: `SERVER_HOST_VAL` in
`secrets.h` becomes your VPS's domain, `RELAY_USE_TLS` stays `true`, and
`RELAY_KEY_VAL` must match `FLIPEE_RELAY_KEY` in the VPS's `.env` — the relay
refuses every request with 503 until that key is set, so it can't
accidentally run open on the internet.

## Interacting with it

No keyboard — same physical-input idea as AgentWatch, plus one gesture
that's new to Flipee:

| Gesture | What it does |
|---|---|
| Tilt away from you (from idle) | Open the news view |
| Tilt away / toward you (in news view) | Next / previous page |
| Firm shake | Refresh now — re-scan WiFi if disconnected, else re-fetch news |
| Fast, deliberate spin/flip | Write a journal reflection to the SD card |
| Leave it alone for a few minutes | Dims to a quiet clock; any real motion wakes it |

The flip gesture is detected as a sustained burst of angular velocity
(`FLIP_GYRO_THRESHOLD` / `FLIP_SUSTAIN_MS` in the customization block) —
it's a best-effort heuristic, not a verified "upside-down" detector, so
expect to tune the threshold once you have the board in hand.

## The SD journal

Flipee keeps a running diary without you ever touching it — every
successful WiFi connection, location fix, and news fetch gets a
timestamped one-liner appended to a daily log:

```
/flipee/2026-09-24.md
/flipee/2026-09-25.md
```

The flip gesture writes something fuller to `/flipee/reflections/`: what
it saw, where it thinks it is, its battery, and a short reflection —
Claude-written if the relay answered in time, otherwise picked from a
small set of on-device templates and filled in with a real headline it
actually fetched, so it's never just filler text:

```
/flipee/reflections/2026-09-24_143207.md
```

## Customization

| Setting | What it does |
|---|---|
| `DISPLAY_ROTATION` | `1` (default) = landscape, rotated 90° clockwise from the native panel orientation by the library's convention. Not verified in hardware which of `1`/`3` is actually clockwise on this exact unit — if it comes out mirrored, flip to `3`. `LCD_WIDTH`/`LCD_HEIGHT` (536×240) assume one of the two 90° rotations; a portrait rotation (`0`/`2`) would need those swapped back and `drawIdle()`'s layout reworked. |
| `HOME_SSID_VAL` / `HOME_PASSWORD_VAL` (in `secrets.h`) | Preferred network when visible; blank = always forage |
| `WIFI_RETRY_MS` | How often to rescan after a failed forage |
| `SERVER_HOST_VAL` / `RELAY_KEY_VAL` (in `secrets.h`), `SERVER_PORT` / `RELAY_USE_TLS` | Where the relay lives and how to reach it |
| `NEWS_REFRESH_MS` | How often to re-fetch headlines while connected |
| `IDLE_SLEEP_MS` | How long untouched before dimming to a quiet clock |
| `SHAKE_THRESHOLD_G` / `SHAKE_COOLDOWN_MS` | Shake sensitivity and minimum time between shakes |
| `PAGE_TILT_THRESHOLD` / `PAGE_TURN_COOLDOWN_MS` | Tilt-to-page sensitivity |
| `FLIP_GYRO_THRESHOLD` / `FLIP_SUSTAIN_MS` / `FLIP_COOLDOWN_MS` | Flip/spin gesture tuning |
| `theme` | `THEME_FLIPDOT` (amber, default), `THEME_MIDNIGHT`, `THEME_PAPER`, or your own |

On the relay side, `flipee_relay.py` has `MODEL` and `SYSTEM_PROMPT` near
the top — the system prompt currently asks for two or three short, wry,
first-person paragraphs; adjust the voice there if you want something
different. `FLIPEE_RELAY_KEY` and `FLIPEE_RELAY_PORT` (default `5024`)
are set via `.env`, not in the Python file.

## How location and news actually work

No API keys needed for either, which is what makes both work on whatever
random open network Flipee ends up foraging, not just your home WiFi:

- **Location**: a plain HTTP `GET` to `ip-api.com`'s free JSON endpoint,
  keyed off Flipee's own public IP — city, region, country, lat/lon, and
  a UTC offset it feeds straight into `configTime()`.
- **News**: Flipee's inferred city is dropped into a public Google News
  RSS feed (`news.google.com/rss/headlines/section/geo/<city>`) fetched
  over HTTPS (`WiFiClientSecure::setInsecure()` — public RSS, nothing
  sensitive, so skipping cert pinning on-device is a reasonable trade).
  `<title>` tags are pulled out with plain substring scanning rather than
  a full XML parser, since the feed shape is simple and predictable.

## Extending it

- **Better flip detection**: right now "flip" is a sustained gyro-
  magnitude burst, not a real orientation check, because the IMU's axis
  mapping relative to the screen wasn't verified here. Once you have the
  board in hand, comparing settled accelerometer sign before/after a
  motion spike would make the gesture more literal.
- **More gesture types**: `detectFlip()` and the shake logic in `loop()`
  are the two patterns to copy for a third gesture — e.g. the unused
  touch controller pins on this board for a tap-to-refresh.
- **Richer relay context**: `flipee_relay.py` currently gets one-shot
  context per request. Keeping a rolling history of past reflections and
  passing a few back in would let Claude's entries build on each other
  rather than starting fresh every time.
- **Multiple devices**: like AgentWatch, the relay doesn't key anything by
  device — fine for one Flipee, would need a device ID to support more.

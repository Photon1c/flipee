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
`Flipee/secrets.h`), and `FLIPEE_DASHBOARD_KEY` (a *different* long random
string — the password for the dashboard, see below). Then:

```
python server/flipee_relay.py
```

This binds plain HTTP on `0.0.0.0:5024`. Set `RELAY_USE_TLS = false` in
the `.ino` to match, and `HOME_SSID_VAL` to this machine's network — an
open network Flipee forages elsewhere won't reach a laptop on your LAN.

On Windows, binding the port isn't enough: the firewall blocks inbound
connections by default, so Flipee's POST is dropped before Flask sees it
and you get a templated reflection with no indication why. Fix it once by
**right-clicking `server\allow-relay-firewall.cmd` and choosing "Run as
administrator"**.

It adds a single inbound TCP 5024 rule scoped to the **Private** profile
and the local subnet, so it never applies on a public network. Run it
again with `remove` to undo.

Use the `.cmd`, not the `.ps1` directly — `.ps1` files have no "Run as
administrator" in their right-click menu, so the PowerShell script has to
elevate itself into a *second* window that disappears on any error. The
`.cmd` elevates first and keeps everything in one window you can read.
Verify it worked with:

```
netsh advfirewall firewall show rule name="Flipee relay (TCP 5024)"
```

(`netsh` rather than `Get-NetFirewallRule` — the cmdlet returns an empty
list rather than an error in some restricted shells, which looks exactly
like a missing rule.)

To confirm the relay is actually being reached rather than silently
falling back, flip Flipee and watch the serial monitor:

```
[relay] POST http://10.0.0.x:5024/reflect
[relay] ok, 1194 chars of Claude
```

The failure modes are distinguishable, which is the point of logging it:

| Serial line | What it means |
|---|---|
| `failed (http -1: ...)` | Never connected — wrong `SERVER_HOST_VAL`, relay not running, or firewall rule missing |
| `failed (http -11: read Timeout)` | Connected fine, but the reply didn't arrive inside `RELAY_TIMEOUT_MS` |
| `failed (http 401: ...)` | Reached the relay; `RELAY_KEY_VAL` doesn't match `FLIPEE_RELAY_KEY` |
| `failed (http 503: ...)` | Relay is running with no `FLIPEE_RELAY_KEY` set, so it refuses everything |

### Why there are two relay timeouts

`RELAY_CONNECT_TIMEOUT_MS` (2500) and `RELAY_TIMEOUT_MS` (15000) cover
genuinely different waits, and collapsing them into one number makes a
healthy relay look broken.

Connecting answers "is anything listening at all?" Out in the wild the
answer is usually no, and the gesture should stay responsive, so that
stays short. But once the handshake succeeds the relay has committed to
an LLM round-trip, which measured **3.4–4.0s on loopback alone** before
any WiFi hop. A shared 2.5s budget expired mid-generation on every single
flip — the relay was working perfectly and the device reported it as
unreachable every time. If you swap `MODEL` in `flipee_relay.py` for a
larger one, raise `RELAY_TIMEOUT_MS` to match.

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

## The dashboard

The SD journal has one problem: it's write-only from a human's point of
view. Reading it means pulling the card out, and on a build with no SD
wired up (the default, since those pins aren't verified) the reflections
aren't kept anywhere at all — they scroll off the screen and are gone.

So the relay keeps them too. It already receives the full context of
every reflection it writes, so `/reflect` now archives each one and
serves a small web GUI for reading them back — at `http://<relay
host>:5024/`, or your domain once it's behind nginx:

```
python server/flipee_relay.py     # dashboard at http://<this machine>:5024/
```

Entries are listed newest-first with their location, network, battery and
timestamp (stored UTC, shown in your local time), the headlines each one
was reacting to, and filters for device, day, and a free-text search
across city/region/country/SSID — "where was it" is sometimes a city and
sometimes the name of a cafe's WiFi. Every filter is in the URL, so views
are shareable, and `/entries` returns the same query as JSON for
scripting. Individual entries permalink at `/entry/<id>`.

**It's password-protected and fails closed.** The archive is a log of
where a device you carry around has been and when, which is worth more
protection than the reflections themselves suggest. The password is
`FLIPEE_DASHBOARD_KEY` in `.env`, and with it unset the dashboard returns
503 rather than serving open — the same stance `/reflect` takes with
`FLIPEE_RELAY_KEY`. It's deliberately a *separate* secret from the relay
key: that one is compiled into the sketch and travels with the hardware,
and shouldn't also unlock the archive. Failed logins are throttled per
IP. Once there's TLS in front of it, set `FLIPEE_DASHBOARD_HTTPS=1` so
the session cookie is marked `Secure` (it's off by default because a
Secure cookie is never sent over plain HTTP, which makes same-LAN login
look like it silently fails).

Storage is a SQLite file — `server/flipee.sqlite3` unless you point
`FLIPEE_DB_PATH` somewhere else, which you probably want on a VPS where
the checkout gets replaced on deploy. It's gitignored, and scp'ing it off
the host is a complete backup. Every row is keyed by `device_id`, a
stable fingerprint derived from the board's efuse MAC (so it survives
reflashing); set `DEVICE_NAME` in the sketch to show something friendlier
than `flipee-3fa91c` once there's more than one. Archiving is
best-effort: if the database is locked or the disk is full, the device
still gets its reflection, it just won't show up here.

## Why the entries build on each other

The first version of the relay sent one request's worth of facts and
nothing else, and it showed. Twelve consecutive entries from the same
desk all opened "Woke up in Federal Way", all picked the same two
headlines out of the same cached feed, all announced plans to move on
from a network the device never left, and two written ten seconds apart
both discovered the town from scratch. One even called it morning at
two in the afternoon.

None of that was the model failing. Nothing in the prompt said: you have
been here seven hours, you wrote four minutes ago, you already used the
apartment fire, and you are not going anywhere. So now it does. Before
each entry the relay assembles a briefing (`context.py`):

- **State** — entry number, how long since the last one, whether it has
  moved, how long it's been in this place, which networks it's used
  here, where else it has woken up. All indexed counts, no model calls.
- **Place** — a Wikipedia summary for the city, fetched once and cached
  forever (`place.py`). Knowing Federal Way is a 100k-person suburb 25
  miles south of Seattle gives it something to think *with*, so the
  headlines stop being the only evidence the place exists.
- **Memory** — what it distilled from earlier entries, below.
- **Headlines split into new vs already seen**, so a feed that hasn't
  changed reads as "nothing new" rather than as a fresh discovery.
- **Its last two entries verbatim**, with instructions not to echo their
  openings or closing moves.

Plus the honesty constraints that state makes possible: don't claim a
battery level with no sensor, don't call it morning unless the device's
own clock says so, don't announce you're moving on unless something says
you are.

**Memory that outlives the window.** Feeding back the last two entries
buys continuity for about an hour; once an entry scrolls out, whatever
Flipee worked out in it is gone. So after each reflection a second cheap
call (`memory.py`) distills three things into SQLite: durable **notes**
about the place and about itself, **threads** it left open, and
**covered** angles it shouldn't reuse. That's the difference between a
longer prompt and actual state — and it's why a second flip five minutes
later now opens "Still here. Still Homenode." and picks up a question it
asked itself rather than re-reporting the news.

It runs on a background thread *after* the device has its reply. Flipee
waits at most `RELAY_TIMEOUT_MS` before falling back to a templated
reflection, and that budget belongs to the entry someone is standing
there waiting to read, not to bookkeeping. If the update fails, the next
entry is slightly less informed and nothing else breaks.

**Memory persists mistakes too**, which is why there's a page for it.
An entry once inferred that Tacoma is north of Federal Way (it's south),
and the distiller dutifully filed it as a fact about the place — from
then on it was in every briefing, stated as something Flipee knew, with
nothing anywhere to contradict it.

So **`/memory` shows what it currently believes, with the claims that
need a human underlined.** A sentence gets marked when it isn't
traceable to anything Flipee actually read — a spatial relationship, a
number in no headline or place summary, or a name no source mentions.
Hover for the reason. Everything is editable in place, and "Forget this"
drops a place's notes while leaving its reflections alone, so it starts
over from what it reads rather than from what it concluded.

The check is string matching against sources already in the database —
no model call. A review pass that cost tokens would be a strange way to
economize on a memory system built to save them.

It deliberately only flags claims that are *checkable and wrong-able*.
"Twelve entries in one place feels like commitment or being stuck" is
not a claim about the world, and flagging introspection would train you
to ignore the highlights — which is worse than not having them. Two
early false positives are instructive: `below` matched "below the noise
threshold" (metaphor, not geography), and "Federal Way's" read as an
unsourced name because the possessive didn't match the sourced "Way".
Both are fixed; both are the kind of thing to watch for when extending
the rules.

Set `FLIPEE_MEMORY=0` for one call per flip and no distillation, or
`FLIPEE_PLACE_LOOKUP=0` to keep its knowledge strictly first-hand.

### What that costs

Run `python server/token_report.py` for a live per-section breakdown
against your own archive. Counting is free and doesn't run inference, so
re-run it after any prompt change. On Claude Haiku 4.5 at the time of
writing, one flip measured:

```
REFLECTION CALL
  system prompt                                     501
  YOUR STATE                                        245
  WHAT YOU'VE WORKED OUT ABOUT THIS PLACE           132
  HEADLINES YOU HAVE ALREADY SEEN                    91
  BACKGROUND ON THIS PLACE                           89
  ...
  YOUR LAST ENTRIES, VERBATIM                       256
  TOTAL INPUT                                      1621
MEMORY CALL (only when there's something new)      1223
```

**The briefing does not grow with the archive.** That's the property
worth protecting: state comes from indexed counts rather than rows,
`covered` caps at 14, `threads` at 4, the place summary at 700
characters, and the verbatim window at one or two entries. A thousand
reflections from now the prompt is the same size. Actual per-entry usage
is recorded in `in_tokens`/`out_tokens` and shown on each dashboard
entry, so that claim stays checkable rather than aspirational.

Three things keep it down, each one measured rather than assumed:

- **The distillation call is skipped when it has nothing to learn.** It's
  ~1223 tokens — 43% of a flip — and three flips ten seconds apart used
  to buy three near-identical rewrites of the same notes. Now it only
  runs if the feed moved or `MIN_GAP_MINUTES` has passed.
- **One verbatim entry instead of two, once notes exist.** That block was
  the most expensive part of the briefing (479 tokens). The notes already
  carry what the older entry *said*; one sample is enough to carry how it
  sounded. 479 → 256.
- **Already-read headlines are truncated to three plus a count.** Listing
  all eight spent ~200 tokens showing the model things the same prompt
  tells it to treat as background. 208 → 91.

**Prompt caching doesn't help here, and it's worth knowing why.** Claude
Haiku 4.5 won't cache a prefix below **4096 tokens**, and the whole
request is ~1621 — a `cache_control` marker would be silently ignored
(`cache_creation_input_tokens: 0`, no error). The minimum isn't monotonic
across models, so this is worth re-checking rather than assuming if you
change `MODEL`. Padding the prompt to reach the minimum would cost more
than the cache saves, and flips are usually further apart than the
5-minute TTL anyway, so the entries would mostly be cold.

## Customization

| Setting | What it does |
|---|---|
| `DISPLAY_ROTATION` | `1` (default) = landscape, rotated 90° clockwise from the native panel orientation by the library's convention. Not verified in hardware which of `1`/`3` is actually clockwise on this exact unit — if it comes out mirrored, flip to `3`. `LCD_WIDTH`/`LCD_HEIGHT` (536×240) assume one of the two 90° rotations; a portrait rotation (`0`/`2`) would need those swapped back and `drawIdle()`'s layout reworked. |
| `HOME_SSID_VAL` / `HOME_PASSWORD_VAL` (in `secrets.h`) | Preferred network when visible; blank = always forage |
| `WIFI_RETRY_MS` | How often to rescan after a failed forage |
| `SERVER_HOST_VAL` / `RELAY_KEY_VAL` (in `secrets.h`), `SERVER_PORT` / `RELAY_USE_TLS` | Where the relay lives and how to reach it |
| `DEVICE_NAME` | Friendly name for this board in the relay's dashboard; blank = its auto-generated `flipee-xxxxxx` id |
| `RELAY_CONNECT_TIMEOUT_MS` / `RELAY_TIMEOUT_MS` | How long to wait for the relay to answer, then to reply (see above) |
| `NEWS_REFRESH_MS` | How often to re-fetch headlines while connected |
| `IDLE_SLEEP_MS` | How long untouched before dimming to a quiet clock |
| `SHAKE_THRESHOLD_G` / `SHAKE_COOLDOWN_MS` | Shake sensitivity and minimum time between shakes |
| `PAGE_TILT_THRESHOLD` / `PAGE_TURN_COOLDOWN_MS` | Tilt-to-page sensitivity |
| `FLIP_GYRO_THRESHOLD` / `FLIP_SUSTAIN_MS` / `FLIP_COOLDOWN_MS` | Flip/spin gesture tuning |
| `theme` | `THEME_FLIPDOT` (amber, default), `THEME_MIDNIGHT`, `THEME_PAPER`, or your own |

On the relay side, `flipee_relay.py` has `MODEL` and `SYSTEM_PROMPT` near
the top — the system prompt currently asks for two or three short, wry,
first-person paragraphs; adjust the voice there if you want something
different — though note that the briefing in `context.py` now does much
of the work the system prompt used to. `FLIPEE_RELAY_KEY`,
`FLIPEE_DASHBOARD_KEY`, `FLIPEE_DASHBOARD_HTTPS`, `FLIPEE_DB_PATH`,
`FLIPEE_MEMORY`, `FLIPEE_MEMORY_MODEL`, `FLIPEE_PLACE_LOOKUP` and
`FLIPEE_RELAY_PORT` (default `5024`) are set via `.env`, not in the
Python file.

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
- **Reviewing entries, not just memory**: `/memory` highlights unsourced
  claims in the notes, but the entries those claims came from aren't
  marked. The same `review.annotate()` would work on entry text — the
  reason it isn't wired up is that entries are mostly introspection, so
  the signal-to-noise would be much worse without tighter rules.
- **Trimming the briefing**: the prompt now carries state, place
  background, memory, and two full entries. That's the right trade while
  the archive is small, but the verbatim entries dominate it and a
  summary of them would cost less. Worth measuring before assuming.
- **Multiple devices**: the archive is keyed by `device_id` and the
  dashboard filters on it, so a second Flipee needs nothing beyond a
  `DEVICE_NAME`. What's still single-tenant is the *key*: every device
  shares one `FLIPEE_RELAY_KEY`, so revoking one means reflashing all of
  them.

"""
flipee_relay.py — an optional companion for Flipee (the ESP32-S3 device).

WHAT IT DOES
------------
Flipee can write journal reflections entirely on its own (a small set of
templated lines filled in with a real headline it fetched). This relay is
what lets those reflections be Claude-written instead: Flipee POSTs what
it saw today, this server asks Claude to write a short first-person
journal entry in Flipee's voice, and Flipee saves whatever comes back to
its SD card.

It's optional and opportunistic by design. Flipee tries this relay with a
short timeout on every "flip to reflect" gesture and just falls back to
its own templated reflection if nothing answers — so it keeps working even
when the relay is down or unreachable.

DEPLOYING THIS SOMEWHERE ALWAYS-ON (recommended)
-------------------------------------------------
Run this on a small VPS rather than your own PC and Flipee can reach it
from whatever network it forages onto, not just one you control. Once
this is reachable from the open internet:

  - Set FLIPEE_RELAY_KEY (below) to a long random value, and put the same
    value in RELAY_KEY in Flipee.ino. Every request must carry it in an
    `X-Flipee-Key` header or it's rejected with 401 — an unauthenticated
    /reflect on the public internet is an open invitation for randoms to
    spend your Anthropic credits. The server refuses to serve /reflect at
    all (503) if this isn't set, rather than silently running unlocked.
  - Don't run this with `python flipee_relay.py` in production — that's
    Flask's development server. Put a real WSGI server behind a reverse
    proxy that terminates TLS instead, e.g.:

        pip install -r requirements.txt -r requirements-vps.txt
        gunicorn -w 2 -b 127.0.0.1:5024 flipee_relay:app

    ...with nginx in front doing TLS termination and proxying to
    127.0.0.1:5024, and a Let's Encrypt cert (certbot) for the domain you
    point Flipee.ino's SERVER_HOST at. A minimal nginx server block:

        server {
            listen 443 ssl http2;
            server_name flipee.example.com;
            ssl_certificate     /etc/letsencrypt/live/flipee.example.com/fullchain.pem;
            ssl_certificate_key /etc/letsencrypt/live/flipee.example.com/privkey.pem;
            location / {
                proxy_pass http://127.0.0.1:5024;
                proxy_set_header Host $host;
                proxy_set_header X-Real-IP $remote_addr;
            }
        }

  - Set Flipee.ino's RELAY_USE_TLS to true (it is by default) so it talks
    https:// to whatever SERVER_HOST resolves to.

SETUP (local / same-LAN testing)
---------------------------------
    pip install -r requirements.txt
    copy .env.example .env
    -- edit .env, set ANTHROPIC_API_KEY and FLIPEE_RELAY_KEY --

    python flipee_relay.py

This binds to 0.0.0.0 over plain HTTP, fine for a quick test against a
device on the same trusted LAN. Set Flipee.ino's RELAY_USE_TLS to false
to match, since there's no TLS listener here without the nginx setup
above.

This is a local development server (Flask's built-in one). It's fine for
same-LAN testing but should not face the internet directly — see the
gunicorn/nginx setup above for that.
"""

import hmac
import os

from flask import Flask, request, jsonify
from dotenv import load_dotenv

import anthropic

load_dotenv()

# ---------------------------------------------------------------------
# Config — change the model here if you'd rather use a different one
# (e.g. "claude-opus-5-5" for higher quality, "claude-haiku-4-5-20251001"
# for the fastest/cheapest replies).
# ---------------------------------------------------------------------
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 400
RELAY_KEY = os.environ.get("FLIPEE_RELAY_KEY", "")
PORT = int(os.environ.get("FLIPEE_RELAY_PORT", "5024"))
SYSTEM_PROMPT = (
    "You are Flipee, a small battery-powered ESP32 device that forages for "
    "open WiFi networks, infers roughly where it is from the connecting IP, "
    "reads local news headlines for that place, and writes short markdown "
    "journal entries about what it noticed. Write in first person, a little "
    "wry and curious — a tiny travel diary kept by a gadget that likes not "
    "knowing where it'll wake up next. Two or three short paragraphs at "
    "most, plain prose (no headers or bullet lists — the caller adds its "
    "own structure around this). You may reference the headlines you were "
    "given, but react to them rather than just listing them."
)

app = Flask(__name__)
client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment


@app.route("/reflect", methods=["POST"])
def reflect():
    if not RELAY_KEY:
        # Fail closed: refuse to serve an unauthenticated endpoint rather than
        # silently running open once this is reachable from the internet.
        return jsonify({"ok": False, "error": "FLIPEE_RELAY_KEY is not configured on this server"}), 503
    if not hmac.compare_digest(request.headers.get("X-Flipee-Key", ""), RELAY_KEY):
        return jsonify({"ok": False, "error": "missing or invalid X-Flipee-Key"}), 401

    payload = request.get_json(silent=True) or {}
    city = (payload.get("city") or "").strip()
    region = (payload.get("region") or "").strip()
    country = (payload.get("country") or "").strip()
    ssid = payload.get("ssid") or "an open network"
    battery_pct = payload.get("battery_pct", -1)
    uptime_s = payload.get("uptime_s", 0)
    headlines = payload.get("headlines") or []

    location = ", ".join(p for p in [city, region, country] if p) or "unknown"
    battery_line = f"{battery_pct}% battery" if isinstance(battery_pct, int) and battery_pct >= 0 else "battery level unmeasured"
    headline_block = "\n".join(f"- {h}" for h in headlines) or "(no headlines fetched this time)"

    user_msg = (
        f"Location (via IP, approximate): {location}\n"
        f"Connected to: {ssid}\n"
        f"{battery_line}, up for {int(uptime_s) // 60} minutes\n\n"
        f"Headlines I saw:\n{headline_block}\n\n"
        "Write today's journal entry."
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if not text:
            return jsonify({"ok": False, "error": "empty response from model"}), 502
        return jsonify({"ok": True, "text": text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/", methods=["GET"])
def index():
    return jsonify({"ok": True, "service": "flipee_relay", "endpoint": "POST /reflect"})


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("WARNING: ANTHROPIC_API_KEY is not set in the environment. "
              "/reflect will fail until it is.")
    if not RELAY_KEY:
        print("WARNING: FLIPEE_RELAY_KEY is not set. /reflect will refuse all "
              "requests (503) until it is — see the module docstring.")
    print(f"flipee_relay: dev server on 0.0.0.0:{PORT} "
          "(use gunicorn + a reverse proxy for anything internet-facing)")
    app.run(host="0.0.0.0", port=PORT)

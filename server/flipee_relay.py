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

It also keeps every reflection it writes (see store.py) and serves a small
web dashboard for reading them back — otherwise they only exist on a
microSD card that has to be physically pulled, and on a build with no SD
wired up they don't exist anywhere at all.

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
  - Set FLIPEE_DASHBOARD_KEY to a *different* long random value — that's
    the password for the dashboard. The archive is a list of where this
    device has been and when, which is worth more protection than the
    reflections themselves suggest, so the dashboard also fails closed:
    with no key set it returns 503 instead of serving open. It's a
    separate secret from FLIPEE_RELAY_KEY on purpose, since that one is
    compiled into the sketch and travels with the hardware.
  - Set FLIPEE_DASHBOARD_HTTPS=1 once TLS is terminated in front of this,
    so the session cookie is marked Secure.
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
                proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            }
        }

  - Set Flipee.ino's RELAY_USE_TLS to true (it is by default) so it talks
    https:// to whatever SERVER_HOST resolves to.

SETUP (local / same-LAN testing)
---------------------------------
    pip install -r requirements.txt
    copy .env.example .env
    -- edit .env, set ANTHROPIC_API_KEY, FLIPEE_RELAY_KEY,
       FLIPEE_DASHBOARD_KEY --

    python flipee_relay.py

This binds to 0.0.0.0 over plain HTTP, fine for a quick test against a
device on the same trusted LAN. Set Flipee.ino's RELAY_USE_TLS to false
to match, since there's no TLS listener here without the nginx setup
above. The dashboard is then at http://<this machine>:5024/.

This is a local development server (Flask's built-in one). It's fine for
same-LAN testing but should not face the internet directly — see the
gunicorn/nginx setup above for that.

ROUTES
------
    POST /reflect   the device's endpoint; X-Flipee-Key header
    GET  /          dashboard (redirects to /login)
    GET  /entry/ID  one reflection, permalinkable
    GET  /entries   the same query as JSON, for scripting
    GET  /health    unauthenticated liveness check
"""

import hashlib
import hmac
import os
import threading
import time
from datetime import timedelta

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   session, url_for)
from dotenv import load_dotenv

import anthropic

import store

load_dotenv()

# ---------------------------------------------------------------------
# Config — change the model here if you'd rather use a different one
# (e.g. "claude-opus-5-5" for higher quality, "claude-haiku-4-5-20251001"
# for the fastest/cheapest replies).
# ---------------------------------------------------------------------
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 400
RELAY_KEY = os.environ.get("FLIPEE_RELAY_KEY", "")
DASHBOARD_KEY = os.environ.get("FLIPEE_DASHBOARD_KEY", "")
DASHBOARD_HTTPS = os.environ.get("FLIPEE_DASHBOARD_HTTPS", "").strip() in ("1", "true", "yes")
PORT = int(os.environ.get("FLIPEE_RELAY_PORT", "5024"))
PAGE_SIZE = 25
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

# Sessions are signed with a key derived from the dashboard password rather
# than a secret of their own: one less thing to configure, and changing the
# password automatically invalidates every existing session, which is what
# you'd want from a password change anyway. With no password set the
# dashboard is 503 everywhere, so a random throwaway key is fine.
app.secret_key = (
    hashlib.sha256(b"flipee-dashboard-session:" + DASHBOARD_KEY.encode()).digest()
    if DASHBOARD_KEY else os.urandom(32)
)
app.permanent_session_lifetime = timedelta(days=30)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Not unconditional: a Secure cookie is never sent over plain HTTP, so
    # defaulting this on would make login silently fail-to-loop during
    # same-LAN testing against this dev server. Turn it on with
    # FLIPEE_DASHBOARD_HTTPS=1 wherever there's TLS in front.
    SESSION_COOKIE_SECURE=DASHBOARD_HTTPS,
)

store.init()


# ---------------------------------------------------------------------
# Dashboard auth
# ---------------------------------------------------------------------
# A single shared password on the open internet is brute-forceable, so
# failed logins are throttled per client IP. This lives in process memory,
# which means each gunicorn worker throttles independently (-w 2 gives an
# attacker 2x the attempts) — the point is to make guessing slow, not to
# be an airtight lockout, and a long random key is what actually protects
# this.
_LOGIN_FAILS = {}
_LOGIN_LOCK = threading.Lock()
LOGIN_MAX_FAILS = 5
LOGIN_LOCKOUT_S = 60


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "?"


def login_locked_out(ip):
    with _LOGIN_LOCK:
        fails, until = _LOGIN_FAILS.get(ip, (0, 0.0))
        if until and time.time() < until:
            return int(until - time.time())
        if until and time.time() >= until:
            _LOGIN_FAILS.pop(ip, None)
    return 0


def note_login_failure(ip):
    with _LOGIN_LOCK:
        fails, until = _LOGIN_FAILS.get(ip, (0, 0.0))
        fails += 1
        if fails >= LOGIN_MAX_FAILS:
            _LOGIN_FAILS[ip] = (0, time.time() + LOGIN_LOCKOUT_S)
        else:
            _LOGIN_FAILS[ip] = (fails, 0.0)


def logged_in():
    return bool(DASHBOARD_KEY) and session.get("flipee_dash") is True


def require_dashboard():
    """Returns a response to send instead of the page, or None to proceed."""
    if not DASHBOARD_KEY:
        # Same fail-closed stance as /reflect: refuse rather than publish a
        # location history to whoever finds the host.
        return jsonify({
            "ok": False,
            "error": "FLIPEE_DASHBOARD_KEY is not configured on this server",
        }), 503
    if not logged_in():
        return redirect(url_for("login", next=request.full_path))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if not DASHBOARD_KEY:
        return jsonify({
            "ok": False,
            "error": "FLIPEE_DASHBOARD_KEY is not configured on this server",
        }), 503
    if logged_in():
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        ip = client_ip()
        wait = login_locked_out(ip)
        if wait:
            error = f"Too many attempts. Try again in {wait}s."
        elif hmac.compare_digest(request.form.get("key", ""), DASHBOARD_KEY):
            session.permanent = True
            session["flipee_dash"] = True
            target = request.args.get("next") or url_for("dashboard")
            # Only ever bounce to a path on this host — an open redirect
            # off a login page is a phishing primitive.
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("dashboard")
            return redirect(target)
        else:
            note_login_failure(ip)
            error = "Wrong password."
    return render_template("login.html", error=error), (200 if not error else 401)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------
def query_args():
    """The filter state shared by the HTML dashboard and the JSON API."""
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    return {
        "device_id": (request.args.get("device") or "").strip(),
        "day": (request.args.get("day") or "").strip(),
        "place": (request.args.get("q") or "").strip(),
        "page": page,
    }


@app.route("/", methods=["GET"])
def dashboard():
    blocked = require_dashboard()
    if blocked is not None:
        return blocked

    f = query_args()
    entries, total = store.query(
        device_id=f["device_id"] or None,
        day=f["day"] or None,
        place=f["place"] or None,
        limit=PAGE_SIZE,
        offset=(f["page"] - 1) * PAGE_SIZE,
    )
    return render_template(
        "dashboard.html",
        entries=entries,
        total=total,
        filters=f,
        page_size=PAGE_SIZE,
        devices=store.devices(),
        days=store.days(f["device_id"] or None),
        single=False,
    )


@app.route("/entry/<int:entry_id>", methods=["GET"])
def entry(entry_id):
    blocked = require_dashboard()
    if blocked is not None:
        return blocked
    found = store.get(entry_id)
    if not found:
        abort(404)
    return render_template(
        "dashboard.html",
        entries=[found],
        total=1,
        filters=query_args(),
        page_size=PAGE_SIZE,
        devices=[],
        days=[],
        single=True,
    )


@app.route("/entries", methods=["GET"])
def entries_json():
    """The dashboard's query as JSON — for scripting against the archive.

    Gated on the same session as the HTML, so a browser that's logged in
    can hit it directly and anything else has to log in first.
    """
    if not DASHBOARD_KEY:
        return jsonify({"ok": False, "error": "FLIPEE_DASHBOARD_KEY is not configured"}), 503
    if not logged_in():
        return jsonify({"ok": False, "error": "not logged in"}), 401
    f = query_args()
    entries, total = store.query(
        device_id=f["device_id"] or None,
        day=f["day"] or None,
        place=f["place"] or None,
        limit=PAGE_SIZE,
        offset=(f["page"] - 1) * PAGE_SIZE,
    )
    return jsonify({"ok": True, "total": total, "page": f["page"],
                    "page_size": PAGE_SIZE, "entries": entries})


# ---------------------------------------------------------------------
# The device's endpoint
# ---------------------------------------------------------------------
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
    # Sketches built before the archive existed don't send these, and a
    # device that can't identify itself still deserves to have its entries
    # kept — they just land under one shared placeholder id.
    device_id = (str(payload.get("device_id") or "").strip() or "unidentified-flipee")[:64]
    device_name = str(payload.get("device_name") or "").strip()[:64]

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
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Archiving is best-effort and deliberately outside the try above: a
    # full disk or a locked database shouldn't cost the device a reflection
    # it already paid an API call for. It gets the text either way and just
    # doesn't appear in the dashboard.
    entry_id = None
    try:
        entry_id = store.save(
            device_id=device_id,
            device_name=device_name,
            city=city, region=region, country=country,
            ssid=payload.get("ssid") or "",
            battery_pct=battery_pct if isinstance(battery_pct, int) and battery_pct >= 0 else None,
            uptime_s=int(uptime_s) if isinstance(uptime_s, int) else None,
            headlines=headlines,
            model=MODEL,
            text=text,
        )
    except Exception as e:
        app.logger.warning("could not archive reflection: %s", e)

    return jsonify({"ok": True, "text": text, "id": entry_id})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "service": "flipee_relay",
        "endpoint": "POST /reflect",
        "dashboard": "/" if DASHBOARD_KEY else "not configured (set FLIPEE_DASHBOARD_KEY)",
    })


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("WARNING: ANTHROPIC_API_KEY is not set in the environment. "
              "/reflect will fail until it is.")
    if not RELAY_KEY:
        print("WARNING: FLIPEE_RELAY_KEY is not set. /reflect will refuse all "
              "requests (503) until it is — see the module docstring.")
    if not DASHBOARD_KEY:
        print("WARNING: FLIPEE_DASHBOARD_KEY is not set. The dashboard will "
              "refuse all requests (503) until it is. Reflections are still "
              "archived, so nothing is lost by setting it later.")
    print(f"flipee_relay: dev server on 0.0.0.0:{PORT}, archive at {store.DB_PATH} "
          "(use gunicorn + a reverse proxy for anything internet-facing)")
    app.run(host="0.0.0.0", port=PORT)

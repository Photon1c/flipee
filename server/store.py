"""
store.py — the reflection archive behind flipee_relay.py's dashboard.

WHY THIS EXISTS
---------------
Reflections used to be write-only from a human's point of view: Flipee
saved them to a microSD card that had to be physically pulled to read,
and on a build with no SD wired up (the default) they weren't persisted
anywhere at all. The relay already receives the full context of every
reflection it writes, so it's the natural place to keep them.

Plain sqlite3 from the standard library — no new dependency, and a single
file you can scp off the VPS or open with any SQLite browser. The write
volume here is a handful of rows a day from one small device; nothing
about this wants a real database.

EVERY ROW IS KEYED BY DEVICE
----------------------------
`device_id` is required (the relay substitutes a placeholder rather than
leaving it empty) even though there's probably only one Flipee today.
Retrofitting a device column onto an archive that already has history in
it means either backfilling a guess or losing the ability to tell two
devices apart for everything written before the change.

CONNECTIONS
-----------
Every call opens and closes its own connection. SQLite connections can't
be shared across threads, and the relay runs under threaded Flask locally
and multi-worker gunicorn on a VPS — a module-level connection would be
a race in both. Connection setup is microseconds against a local file.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

# Default next to this file so `python flipee_relay.py` in a fresh clone
# just works. Point FLIPEE_DB_PATH somewhere persistent on a VPS if the
# checkout lives anywhere that gets blown away on redeploy.
DB_PATH = os.environ.get("FLIPEE_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "flipee.sqlite3"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reflections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT    NOT NULL,
    device_name TEXT    NOT NULL DEFAULT '',
    created_utc TEXT    NOT NULL,          -- ISO8601, e.g. 2026-09-26T14:03:11Z
    city        TEXT    NOT NULL DEFAULT '',
    region      TEXT    NOT NULL DEFAULT '',
    country     TEXT    NOT NULL DEFAULT '',
    ssid        TEXT    NOT NULL DEFAULT '',
    battery_pct INTEGER,                   -- NULL when the build has no ADC
    uptime_s    INTEGER,
    headlines   TEXT    NOT NULL DEFAULT '[]',  -- JSON array of strings
    model       TEXT    NOT NULL DEFAULT '',
    text        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reflections_created
    ON reflections(created_utc DESC);
CREATE INDEX IF NOT EXISTS idx_reflections_device
    ON reflections(device_id, created_utc DESC);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    """Create the schema if it isn't there. Safe to call on every start."""
    with connect() as conn:
        conn.executescript(SCHEMA)


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save(device_id, device_name, city, region, country, ssid,
         battery_pct, uptime_s, headlines, model, text, created_utc=None):
    """Store one reflection; returns its row id.

    The timestamp is the relay's own UTC clock rather than anything the
    device sends. Flipee only has a clock once it has joined a network and
    hit NTP, and it doesn't send one — but the relay can't have received
    the request before it existed, so server time is both available and
    honest.
    """
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO reflections
                 (device_id, device_name, created_utc, city, region, country,
                  ssid, battery_pct, uptime_s, headlines, model, text)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (device_id, device_name, created_utc or utc_now_iso(), city, region,
             country, ssid, battery_pct, uptime_s,
             json.dumps(list(headlines or [])), model, text),
        )
        return cur.lastrowid


def _row_to_entry(row):
    entry = dict(row)
    try:
        entry["headlines"] = json.loads(entry.get("headlines") or "[]")
    except (ValueError, TypeError):
        entry["headlines"] = []
    entry["location"] = ", ".join(
        p for p in (entry["city"], entry["region"], entry["country"]) if p
    ) or "unknown"
    entry["day"] = (entry["created_utc"] or "")[:10]
    return entry


def query(device_id=None, day=None, place=None, limit=25, offset=0):
    """Newest first, filtered. Returns (entries, total_matching).

    `place` is a free-text match across city/region/country/ssid — the
    dashboard's one search box, since "where was it" is sometimes a city
    and sometimes the name of the cafe's WiFi.
    """
    where, params = [], []
    if device_id:
        where.append("device_id = ?")
        params.append(device_id)
    if day:
        # created_utc is a fixed-width ISO string, so a prefix match is an
        # exact day filter and still uses the index.
        where.append("created_utc LIKE ?")
        params.append(day + "%")
    if place:
        where.append("(city LIKE ? OR region LIKE ? OR country LIKE ? OR ssid LIKE ?)")
        params.extend(["%" + place + "%"] * 4)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    with connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM reflections" + clause, params
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM reflections" + clause +
            " ORDER BY created_utc DESC, id DESC LIMIT ? OFFSET ?",
            params + [int(limit), int(offset)],
        ).fetchall()
    return [_row_to_entry(r) for r in rows], total


def get(entry_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM reflections WHERE id = ?", (entry_id,)
        ).fetchone()
    return _row_to_entry(row) if row else None


def devices():
    """Every device that's ever reported, for the dashboard's filter."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT device_id,
                      MAX(device_name)   AS device_name,
                      COUNT(*)           AS entries,
                      MAX(created_utc)   AS last_seen
                 FROM reflections
                GROUP BY device_id
                ORDER BY last_seen DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def days(device_id=None, limit=180):
    """Distinct days that have entries, newest first."""
    params = []
    clause = ""
    if device_id:
        clause = " WHERE device_id = ?"
        params.append(device_id)
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT substr(created_utc, 1, 10) AS day FROM reflections"
            + clause + " ORDER BY day DESC LIMIT ?",
            params + [int(limit)],
        ).fetchall()
    return [r["day"] for r in rows]

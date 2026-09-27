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
    place_key   TEXT    NOT NULL DEFAULT '',  -- normalized city|region|country
    ssid        TEXT    NOT NULL DEFAULT '',
    battery_pct INTEGER,                   -- NULL when the build has no ADC
    uptime_s    INTEGER,
    local_time  TEXT    NOT NULL DEFAULT '',  -- the device's own wall clock
    tz_offset_s INTEGER,
    lat         REAL,
    lon         REAL,
    headlines   TEXT    NOT NULL DEFAULT '[]',  -- JSON array of strings
    model       TEXT    NOT NULL DEFAULT '',
    text        TEXT    NOT NULL
);
-- What Flipee has worked out and wants to carry forward, distilled after
-- each entry (see memory.py). One row per device per place, plus a row
-- with place_key='*' for what it carries everywhere.
CREATE TABLE IF NOT EXISTS device_memory (
    device_id   TEXT NOT NULL,
    place_key   TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    threads     TEXT NOT NULL DEFAULT '[]',  -- JSON array: questions left open
    covered     TEXT NOT NULL DEFAULT '[]',  -- JSON array: angles already written
    updated_utc TEXT NOT NULL,
    PRIMARY KEY (device_id, place_key)
);

-- Background on a place, fetched once from Wikipedia and kept forever.
-- Shared across devices: a fact about Federal Way is a fact about Federal
-- Way regardless of which Flipee is standing in it.
CREATE TABLE IF NOT EXISTS place_facts (
    place_key   TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 1,  -- 0 = lookup failed; retried later
    fetched_utc TEXT NOT NULL
);
"""

# Indexes live apart from the tables because they're created *after* the
# column migration below: an index on place_key can't be built until the
# ALTER that adds place_key to a pre-existing archive has run.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_reflections_created
    ON reflections(created_utc DESC);
CREATE INDEX IF NOT EXISTS idx_reflections_device
    ON reflections(device_id, created_utc DESC);
CREATE INDEX IF NOT EXISTS idx_reflections_place
    ON reflections(device_id, place_key, created_utc DESC);
"""

# Columns added after the first release. SQLite can't add them idempotently
# in SCHEMA, so init() checks and ALTERs — the archive already has history
# in it by now and dropping it to get a new column would be absurd.
_ADDED_COLUMNS = (
    ("place_key", "TEXT NOT NULL DEFAULT ''"),
    ("local_time", "TEXT NOT NULL DEFAULT ''"),
    ("tz_offset_s", "INTEGER"),
    ("lat", "REAL"),
    ("lon", "REAL"),
)


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def place_key(city, region, country):
    """Normalized identity for a place, so 'Federal Way' and 'federal way'
    are the same town and a re-geolocation isn't mistaken for travel."""
    return "|".join((p or "").strip().lower().replace("|", " ")
                    for p in (city, region, country))


def init():
    """Create the schema if it isn't there, and migrate. Safe every start."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(reflections)")}
        added = []
        for name, ddl in _ADDED_COLUMNS:
            if name not in existing:
                conn.execute("ALTER TABLE reflections ADD COLUMN %s %s" % (name, ddl))
                added.append(name)
        if "place_key" in added:
            # Backfill from the city/region/country already on every row, so
            # pre-migration history still counts toward "how long have I
            # been here" instead of looking like one long absence.
            conn.execute(
                "UPDATE reflections SET place_key = "
                "lower(trim(city)) || '|' || lower(trim(region)) || '|' || lower(trim(country))"
            )
        conn.executescript(INDEXES)
        return added


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save(device_id, device_name, city, region, country, ssid,
         battery_pct, uptime_s, headlines, model, text, created_utc=None,
         local_time="", tz_offset_s=None, lat=None, lon=None):
    """Store one reflection; returns its row id.

    `created_utc` is the relay's own clock rather than anything the device
    sends — the relay can't have received the request before it existed,
    so server time is both available and honest. `local_time` is separate
    and is the device's wall clock, which is the one that knows whether
    it's morning where Flipee actually is.
    """
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO reflections
                 (device_id, device_name, created_utc, city, region, country,
                  place_key, ssid, battery_pct, uptime_s, local_time,
                  tz_offset_s, lat, lon, headlines, model, text)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (device_id, device_name, created_utc or utc_now_iso(), city, region,
             country, place_key(city, region, country), ssid, battery_pct,
             uptime_s, local_time, tz_offset_s, lat, lon,
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


# =====================================================================
# Continuity — what one device knows about itself and where it is
# =====================================================================

def _parse_utc(stamp):
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def recent_entries(device_id, limit=4, pk=None):
    """This device's last few entries, newest first — the raw material for
    picking up a thread instead of starting over."""
    where, params = ["device_id = ?"], [device_id]
    if pk:
        where.append("place_key = ?")
        params.append(pk)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reflections WHERE " + " AND ".join(where) +
            " ORDER BY created_utc DESC, id DESC LIMIT ?", params + [int(limit)]
        ).fetchall()
    return [_row_to_entry(r) for r in rows]


def context_state(device_id, city, region, country, ssid):
    """Everything derivable about where this device stands in its own history.

    This is the answer to "why does every entry read like a first
    impression": nothing ever told it that it had been here before, or
    that it wrote four minutes ago. All of it is a few indexed counts.
    """
    pk = place_key(city, region, country)
    now = datetime.now(timezone.utc)
    state = {"place_key": pk}

    with connect() as conn:
        state["total_entries"] = conn.execute(
            "SELECT COUNT(*) FROM reflections WHERE device_id = ?", (device_id,)
        ).fetchone()[0]

        last = conn.execute(
            "SELECT * FROM reflections WHERE device_id = ? "
            "ORDER BY created_utc DESC, id DESC LIMIT 1", (device_id,)
        ).fetchone()

        here = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_utc) AS first_seen "
            "FROM reflections WHERE device_id = ? AND place_key = ?",
            (device_id, pk),
        ).fetchone()

        state["places_seen"] = conn.execute(
            "SELECT COUNT(DISTINCT place_key) FROM reflections WHERE device_id = ?",
            (device_id,),
        ).fetchone()[0]

        state["networks_here"] = [
            r["ssid"] for r in conn.execute(
                "SELECT DISTINCT ssid FROM reflections "
                "WHERE device_id = ? AND place_key = ? AND ssid <> '' LIMIT 8",
                (device_id, pk),
            ).fetchall()
        ]

        # Places other than this one, most recent first — "I was in Lisbon
        # two days ago" is the kind of thing a travel diary should know.
        state["other_places"] = [
            dict(r) for r in conn.execute(
                "SELECT city, region, country, COUNT(*) AS n, MAX(created_utc) AS last_seen "
                "FROM reflections WHERE device_id = ? AND place_key <> ? "
                "GROUP BY place_key ORDER BY last_seen DESC LIMIT 4",
                (device_id, pk),
            ).fetchall()
        ]

    state["entries_here"] = here["n"] if here else 0
    state["first_seen_here"] = here["first_seen"] if here else None
    state["is_first_entry"] = state["total_entries"] == 0
    state["is_new_place"] = state["entries_here"] == 0
    state["is_new_network"] = bool(ssid) and ssid not in state["networks_here"]

    if last:
        prev = _parse_utc(last["created_utc"])
        state["last_entry"] = _row_to_entry(last)
        state["minutes_since_last"] = (
            int((now - prev).total_seconds() // 60) if prev else None
        )
        state["moved_since_last"] = last["place_key"] != pk
    else:
        state["last_entry"] = None
        state["minutes_since_last"] = None
        state["moved_since_last"] = False

    if state["first_seen_here"]:
        first = _parse_utc(state["first_seen_here"])
        state["hours_here"] = (
            round((now - first).total_seconds() / 3600, 1) if first else None
        )
    else:
        state["hours_here"] = None
    return state


def covered_headlines(device_id, pk, limit=8):
    """Headlines this device has already had in front of it recently, so a
    fresh fetch can be split into 'new' and 'you've seen this'."""
    seen = set()
    for entry in recent_entries(device_id, limit=limit, pk=pk):
        seen.update(entry["headlines"])
    return seen


# =====================================================================
# Distilled memory (written by memory.py after each entry)
# =====================================================================

GLOBAL_KEY = "*"  # the row for what Flipee carries between places


def get_memory(device_id, pk):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM device_memory WHERE device_id = ? AND place_key = ?",
            (device_id, pk),
        ).fetchone()
    if not row:
        return {"notes": "", "threads": [], "covered": [], "updated_utc": None}
    out = dict(row)
    for field in ("threads", "covered"):
        try:
            out[field] = json.loads(out[field] or "[]")
        except (ValueError, TypeError):
            out[field] = []
    return out


def save_memory(device_id, pk, notes, threads, covered):
    with connect() as conn:
        conn.execute(
            """INSERT INTO device_memory
                 (device_id, place_key, notes, threads, covered, updated_utc)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(device_id, place_key) DO UPDATE SET
                 notes = excluded.notes, threads = excluded.threads,
                 covered = excluded.covered, updated_utc = excluded.updated_utc""",
            (device_id, pk, notes or "", json.dumps(list(threads or [])),
             json.dumps(list(covered or [])), utc_now_iso()),
        )


# =====================================================================
# Place background (written by place.py)
# =====================================================================

def get_place_facts(pk):
    with connect() as conn:
        row = conn.execute("SELECT * FROM place_facts WHERE place_key = ?", (pk,)).fetchone()
    return dict(row) if row else None


def save_place_facts(pk, title, summary, source, ok=True):
    with connect() as conn:
        conn.execute(
            """INSERT INTO place_facts (place_key, title, summary, source, ok, fetched_utc)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(place_key) DO UPDATE SET
                 title = excluded.title, summary = excluded.summary,
                 source = excluded.source, ok = excluded.ok,
                 fetched_utc = excluded.fetched_utc""",
            (pk, title or "", summary or "", source or "", 1 if ok else 0, utc_now_iso()),
        )

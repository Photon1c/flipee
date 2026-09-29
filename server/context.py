"""
context.py — what Flipee knows at the moment someone flips it.

THE PROBLEM THIS SOLVES
-----------------------
The relay used to send one request's worth of facts and nothing else, so
every entry was a first impression. Twelve consecutive entries from the
same desk all opened "Woke up in Federal Way", all picked the same two
headlines out of the same cached feed, all announced plans to move on
from a network the device never left, and two of them written ten
seconds apart both discovered the town from scratch.

None of that is a model failing. Nothing in the prompt said: you have
been here seven hours, you wrote four minutes ago, you already used the
apartment fire, it's afternoon, and you are not going anywhere.

So this module assembles a briefing before each entry:

    STATE          where it stands in its own history, from SQL counts
    PLACE          Wikipedia background, fetched once per city (place.py)
    MEMORY         what it distilled from earlier entries (memory.py)
    THREADS        questions it left open and could return to
    COVERED        angles already used, so it stops relitigating them
    RECENT         its own last entries verbatim, for voice and callbacks
    HEADLINES      split into genuinely new vs already seen

Everything here is cheap: indexed counts, one cached HTTP lookup, and
text the relay already had.
"""

import re

import store
import place

# A label line from the briefing — "[2026-09-27T11:56:32Z, from Federal
# Way, Washington, United States]" — reproduced at the top of an entry.
_LABEL_LINE = re.compile(r"^\s*\[[^\]\n]{0,120}\]\s*$")


def clean_entry_text(text):
    """Strip briefing formatting the model copied into its own entry.

    The recent-entries block labels each sample with a bracketed
    timestamp so the model can tell them apart. At least once it
    reproduced that label as the first line of a new entry, and the
    archive kept it verbatim — an entry that opens with someone else's
    timestamp. Telling the model not to do it is the real fix; this is
    the cheap guard for when it does anyway.
    """
    lines = (text or "").split("\n")
    while lines and (not lines[0].strip() or _LABEL_LINE.match(lines[0])):
        lines.pop(0)
    return "\n".join(lines).strip()

# How much history goes in the prompt. Two full entries is enough to hold
# a voice and a thread without the model simply paraphrasing yesterday —
# and it keeps the request inside the device's 15s read timeout.
RECENT_ENTRIES = 2
RECENT_CHARS = 900
# "A quick second look" rather than a new sitting. Below this many minutes
# the entry should continue the last one instead of re-establishing where
# it is; rapid flips are how this thing actually gets used.
SAME_SITTING_MINUTES = 45
# How many already-read headlines to spell out before summarizing the rest
# as a count.
STALE_SHOWN = 3


def _describe_gap(state):
    """The one line that most changes how an entry should open."""
    if state["is_first_entry"]:
        return ("This is your first entry ever. You have no past to refer back "
                "to — say so if it's interesting, but don't invent a history.")

    mins = state["minutes_since_last"]
    last = state["last_entry"]
    when = "just now" if mins is None else _ago(mins)

    if state["moved_since_last"]:
        return ("You have MOVED since your last entry %s, which you wrote in %s. "
                "This is an arrival — the one time it's right to introduce a place."
                % (when, last["location"]))
    if mins is not None and mins <= SAME_SITTING_MINUTES:
        return ("You wrote your last entry %s, from this same place and network. "
                "This is the same sitting, not a new arrival: continue the thought, "
                "go deeper, or pick up a thread you left open. Do NOT re-introduce "
                "where you are." % when)
    return ("Your last entry was %s, from this same place. You haven't moved. "
            "Pick up where you left off rather than arriving again." % when)


def _ago(minutes):
    if minutes < 1:
        return "less than a minute ago"
    if minutes < 60:
        return "%d minutes ago" % minutes
    hours = minutes / 60.0
    if hours < 24:
        return "%.1f hours ago" % hours
    return "%.1f days ago" % (hours / 24.0)


def _state_lines(state, ssid, local_time, battery_pct, uptime_s, lat, lon):
    lines = ["- " + _describe_gap(state)]
    lines.append("- Entries you have written in total: %d (this will be #%d)."
                 % (state["total_entries"], state["total_entries"] + 1))

    if state["is_new_place"]:
        lines.append("- You have never been here before.")
    else:
        here = "- You have written %d entr%s from this place" % (
            state["entries_here"], "y" if state["entries_here"] == 1 else "ies")
        if state["hours_here"] is not None:
            here += ", first arriving %s" % _ago(state["hours_here"] * 60)
        lines.append(here + ".")

    if ssid:
        lines.append("- Network: %s%s." % (
            ssid, " — new to you" if state["is_new_network"] else
            " — one you've used here before"))
    if state["other_places"]:
        others = "; ".join(
            "%s (%d, last %s)" % (
                ", ".join(p for p in (o["city"], o["country"]) if p),
                o["n"], (o["last_seen"] or "")[:10])
            for o in state["other_places"])
        lines.append("- Other places you've woken up: %s." % others)
    elif not state["is_new_place"]:
        lines.append("- This is the only place you have ever written from. "
                     "You are not a seasoned traveller yet; don't pretend to be.")

    if local_time:
        lines.append("- Local time where you are: %s. Use this — don't guess "
                     "at the time of day." % local_time)
    else:
        lines.append("- Your clock hasn't synced, so you genuinely don't know "
                     "what time it is. Don't assert a time of day.")

    if isinstance(battery_pct, int) and battery_pct >= 0:
        lines.append("- Battery: %d%%." % battery_pct)
    else:
        lines.append("- This build has NO battery sensor. You cannot tell how "
                     "charged you are. Don't claim a level, and don't make "
                     "running low a recurring bit.")

    if uptime_s:
        lines.append("- Awake for %d minutes this stretch." % (int(uptime_s) // 60))
    if lat or lon:
        lines.append("- Coordinates behind that city name: %.3f, %.3f." % (lat or 0, lon or 0))
    return lines


def build(device_id, **kwargs):
    """The user message for one reflection, plus the state dict (which the
    caller reuses for the memory update)."""
    blocks, state = build_blocks(device_id, **kwargs)
    return "\n\n".join(blocks), state


def build_blocks(device_id, city, region, country, ssid, battery_pct, uptime_s,
                 headlines, local_time="", lat=None, lon=None):
    """The briefing as its separate sections.

    Kept separate from build() so the sections can be counted individually —
    knowing the briefing costs ~1.2k tokens is less useful than knowing
    which part of it does.
    """
    state = store.context_state(device_id, city, region, country, ssid)
    pk = state["place_key"]
    location = ", ".join(p for p in (city, region, country) if p) or "somewhere unnamed"

    place_mem = store.get_memory(device_id, pk)
    global_mem = store.get_memory(device_id, store.GLOBAL_KEY)
    facts = place.facts_for(city, region, country)

    seen = store.covered_headlines(device_id, pk)
    fresh = [h for h in headlines if h not in seen]
    stale = [h for h in headlines if h in seen]
    # The caller uses this to decide whether distilling memory again is
    # worth a second model call.
    state["new_headlines"] = len(fresh)

    blocks = ["WHERE YOU ARE\n%s, on %s." % (location, ssid or "an open network")]
    blocks.append("YOUR STATE\n" + "\n".join(
        _state_lines(state, ssid, local_time, battery_pct, uptime_s, lat, lon)))

    if facts and facts["summary"]:
        blocks.append("BACKGROUND ON THIS PLACE (you looked it up)\n%s" % facts["summary"])

    if global_mem["notes"]:
        blocks.append("WHAT YOU CARRY WITH YOU (your own notes)\n%s" % global_mem["notes"])
    if place_mem["notes"]:
        blocks.append("WHAT YOU'VE WORKED OUT ABOUT THIS PLACE (your own notes)\n%s"
                      % place_mem["notes"])
    if place_mem["threads"]:
        blocks.append("THREADS YOU LEFT OPEN HERE\n" +
                      "\n".join("- %s" % t for t in place_mem["threads"]) +
                      "\nPicking one of these up is usually better than starting "
                      "something new.")
    if place_mem["covered"]:
        blocks.append("ANGLES YOU HAVE ALREADY WRITTEN HERE — do not repeat these\n" +
                      "\n".join("- %s" % c for c in place_mem["covered"]))

    # Verbatim entries are the single most expensive block in the briefing
    # (479 of 1961 tokens, measured). They're here to stop the model echoing
    # its own phrasing — and once distilled notes exist, those already carry
    # what the older entry *said*, so one sample is enough to carry how it
    # sounded. Falls back to two when there's no memory yet.
    depth = 1 if place_mem["notes"] else RECENT_ENTRIES
    recent = store.recent_entries(device_id, limit=depth)
    if recent:
        parts = []
        for entry in recent:
            text = entry["text"]
            if len(text) > RECENT_CHARS:
                text = text[:RECENT_CHARS].rsplit(" ", 1)[0] + "..."
            parts.append("[%s, from %s]\n%s" % (
                entry["created_utc"], entry["location"], text))
        blocks.append("YOUR LAST ENTRIES, VERBATIM\n" + "\n\n".join(parts) +
                      "\n\nThe bracketed lines are labels so you can tell them "
                      "apart — they are not part of an entry and must never "
                      "appear in what you write. Don't echo their openings, "
                      "their structure, or their closing moves either. You "
                      "already made those.")

    if fresh:
        blocks.append("HEADLINES THAT ARE NEW SINCE YOU LAST LOOKED\n" +
                      "\n".join("- %s" % h for h in fresh))
    if stale:
        # Listing all eight costs ~200 tokens to show the model things it's
        # being told to treat as background. A few for recall plus a count
        # does the same job.
        shown = stale[:STALE_SHOWN]
        more = len(stale) - len(shown)
        blocks.append(("HEADLINES YOU HAVE ALREADY SEEN%s\n" % (
            " (the feed hasn't changed much)" if not fresh else "")) +
            "\n".join("- %s" % h for h in shown) +
            ("\n- ...and %d more you've already read" % more if more else ""))
    if not headlines:
        blocks.append("HEADLINES\n(nothing fetched this time — say so if it matters)")

    if fresh:
        ask = ("Write the next entry. Lead with what's actually new to you.")
    elif headlines:
        ask = ("Write the next entry. Nothing in the feed has changed since you "
               "last looked, so don't re-report it — notice something else: the "
               "place itself, the network, the passing of the day, a thread you "
               "left open, or what it's like to keep reading the same news.")
    else:
        ask = "Write the next entry."
    blocks.append(ask)

    return blocks, state

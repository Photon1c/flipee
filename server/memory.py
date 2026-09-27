"""
memory.py — what Flipee keeps after an entry is written.

Feeding the last two entries into the next prompt buys continuity for
about an hour. It doesn't survive the window: once an entry scrolls out,
whatever Flipee worked out in it is gone, and it starts rediscovering
the same things. That's the difference between a longer prompt and
actual state.

So after each reflection, a second cheap model call distills what's
worth keeping into three things:

    notes    durable observations about a place, and about itself
    threads  questions it left hanging, to pick up next time
    covered  angles already used here, so it stops relitigating them

Two rows per device: one for the place it's in, one (place_key '*') for
what it carries everywhere.

THIS RUNS AFTER THE DEVICE HAS ITS REPLY
----------------------------------------
On a background thread, deliberately. Flipee waits at most 15s for the
relay before falling back to a templated reflection, and that budget
belongs to the entry the user is standing there waiting to read — not to
bookkeeping. If the update fails, the next entry is slightly less
informed and nothing else breaks.
"""

import json
import threading
from datetime import datetime, timedelta, timezone

import store

MAX_TOKENS = 700
# Don't distill again this soon unless the feed actually moved. Measured,
# the distillation call is ~1.2k input tokens — about 38% of a flip's
# total — and three flips in ten seconds (which is how this device really
# gets used) produce three near-identical rewrites of the same notes. The
# entry still gets archived; only the bookkeeping is skipped.
MIN_GAP_MINUTES = 20
# Small enough that the model must actually choose what matters. An
# unbounded memory becomes a transcript, which is what we already have.
NOTE_LIMITS = "about 100 words for place notes, about 60 for carried notes"
MAX_THREADS = 4
MAX_COVERED = 14

SYSTEM = (
    "You maintain the private working memory of Flipee, a small roaming "
    "device that keeps a journal. You are not writing journal entries — you "
    "are writing the notes Flipee will read before it writes the next one. "
    "Be concrete and specific: names, numbers, recurring details, things it "
    "noticed and might notice again. Terse and factual beats evocative here. "
    "Never invent anything that isn't in what you were given. "
    "Reply with a single JSON object and nothing else."
)

# One update at a time per device: two quick flips would otherwise race and
# the loser's observations would vanish under last-write-wins.
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(device_id):
    with _LOCKS_GUARD:
        if device_id not in _LOCKS:
            _LOCKS[device_id] = threading.Lock()
        return _LOCKS[device_id]


def should_update(device_id, pk, state):
    """Whether this entry is worth a distillation call.

    Yes if anything genuinely new came through the feed, or if it's been
    long enough that the notes have drifted. No for the burst of flips a
    few seconds apart that all saw the same headlines — those would spend
    a full call to rewrite the same paragraph.
    """
    if state.get("new_headlines"):
        return True
    last = store._parse_utc(store.get_memory(device_id, pk)["updated_utc"] or "")
    if last is None:
        return True  # never distilled here
    return datetime.now(timezone.utc) - last >= timedelta(minutes=MIN_GAP_MINUTES)


def _parse_json(text):
    """Tolerate a stray ```json fence or a sentence of preamble."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _prompt(location, entry_text, headlines, place_mem, global_mem):
    parts = []
    if place_mem["notes"]:
        parts.append("YOUR CURRENT NOTES ON %s:\n%s" % (location.upper(), place_mem["notes"]))
    if place_mem["threads"]:
        parts.append("OPEN THREADS:\n" + "\n".join("- %s" % t for t in place_mem["threads"]))
    if place_mem["covered"]:
        parts.append("ANGLES ALREADY COVERED HERE:\n" +
                     "\n".join("- %s" % c for c in place_mem["covered"]))
    if global_mem["notes"]:
        parts.append("YOUR CURRENT CARRIED NOTES (true anywhere):\n%s" % global_mem["notes"])
    if headlines:
        parts.append("HEADLINES IT WAS LOOKING AT:\n" +
                     "\n".join("- %s" % h for h in headlines))
    parts.append("THE ENTRY IT JUST WROTE, IN %s:\n%s" % (location.upper(), entry_text))
    parts.append(
        "Update the memory. Merge new observations into the existing notes "
        "rather than replacing them, drop what no longer earns its place, and "
        "keep it to %s.\n\n"
        "Return JSON with exactly these keys:\n"
        '  "place_notes"  — what Flipee now knows about %s: the texture of the '
        'place, what keeps coming up, what it has decided it thinks.\n'
        '  "carried_notes" — what is true about Flipee itself regardless of '
        'where it is: its habits, its running preoccupations, how it has been '
        'feeling about drifting. Omit anything place-specific.\n'
        '  "threads" — up to %d short open questions it could return to, '
        'phrased so a later entry could answer or revisit them. [] if none.\n'
        '  "covered" — up to %d short labels for angles it has now used here '
        '(e.g. "the Pierce County windstorm", "naming of the Homenode router"), '
        "so it doesn't write them again. Keep the most recent and distinctive."
        % (NOTE_LIMITS, location, MAX_THREADS, MAX_COVERED)
    )
    return "\n\n".join(parts)


def update(client, model, device_id, pk, location, entry_text, headlines, logger=None):
    """Distill and persist. Returns True if memory was written."""
    lock = _lock_for(device_id)
    with lock:
        try:
            place_mem = store.get_memory(device_id, pk)
            global_mem = store.get_memory(device_id, store.GLOBAL_KEY)
            response = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM,
                messages=[{"role": "user", "content": _prompt(
                    location, entry_text, headlines, place_mem, global_mem)}],
            )
            raw = "".join(b.text for b in response.content if b.type == "text")
            parsed = _parse_json(raw)
            if not parsed:
                if logger:
                    logger.warning("memory update: could not parse a JSON object "
                                   "from the model's reply")
                return False

            threads = [str(t).strip() for t in (parsed.get("threads") or []) if str(t).strip()]
            covered = [str(c).strip() for c in (parsed.get("covered") or []) if str(c).strip()]
            store.save_memory(
                device_id, pk,
                notes=str(parsed.get("place_notes") or place_mem["notes"]),
                threads=threads[:MAX_THREADS],
                covered=covered[:MAX_COVERED],
            )
            carried = str(parsed.get("carried_notes") or "").strip()
            if carried:
                # The global row keeps notes only; threads and covered angles
                # are meaningful per-place and would blur together here.
                store.save_memory(device_id, store.GLOBAL_KEY, notes=carried,
                                  threads=[], covered=[])
            return True
        except Exception as e:
            if logger:
                logger.warning("memory update failed: %s", e)
            return False


def update_async(client, model, device_id, pk, location, entry_text, headlines, logger=None):
    """Fire-and-forget so the device never waits on bookkeeping."""
    thread = threading.Thread(
        target=update,
        args=(client, model, device_id, pk, location, entry_text, headlines, logger),
        name="flipee-memory",
        daemon=True,
    )
    thread.start()
    return thread

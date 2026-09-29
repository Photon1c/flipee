"""
review.py — flags the sentences in Flipee's memory that a human should check.

THE FAILURE THIS CATCHES
------------------------
An entry once inferred that Tacoma is north of Federal Way. It's south.
The distiller filed it as a fact about the place, and from then on it was
in every briefing — stated as something Flipee knew, with nothing
anywhere to contradict it. Memory makes good observations durable and
makes wrong ones durable in exactly the same way.

So this marks claims that aren't traceable to anything Flipee actually
read. Three kinds, chosen because they're the ones that are both
checkable and wrong-able:

  spatial      "north of", "25 miles from", "just above" — relational
               geography the model is happy to infer and bad at.
  quantity     a number that appears in no headline and no place summary.
  unsourced    a proper noun no source mentions, i.e. a name it supplied
               rather than read.

Everything else is left alone. "Twelve entries in one place feels like
commitment or being stuck" is not a claim about the world and shouldn't
be dressed up as one — flagging introspection would train you to ignore
the highlights, which is worse than not having them.

NO MODEL CALL
-------------
This is string matching against sources already in the database. A
review pass that cost tokens would be a strange way to economize on a
memory system built to save them.
"""

import re

import store

# No "above"/"below"/"beyond": they're spatial about as often as they're
# metaphorical, and "what happens below the noise threshold" getting
# flagged as geography is exactly the kind of false positive that teaches
# you to stop reading the highlights.
DIRECTION_WORDS = {
    "north", "south", "east", "west", "northern", "southern", "eastern",
    "western", "northeast", "northwest", "southeast", "southwest",
    "upstream", "downstream", "inland", "coastal", "neighbor", "neighbour",
    "neighboring", "neighbouring", "adjacent", "borders", "bordering",
    "nearby",
}
DISTANCE_WORDS = {"mile", "miles", "km", "kilometre", "kilometres",
                  "kilometer", "kilometers", "block", "blocks"}

# Capitalized words that start sentences or are too generic to treat as a
# name the model must have read somewhere.
COMMON = {
    "the", "a", "an", "it", "its", "this", "that", "these", "those", "i",
    "flipee", "homenode", "wifi", "the", "there", "here", "both", "and",
    "but", "pattern", "geography", "location", "entries", "entry", "note",
    "notes", "device", "still", "yesterday", "today", "tomorrow", "morning",
    "afternoon", "evening", "night", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "january", "february",
    "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december",
    # Clock suffixes: the time itself is stripped before the number check,
    # which leaves "PM" behind looking like a proper noun.
    "am", "pm", "a.m", "p.m",
}

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_NUMBER = re.compile(r"\d[\d,.]*")
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?", re.I)

# Spelled-out numbers, because a fabricated quantity doesn't announce
# itself with digits. The entry that claimed "I've been here nineteen
# days" — when its briefing said 2.3 — sailed past a digits-only check.
_UNITS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
# "hundred"/"thousand"/"dozen" are counted as quantity words but not given
# a value — "two hundred" isn't 2+100, and guessing wrong would be worse
# than not guessing.
_MAGNITUDES = {"hundred", "thousand", "million", "dozen"}
# Deliberately excluded: "one". It's a pronoun and an article far more
# often than a count ("one of the", "no one", "the one that stuck"), and
# flagging every instance would bury the real finds.
_NUMBER_WORDS = set(_UNITS) | set(_TENS) | _MAGNITUDES


def _word_number(token):
    """(value, True) for a spelled-out number, (None, False) if it isn't one.

    Handles 'nineteen' and 'twenty-five'; returns no value for magnitudes,
    which still count as quantities worth checking.
    """
    parts = [p for p in token.lower().split("-") if p]
    if not parts or not all(p in _NUMBER_WORDS for p in parts):
        return None, False
    if any(p in _MAGNITUDES for p in parts):
        return None, True
    total = 0
    for p in parts:
        total += _UNITS.get(p, 0) or _TENS.get(p, 0)
    return (total or None), True


def sources_for(device_id, pk):
    """Everything Flipee has actually read at this place: the headlines it
    was given, and the background it looked up."""
    parts = []
    facts = store.get_place_facts(pk)
    if facts and facts["ok"]:
        parts.append(facts["title"])
        parts.append(facts["summary"])
    for entry in store.recent_entries(device_id, limit=60, pk=pk):
        parts.extend(entry["headlines"])
        parts.extend([entry["city"], entry["region"], entry["country"], entry["ssid"]])
    return "\n".join(p for p in parts if p)


_CONTRACTION = re.compile(r"['’].*$")


def _normalize(word):
    """Reduce a token to the word a source would have to contain.

    Everything from an apostrophe on is dropped, which covers both the
    possessive ("Federal Way's" must match a source's "Way") and
    contractions ("I'm" is the pronoun "I", not a proper noun that no
    source mentions — it was firing on nearly every first-person
    sentence, which is most of them).
    """
    return _CONTRACTION.sub("", word.lower())


def _vocabulary(source_text):
    return {_normalize(w) for w in _WORD.findall(source_text)}


def _numbers(source_text):
    return {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(source_text)}


def _check(sentence, vocab, numbers):
    """Reasons this sentence wants a human, or [] if it's fine."""
    reasons = []
    words = _WORD.findall(sentence)
    lowered = [_normalize(w) for w in words]

    if any(w in DIRECTION_WORDS for w in lowered) or any(w in DISTANCE_WORDS for w in lowered):
        reasons.append("spatial claim — check it against a map")

    # Clock times are read off the device's own clock, which is a source
    # the headlines and place summary know nothing about. "8:32 PM" is the
    # one number in an entry that's guaranteed not to be invented.
    without_clocks = _CLOCK.sub(" ", sentence)
    unsourced_numbers = [n for n in
                         (x.replace(",", "").rstrip(".") for x in _NUMBER.findall(without_clocks))
                         if n not in numbers]

    # Same test for spelled-out numbers, but check the digit form too: a
    # source that says "40 displaced" grounds an entry that says "forty",
    # and flagging that would be a false positive of exactly the kind
    # that teaches people to ignore highlights.
    for token in words:
        value, is_number = _word_number(token)
        if not is_number:
            continue
        if _normalize(token) in vocab:
            continue
        if value is not None and str(value) in numbers:
            continue
        unsourced_numbers.append(token)

    if unsourced_numbers:
        reasons.append("number not in any headline or place summary: "
                       + ", ".join(sorted(set(unsourced_numbers))[:3]))

    # Skip the first word: sentence-initial capitalization says nothing.
    # Single letters are initials, list markers, or punctuation artifacts —
    # never a name whose absence from the sources means anything.
    names = [w for w in words[1:]
             if len(_normalize(w)) > 1 and w[:1].isupper()
             and _normalize(w) not in COMMON and _normalize(w) not in vocab]
    if names:
        reasons.append("not mentioned by any source it read: "
                       + ", ".join(sorted(set(names))[:3]))
    return reasons


def annotate(text, device_id=None, pk=None, source_text=None):
    """Split text into segments, each marked with why it needs review.

    Returns [{"text": str, "reasons": [str, ...]}, ...] — segments with an
    empty reasons list are unremarkable and render as plain prose.
    """
    if not text:
        return []
    if source_text is None:
        source_text = sources_for(device_id, pk)
    vocab, numbers = _vocabulary(source_text), _numbers(source_text)
    return [{"text": s, "reasons": _check(s, vocab, numbers)}
            for s in _SENTENCE.split(text.strip()) if s.strip()]


def count_flagged(segments):
    return sum(1 for s in segments if s["reasons"])


def unsourced_numbers(text, source_text):
    """Figures in a piece of writing that appear nowhere in its sources.

    `source_text` must be facts only — the state lines, place background
    and headlines — and must NOT include previous entries. Validating
    against a briefing that contains earlier entries lets a fabrication
    launder itself: one entry invented "nineteen days", that text went
    into the next briefing as a writing sample, and "nineteen" then
    counted as sourced ever after.

    Used by both writers: the archivist retries on a hit (nobody is
    waiting on it), while /reflect just flags the entry for the archivist
    to repair, because a device is holding a 15s budget open.
    """
    found = []
    for seg in annotate(text, source_text=source_text):
        for reason in seg["reasons"]:
            if reason.startswith("number"):
                found.append(reason.split(": ", 1)[-1])
    return found

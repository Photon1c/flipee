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
}

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_NUMBER = re.compile(r"\d[\d,.]*")


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


_POSSESSIVE = re.compile(r"['’]s$")


def _normalize(word):
    """'Way's' and 'Way' are the same word for grounding purposes — without
    this, any possessive reads as a name no source mentions."""
    return _POSSESSIVE.sub("", word.lower())


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

    unsourced_numbers = [n for n in
                         (x.replace(",", "").rstrip(".") for x in _NUMBER.findall(sentence))
                         if n not in numbers]
    if unsourced_numbers:
        reasons.append("number not in any headline or place summary: "
                       + ", ".join(sorted(set(unsourced_numbers))[:3]))

    # Skip the first word: sentence-initial capitalization says nothing.
    names = [w for w in words[1:]
             if w[:1].isupper() and _normalize(w) not in COMMON
             and _normalize(w) not in vocab]
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

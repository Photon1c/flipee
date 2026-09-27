"""
place.py — a little background on wherever Flipee has woken up.

WHY
---
Flipee's only knowledge of a place used to be its name and eight local
headlines, which is why its entries kept reasoning in circles about the
same apartment fire: there was nothing else in the prompt to think
*with*. Knowing that Federal Way is a suburb between Seattle and Tacoma
on Puget Sound, built up after the freeway came through, gives it actual
anchors — the headlines become something happening *somewhere* rather
than the only evidence a place exists.

One Wikipedia summary per city, fetched the first time Flipee lands
there and then cached forever in SQLite. A device that sits in one town
does exactly one lookup, ever.

Deliberately stdlib urllib rather than requests: the relay's whole
dependency list is flask/anthropic/dotenv and this doesn't justify
another entry.

Set FLIPEE_PLACE_LOOKUP=0 to turn it off — then the only outbound call
the relay makes is to Anthropic, and Flipee's knowledge stays strictly
first-hand.
"""

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import store

ENABLED = os.environ.get("FLIPEE_PLACE_LOOKUP", "1").strip().lower() not in ("0", "false", "no")
API = "https://en.wikipedia.org/api/rest_v1/page/summary/"
# Wikipedia asks API clients to identify themselves and can rate-limit
# generic agents harder.
USER_AGENT = "flipee-relay/1.0 (https://github.com/Photon1c/flipee)"
TIMEOUT_S = 4.0
MAX_SUMMARY_CHARS = 700
# A failed lookup is cached too, so a town with no article doesn't mean a
# doomed HTTP call on every single flip — but not cached *forever*, since
# the failure was just as likely a flaky network as a missing page.
RETRY_FAILED_AFTER = timedelta(hours=24)


def _ssl_context():
    """Verify against certifi's bundle rather than whatever the interpreter
    happens to trust.

    Not paranoia: on the dev machine here, an Anaconda build reports no
    default CA file at all and falls back to a store old enough that
    Wikipedia fails with "certificate has expired" — while the exact same
    request against certifi's bundle succeeds. The tempting fix is to turn
    verification off, which is the wrong trade on a server that also holds
    an API key; pinning to a current bundle keeps the check and fixes the
    cause. certifi is already installed as a transitive dependency of the
    anthropic SDK, and is now pinned in requirements.txt for this.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL = _ssl_context()


def _candidates(city, region, country):
    """Most specific title first. 'Federal Way, Washington' is a real
    article; bare 'Federal Way' redirects to it, but for somewhere like
    'Springfield' the qualified form is the only one that isn't a
    disambiguation page."""
    city = (city or "").strip()
    if not city:
        return []
    region = (region or "").strip()
    country = (country or "").strip()
    out = []
    if region:
        out.append("%s, %s" % (city, region))
    if country and country != region:
        out.append("%s, %s" % (city, country))
    out.append(city)
    return out


def _fetch(title):
    url = API + urllib.parse.quote(title.replace(" ", "_"), safe="")
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=_SSL) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    # A disambiguation page is worse than nothing — it would hand the model
    # a list of unrelated towns and invite it to confuse them.
    if data.get("type") not in (None, "standard"):
        return None
    extract = (data.get("extract") or "").strip()
    if not extract:
        return None
    if len(extract) > MAX_SUMMARY_CHARS:
        cut = extract.rfind(". ", 0, MAX_SUMMARY_CHARS)
        extract = extract[:cut + 1] if cut > 200 else extract[:MAX_SUMMARY_CHARS] + "..."
    return {
        "title": data.get("title") or title,
        "summary": extract,
        "source": (data.get("content_urls", {}).get("desktop", {}).get("page")
                   or "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")),
    }


def facts_for(city, region, country):
    """Cached background for a place, or None. Never raises: no anchors is
    a worse entry, not a failed one."""
    pk = store.place_key(city, region, country)
    if not (city or "").strip():
        return None

    cached = store.get_place_facts(pk)
    if cached:
        if cached["ok"]:
            return cached
        fetched = store._parse_utc(cached["fetched_utc"])
        if fetched and datetime.now(timezone.utc) - fetched < RETRY_FAILED_AFTER:
            return None  # known-bad and still fresh; don't hammer it

    if not ENABLED:
        return None

    for title in _candidates(city, region, country):
        try:
            found = _fetch(title)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
            continue
        if found:
            store.save_place_facts(pk, found["title"], found["summary"],
                                   found["source"], ok=True)
            return store.get_place_facts(pk)

    store.save_place_facts(pk, "", "", "", ok=False)
    return None

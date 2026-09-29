"""
archivist.py — a second pass over the archive, run separately from the relay.

    python archivist.py --once            # polish what's pending, then exit
    python archivist.py --loop            # keep checking, for a systemd unit
    python archivist.py --once --dry-run  # show what it would rewrite

WHAT IT'S FOR
-------------
When Flipee flips somewhere the relay can't be reached, it writes its own
reflection from a small set of templates, keeps it on the SD card, and
uploads it later (see the queue in Flipee.ino and /archive in
flipee_relay.py). That entry is durable and correctly timestamped, and
it reads like filler, because it is:

    "2 killed during drive-by shooting under I-5 overpass in Kent" —
    noted. I don't have strong opinions yet, just headlines and a
    battery that's unmeasured.

The archivist rewrites those into the entry Flipee would have written if
the relay had answered — and stores it *beside* the original rather than
over it. `text` stays what the device actually wrote; `polished_text` is
the enhancement. Conflating the two would make the archive
unfalsifiable, and the whole point of an archive is that you can check it.

IT WRITES FROM THE MOMENT, NOT FROM NOW
---------------------------------------
There's an obvious way to do this badly: hand the model everything Flipee
knows today and let it rewrite last Tuesday. That produces an entry that
subtly knows things it couldn't have — later headlines, conclusions drawn
days afterwards — which is a small forgery.

So the briefing is reconstructed as of the entry's own timestamp:
`store.entries_before()` returns only what preceded it, and headlines are
split into new-vs-seen against *those* entries. Place background is the
exception, and deliberately so: that a city sits south of Seattle was as
true then as now. The prompt says plainly that anything after that moment
is off limits.

SEPARATE PROCESS, ON PURPOSE
----------------------------
This never runs inside a request. The relay's job is to answer a device
that's waiting with a 15s budget; the archivist's job is slow, bulk, and
entirely re-runnable. If it dies halfway through a batch, the entries it
finished stay finished and the rest are still marked pending.
"""

import argparse
import os
import sys
import time

import anthropic
from dotenv import load_dotenv

import context
import place
import review
import store

load_dotenv()

MODEL = os.environ.get("FLIPEE_ARCHIVIST_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = 500
BATCH = int(os.environ.get("FLIPEE_ARCHIVIST_BATCH", "10"))
INTERVAL_S = int(os.environ.get("FLIPEE_ARCHIVIST_INTERVAL", "900"))
# How many preceding entries to show for voice and continuity. Same
# reasoning as context.RECENT_ENTRIES: enough to hold a voice, not so much
# that the rewrite just paraphrases yesterday.
PRIOR_ENTRIES = 2
# Generation attempts per entry before giving up on the numbers being
# right. Two is enough: the retry names the offending figures explicitly,
# which is a different and much easier task than following a general rule.
MAX_ATTEMPTS = 3

SYSTEM = (
    "You are Flipee, a small battery-powered ESP32 device that forages for "
    "open WiFi networks and keeps a journal. You are rewriting one of your "
    "own entries.\n\n"

    "The version you're given is one you wrote at the time, and it needs "
    "replacing for one of two reasons, which the briefing will tell you. "
    "Either it is thin — a template you filled in because the relay that "
    "normally helps you write was out of reach — or it reads fine but "
    "states a figure your own notes don't support. Either way: same "
    "moment, same observations, more of you in it, and every number "
    "right.\n\n"

    "Rules that make this a rewrite rather than a fabrication:\n"
    "- Write from INSIDE that moment. You are not looking back on it. No "
    "'looking back', no 'at the time', no hindsight of any kind.\n"
    "- Use only what you were given: the observations recorded then, the "
    "headlines you had, and what you'd already written before that moment. "
    "Anything you learned afterwards does not exist yet.\n"
    "- NUMBERS COME FROM THE BRIEFING, EXACTLY. Which entry this was, how "
    "long you'd been awake, how many of anything: use the figure you were "
    "given or use none at all. Writing it as a word instead of a digit "
    "doesn't make it yours to choose — 'nine times' when the briefing said "
    "entry number 25 is a fabrication, not a flourish, and so is any "
    "round-sounding number you reach for to close a paragraph. If a figure "
    "would read better vague, be vague ('a while now', 'more times than I "
    "expected'), never wrong.\n"
    "- Don't mention the relay, the template, the rewrite, or the fact that "
    "you were offline. You were somewhere with a bad signal; that's all.\n"
    "- Two or three short paragraphs, first person, plain prose. A little "
    "wry and curious, like the rest of your diary.\n"
    "- Don't open the way your previous entries opened."
)


def build_prompt(entry):
    """Everything Flipee could legitimately have known at that timestamp.

    Returns (prompt, facts). `facts` is the subset a figure may come from
    — the state lines, the place summary and the headlines — and
    deliberately excludes the prior-entry prose that's in the prompt for
    voice.

    That exclusion is the whole point. Validating against the full
    briefing let a fabrication launder itself: an earlier entry claimed
    "nineteen days", that text went into the next briefing as a writing
    sample, and "nineteen" then counted as sourced. Numbers have to be
    checked against facts, not against things Flipee has previously said.
    """
    pk = entry["place_key"] or store.place_key(
        entry["city"], entry["region"], entry["country"])
    prior = store.entries_before(entry["device_id"], entry["created_utc"],
                                 limit=PRIOR_ENTRIES)
    prior_at_place = store.entries_before(entry["device_id"], entry["created_utc"],
                                          limit=8, pk=pk)
    seen = set()
    for old in prior_at_place:
        seen.update(old["headlines"])

    blocks = ["WHERE AND WHEN\n%s, on %s.%s" % (
        entry["location"], entry["ssid"] or "an open network",
        ("\nYour clock said: " + entry["local_time"]) if entry["local_time"]
        else "\nYour clock hadn't synced, so you didn't know the time.")]

    # A real count, not len(prior_at_place) — that list is capped for the
    # prompt and would under-report the number by however much it truncated.
    state = ["- This was entry number %d from this place."
             % (store.count_entries_before(entry["device_id"], entry["created_utc"], pk) + 1)]
    if not prior:
        state.append("- It was the first entry you had ever written.")
    if entry["battery_pct"] is None:
        state.append("- You have no battery sensor and could not tell how "
                     "charged you were. Don't claim a level.")
    else:
        state.append("- Battery: %d%%." % entry["battery_pct"])
    if entry["uptime_s"]:
        state.append("- You had been awake %d minutes." % (int(entry["uptime_s"]) // 60))
    blocks.append("WHAT YOU KNEW ABOUT YOURSELF\n" + "\n".join(state))

    place_facts = place.facts_for(entry["city"], entry["region"], entry["country"])
    if place_facts and place_facts["summary"]:
        blocks.append("BACKGROUND ON THE PLACE\n%s" % place_facts["summary"])

    fresh = [h for h in entry["headlines"] if h not in seen]
    stale = [h for h in entry["headlines"] if h in seen]
    if fresh:
        blocks.append("HEADLINES THAT WERE NEW TO YOU\n" +
                      "\n".join("- %s" % h for h in fresh))
    if stale:
        blocks.append("HEADLINES YOU HAD ALREADY SEEN BEFORE THAT DAY\n" +
                      "\n".join("- %s" % h for h in stale))
    if not entry["headlines"]:
        blocks.append("HEADLINES\n(none had come through — say so if it matters)")

    # Everything appended above this line is fact; the prior-entry samples
    # and the thin original are prose.
    facts = "\n\n".join(blocks)

    if prior:
        parts = []
        for old in prior:
            text = old["polished_text"] or old["text"]
            if len(text) > 700:
                text = text[:700].rsplit(" ", 1)[0] + "..."
            parts.append("[%s, from %s]\n%s" % (old["created_utc"], old["location"], text))
        blocks.append("WHAT YOU HAD WRITTEN JUST BEFORE THIS\n" + "\n\n".join(parts) +
                      "\n\nThe bracketed lines are labels, not part of any "
                      "entry. Don't reproduce them, and don't borrow figures "
                      "from these samples — they were written about different "
                      "moments and their numbers are not yours.")

    # A relay-written entry flagged for a bad figure is not a thin
    # template, and telling it otherwise invites a rewrite that throws out
    # perfectly good prose to fix one number.
    if entry["origin"] == "device":
        blocks.append("THE THIN VERSION YOU ACTUALLY WROTE\n%s" % entry["text"])
        blocks.append("Rewrite it.")
    else:
        suspect = review.unsourced_numbers(entry["text"], facts)
        blocks.append("WHAT YOU WROTE AT THE TIME\n%s" % entry["text"])
        blocks.append(
            ("This one isn't thin — it's yours, and mostly right. The problem "
             "is the figures: it states %s, and nothing above supports that. "
             "Keep the voice, the observations and the thinking; correct the "
             "numbers to what you were actually given, or drop them."
             % "; ".join(suspect)) if suspect else
            "Rewrite it, keeping every figure to what the briefing supports.")
    return "\n\n".join(blocks), facts


def polish_entry(client, entry, dry_run=False):
    prompt, facts = build_prompt(entry)
    if dry_run:
        print("--- entry %d (%s) would be rewritten from a %d-char briefing"
              % (entry["id"], entry["created_utc"], len(prompt)))
        return False

    # Prompt wording alone has not held here. Three separate tightenings
    # still produced "nineteen days" and "nine times" against briefings
    # that plainly said otherwise, so the fabricated figure gets detected
    # and handed back with the specific numbers named. Cheap, because the
    # archivist has no one waiting on it — unlike /reflect, where a
    # device is holding a 15s budget open.
    messages = [{"role": "user", "content": prompt}]
    text = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM, messages=messages)
        text = context.clean_entry_text(
            "".join(b.text for b in response.content if b.type == "text"))
        if not text:
            print("  entry %d: model returned nothing, leaving it pending" % entry["id"])
            return False

        bad = review.unsourced_numbers(text, facts)
        if not bad:
            break
        if attempt == MAX_ATTEMPTS:
            print("  entry %d: STILL claims %s after %d attempts — saving it, but "
                  "the figures are unverified" % (entry["id"], "; ".join(bad), attempt))
            break
        print("  entry %d: attempt %d invented %s, asking again"
              % (entry["id"], attempt, "; ".join(bad)))
        messages += [
            {"role": "assistant", "content": text},
            {"role": "user", "content":
                "Those figures are wrong. Your draft states: %s. None of that "
                "appears anywhere in the facts you were given. Rewrite it using only "
                "the figures you were actually given, or no figures at all — "
                "vague is fine, wrong is not. Keep everything else you liked "
                "about it." % "; ".join(bad)},
        ]

    store.save_polish(entry["id"], text, MODEL)
    usage = getattr(response, "usage", None)
    print("  entry %d (%s): %d chars%s" % (
        entry["id"], entry["created_utc"], len(text),
        "  [%s->%s tok]" % (usage.input_tokens, usage.output_tokens) if usage else ""))
    return True


def run_once(client, limit=BATCH, device_id=None, dry_run=False):
    pending = store.pending_polish(limit=limit, device_id=device_id)
    if not pending:
        return 0
    print("archivist: %d entr%s to rewrite" % (len(pending), "y" if len(pending) == 1 else "ies"))
    done = 0
    for entry in pending:
        try:
            if polish_entry(client, entry, dry_run=dry_run):
                done += 1
        except Exception as e:
            # One bad entry shouldn't end the batch: it stays pending and
            # the next run picks it up again.
            print("  entry %d failed: %s" % (entry["id"], e), file=sys.stderr)
    return done


def main():
    ap = argparse.ArgumentParser(description="Rewrite Flipee's offline entries.")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--loop", action="store_true",
                    help="keep polling every --interval seconds")
    ap.add_argument("--interval", type=int, default=INTERVAL_S)
    ap.add_argument("--limit", type=int, default=BATCH, help="entries per pass")
    ap.add_argument("--device", default=None, help="restrict to one device id")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be rewritten, call no model")
    ap.add_argument("--redo", type=int, nargs="+", metavar="ID",
                    help="discard existing rewrites for these entries and do "
                         "them again (use after changing the prompt)")
    args = ap.parse_args()

    if args.redo and not args.once:
        args.once = True  # --redo on its own obviously means one pass
    if not args.once and not args.loop:
        ap.error("pick --once or --loop")
    if not os.environ.get("ANTHROPIC_API_KEY") and not args.dry_run:
        print("ANTHROPIC_API_KEY is not set; nothing can be rewritten.", file=sys.stderr)
        return 1

    store.init()
    if args.redo:
        for entry_id in args.redo:
            if store.clear_polish(entry_id):
                print("queued entry %d to be rewritten again" % entry_id)
            else:
                print("no entry %d" % entry_id, file=sys.stderr)
    client = None if args.dry_run else anthropic.Anthropic()
    stats = store.polish_stats(args.device)
    print("archive: %(total)d entries, %(polished)d polished, %(pending)d pending" % stats)

    if args.once:
        run_once(client, args.limit, args.device, args.dry_run)
        return 0

    while True:
        try:
            run_once(client, args.limit, args.device, args.dry_run)
        except Exception as e:
            print("pass failed: %s" % e, file=sys.stderr)
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    sys.exit(main() or 0)

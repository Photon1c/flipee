"""
token_report.py — what one flip actually costs, measured not guessed.

    python token_report.py [device_id]

Counts the real briefing for a device against the relay's own model using
the count_tokens endpoint (never a third-party tokenizer — those are
OpenAI's and undercount Claude by 15-20%). Prints a per-section
breakdown, because "the briefing is 1.2k tokens" is less actionable than
knowing which section is 600 of them.

Counting is free and doesn't run inference, so this is safe to re-run
after any prompt change.
"""

import sys

import anthropic
from dotenv import load_dotenv

import context
import flipee_relay
import memory
import store

load_dotenv()

# Claude Haiku 4.5 list price, USD per million tokens.
PRICE_IN, PRICE_OUT = 1.00, 5.00
# Claude Haiku 4.5 will not cache a prefix below this. Ours is nowhere
# near it, which is the single most useful fact in this report.
CACHE_MINIMUM = 4096

client = anthropic.Anthropic()
MODEL = flipee_relay.MODEL


def count(system, user_text):
    return client.messages.count_tokens(
        model=MODEL, system=system,
        messages=[{"role": "user", "content": user_text}],
    ).input_tokens


def main():
    device_id = sys.argv[1] if len(sys.argv) > 1 else None
    if not device_id:
        found = store.devices()
        if not found:
            print("No devices in the archive yet.")
            return
        device_id = found[0]["device_id"]

    latest = store.recent_entries(device_id, limit=1)
    if not latest:
        print("No entries for %s." % device_id)
        return
    entry = latest[0]

    blocks, state = context.build_blocks(
        device_id,
        city=entry["city"], region=entry["region"], country=entry["country"],
        ssid=entry["ssid"], battery_pct=entry["battery_pct"],
        uptime_s=entry["uptime_s"], headlines=entry["headlines"],
        local_time=entry["local_time"], lat=entry["lat"], lon=entry["lon"],
    )

    system_only = count(flipee_relay.SYSTEM_PROMPT, "x")
    full = count(flipee_relay.SYSTEM_PROMPT, "\n\n".join(blocks))

    print("device %s  |  model %s  |  %d entries archived"
          % (device_id, MODEL, state["total_entries"]))
    print()
    print("REFLECTION CALL")
    print("  %-46s %6d" % ("system prompt", system_only))

    # Marginal cost per section: total minus that section. Counting a
    # section alone would charge it for per-message overhead it doesn't
    # actually add.
    for block in blocks:
        without = count(flipee_relay.SYSTEM_PROMPT,
                        "\n\n".join(b for b in blocks if b is not block))
        label = block.split("\n", 1)[0][:44]
        print("  %-46s %6d" % (label, full - without))
    print("  %-46s %6d" % ("TOTAL INPUT", full))

    pm = store.get_memory(device_id, state["place_key"])
    gm = store.get_memory(device_id, store.GLOBAL_KEY)
    mem_prompt = memory._prompt(entry["location"], entry["text"],
                                entry["headlines"], pm, gm)
    mem_in = count(memory.SYSTEM, mem_prompt)

    print()
    print("MEMORY CALL (background, after the device has its reply)")
    print("  %-46s %6d" % ("TOTAL INPUT", mem_in))

    out_reflection, out_memory = flipee_relay.MAX_TOKENS, memory.MAX_TOKENS
    cost = ((full + mem_in) * PRICE_IN
            + (out_reflection + out_memory) * PRICE_OUT) / 1_000_000

    print()
    print("PER FLIP, worst case (both outputs hitting max_tokens)")
    print("  input   %6d tokens" % (full + mem_in))
    print("  output  %6d tokens (caps, not measured)" % (out_reflection + out_memory))
    print("  cost    $%.5f   ->  $%.3f per 100 flips" % (cost, cost * 100))

    print()
    if full < CACHE_MINIMUM:
        print("Prompt caching: NOT AVAILABLE at this size. %s needs a %d-token\n"
              "prefix and the whole request is %d. A cache_control marker here\n"
              "would be silently ignored (cache_creation_input_tokens: 0)."
              % (MODEL, CACHE_MINIMUM, full))
    else:
        print("Prompt caching: viable — the request clears %s's %d-token minimum."
              % (MODEL, CACHE_MINIMUM))


if __name__ == "__main__":
    main()

"""
ollama_backend.py — an alternative writer for reflections.

Ollama runs on the VPS (localhost:11434) with cloud models available
through it, and its OpenAI-compatible endpoint is close enough to the
shape we already use that swapping writers is a config change rather
than a rewrite. Set REFLECTION_BACKEND=ollama-cloud to use it, and the
relay falls back to Anthropic if it fails.

WHY stdlib urllib AND NOT httpx
-------------------------------
httpx is already installed — the anthropic SDK depends on it — so
importing it looks free. It isn't. The relay's one real outage was an
httpx2/httpcore2 version conflict that took /reflect down while /health
stayed a cheerful 200, and the fix was pinning anthropic==0.40.0.
Depending on httpx directly would make that pin load-bearing for a
second, unrelated reason, and the next person to run `pip install -U`
gets to rediscover it. place.py already talks to Wikipedia over urllib;
this is the same shape of call.

TIMEOUT IS A SHARED BUDGET, NOT A LOCAL CHOICE
----------------------------------------------
The device gives the relay RELAY_TIMEOUT_MS (15s) and then writes its own
templated reflection. If Ollama burns 12s before failing over, Anthropic's
~4.5s lands past that deadline and the flip gets a template anyway — a
fallback that exists but never arrives in time. So the default here is 7s:
long enough for a cloud model to answer, short enough to leave room for
the fallback to actually be useful.
"""

import json
import os
import urllib.error
import urllib.request

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("OLLAMA_MODEL", "kimi-k2.5:cloud")
# See the note above: this plus the Anthropic fallback has to fit inside
# the device's RELAY_TIMEOUT_MS, not merely be smaller than it.
TIMEOUT_S = float(os.environ.get("OLLAMA_TIMEOUT", "7"))


class OllamaError(RuntimeError):
    """Anything that means "no usable reflection from Ollama"."""


def generate(system, prompt, max_tokens):
    """Returns (text, in_tokens, out_tokens). Raises OllamaError.

    Deliberately sets no temperature. The Anthropic path doesn't set one
    either, and the point of running two writers is comparing the models
    — changing the sampling at the same time means any difference in the
    prose can't be attributed to either.
    """
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")

    request = urllib.request.Request(
        HOST + "/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise OllamaError("HTTP %s from Ollama: %s" % (e.code, detail))
    except Exception as e:
        # Timeouts, refused connections, malformed JSON — all the same
        # thing to the caller, which is going to fall back regardless.
        raise OllamaError("%s: %s" % (e.__class__.__name__, e))

    try:
        text = payload["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise OllamaError("unexpected response shape: %s"
                          % json.dumps(payload)[:200])
    if not text:
        raise OllamaError("empty reflection")

    # Mapped so the archive's token accounting keeps working across both
    # writers — otherwise half the entries would silently record nothing
    # and the per-entry cost figures in the dashboard would be a lie of
    # omission.
    usage = payload.get("usage") or {}
    return text, usage.get("prompt_tokens"), usage.get("completion_tokens")

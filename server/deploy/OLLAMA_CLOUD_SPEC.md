# Ollama Cloud Model Integration — Implementation Spec
**Date:** 2026-09-29  
**Status:** Ready to implement  
**Author:** Sherlock (systems detective)

---

## Context

The relay currently calls Anthropic (claude-haiku-4-5) for all reflections.
Goal: add an Ollama cloud backend as a cost-saving alternative, switchable
via a single env var with Anthropic as automatic fallback.

Ollama is already running on the VPS (`localhost:11434`) with 8 cloud models
confirmed accessible:

```
nemotron-3-super:cloud
nemotron-3-ultra:cloud
deepseek-v4-flash:cloud
kimi-k2.6:cloud
kimi-k2.5:cloud
gemma4:31b-cloud
ministral-3:14b-cloud
glm-5.1:cloud
```

Local Ollama (home machine via Tailscale) is NOT accessible from VPS —
`100.70.36.88:11434` returns empty (API not exposed on Tailscale interface).
Use `localhost:11434` on the VPS only.

---

## New `.env` Variables

```bash
# Switch reflection backend: "anthropic" (default) or "ollama-cloud"
REFLECTION_BACKEND=ollama-cloud

# Which Ollama cloud model to use (start with kimi-k2.5:cloud)
OLLAMA_MODEL=kimi-k2.5:cloud

# Timeout in seconds before falling back to Anthropic (must be < RELAY_TIMEOUT_MS/1000)
OLLAMA_TIMEOUT=12

# Fall back to Anthropic if Ollama fails/times out (recommended: true)
ANTHROPIC_FALLBACK=true
```

Add these to `server/.env.example` as well (with placeholder values).

---

## Implementation: `flipee_relay.py`

### 1. New helper function

Add alongside the existing Anthropic call in `flipee_relay.py`:

```python
import httpx
import os

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "kimi-k2.5:cloud")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "12"))
REFLECTION_BACKEND = os.environ.get("REFLECTION_BACKEND", "anthropic")
ANTHROPIC_FALLBACK = os.environ.get("ANTHROPIC_FALLBACK", "true").lower() == "true"


def _ollama_reflect(prompt: str, system: str) -> str:
    """
    Call Ollama's OpenAI-compatible chat endpoint.
    Raises on timeout or HTTP error — caller handles fallback.
    """
    resp = httpx.post(
        f"{OLLAMA_HOST}/v1/chat/completions",
        json={
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 400,
            "temperature": 0.8,
        },
        timeout=OLLAMA_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()
```

### 2. Update `/reflect` endpoint

In the existing `/reflect` handler, replace the Anthropic call block with:

```python
model_used = MODEL  # existing Anthropic model name
reflection_text = None

if REFLECTION_BACKEND == "ollama-cloud":
    try:
        reflection_text = _ollama_reflect(prompt, SYSTEM_PROMPT)
        model_used = OLLAMA_MODEL
    except Exception as e:
        app.logger.warning(f"Ollama failed ({e}), falling back to Anthropic")
        if not ANTHROPIC_FALLBACK:
            raise
        # fall through to Anthropic below

if reflection_text is None:
    # Existing Anthropic path (unchanged)
    response = client.messages.create(
        model=MODEL,
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    reflection_text = response.content[0].text.strip()
    model_used = MODEL

# Store model_used instead of MODEL when saving to DB
```

### 3. Log the backend used

The `reflections` table already has a `model` column. Make sure `model_used`
is passed to `store.add(...)` so each entry records which backend generated it.
This enables easy A/B quality comparison in the dashboard.

---

## Test Priority Order

Test in this order — quality first, then speed:

| Model | Why |
|---|---|
| `kimi-k2.5:cloud` | Strong narrative/creative writer, start here |
| `deepseek-v4-flash:cloud` | Fast, good prose, good fallback |
| `nemotron-3-super:cloud` | NVIDIA model, unknown writing style |
| `glm-5.1:cloud` | Chinese model, interesting cultural perspective |
| `gemma4:31b-cloud` | Large, may be slow |
| `ministral-3:14b-cloud` | Mistral variant, solid general quality |

**Skip for reflections:** `nemotron-3-ultra:cloud` (likely overkill/slow)

---

## Archivist Note

Keep Anthropic for the archivist polish pass (`archivist.py`).
Quality matters more than cost for the async rewrite — latency is irrelevant
and Claude's prose is noticeably better for the polish task.

Only the live `/reflect` path needs the Ollama option.

---

## A/B Testing Workflow

Once implemented, switch backends with no code changes:

```bash
# Switch to Ollama cloud
sudo systemctl edit flipee-relay --force
# Add: Environment=REFLECTION_BACKEND=ollama-cloud
# Add: Environment=OLLAMA_MODEL=kimi-k2.5:cloud
sudo systemctl restart flipee-relay

# Generate a few entries, compare quality in dashboard

# Switch back to Anthropic
sudo systemctl edit flipee-relay --force
# Remove the overrides
sudo systemctl restart flipee-relay
```

Or just edit `server/.env` and restart:

```bash
# Edit .env: REFLECTION_BACKEND=ollama-cloud
sudo systemctl restart flipee-relay
```

---

## Verification Steps (After Implementation)

```bash
# 1. Test Ollama endpoint directly on VPS
curl -s http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "kimi-k2.5:cloud",
    "messages": [{"role":"user","content":"Write one sentence about Federal Way, WA."}],
    "max_tokens": 50
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['choices'][0]['message']['content'])"

# 2. Test /reflect with REFLECTION_BACKEND=ollama-cloud set
curl -s -X POST https://flipee.website/reflect \
  -H "Content-Type: application/json" \
  -H "X-Flipee-Key: <relay-key>" \
  -d '{"city":"Federal Way","region":"Washington","country":"US",
       "headlines":["SR-99 closure resolved"],"battery":80,
       "lat":47.32,"lon":-122.31}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['ok'], d.get('model','?'), d.get('text','')[:120])"

# 3. Verify model column in DB shows ollama model name
sqlite3 ~/temporary_shuttle/flipee/server/flipee.sqlite3 \
  "SELECT id, model, substr(text,1,60) FROM reflections ORDER BY id DESC LIMIT 3;"
```

Expected: `model` column shows `kimi-k2.5:cloud` for Ollama entries,
`claude-haiku-4-5-20251001` for Anthropic entries.

---

## Files to Modify

- `server/flipee_relay.py` — add `_ollama_reflect()`, update `/reflect` handler
- `server/.env.example` — add new env vars with placeholder values
- `server/.env` (VPS) — add `REFLECTION_BACKEND`, `OLLAMA_MODEL`, `OLLAMA_TIMEOUT`

No device firmware changes needed. No DB schema changes needed.

---

*Spec ready for implementation. Pull from `main` branch before starting.*

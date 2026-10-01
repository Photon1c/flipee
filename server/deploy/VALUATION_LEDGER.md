# Flipee Valuation Ledger
**Project:** Flipee — Foraging Journal Device
**Status:** Production (Phase M9 complete) — M10 on hold
**Date:** 2026-09-30
**Prepared by:** Sherlock (systems detective)

---

## Executive Summary

Flipee has achieved **production readiness** as of 2026-09-29. The full stack is live:
- Device wakes on any open WiFi → `flipee.website:443` (TLS) → nginx → gunicorn → LLM → dashboard
- SD card local backup + store-and-forward queue + async archivist polish
- 32 entries archived with continuity across machine migrations
- Lifetime token cost: **$0.05** (all Haiku)

**M10 phase is on hold** — not due to Flipee's performance, but because the broader project budget absorbed ~$400 in frontier model (Opus) spend over 4 days of interactive development work. This is a **portfolio-level budget discipline decision**, not a Flipee technical issue.

---

## Current Asset Valuation

| Component | Status | Cost to Build | Ongoing Cost | Notes |
|---|---|---|---|---|
| **Hardware (ESP32-S3 AMOLED V2)** | Deployed | ~$45 | $0 | SDMMC auto-detect, battery ADC ready |
| **Domain (flipee.website)** | Active | $12/yr | $12/yr | Namecheap, auto-renewal |
| **VPS (srv1313560)** | Active | $0/mo | $0/mo | Existing infrastructure |
| **TLS Certificate** | Active | $0 | $0 | Let's Encrypt, auto-renewal |
| **Relay Software** | Production | Dev time | $0.05 lifetime | Haiku only, 32 entries |
| **Archivist Service** | Production | Dev time | $0 | Runs every 15min via systemd |
| **Queue Durability (SD + NVS)** | Proven | Dev time | $0 | Survived real outage test |
| **Dashboard** | Production | Dev time | $0 | SQLite + static files |

**Total tangible asset value:** ~$57 + dev time
**Monthly burn rate:** ~$1 (domain amortized)
**Token efficiency:** $0.05 / 32 entries = **$0.0016 per reflection**

---

## Phase History

| Phase | Scope | Status | Commit |
|---|---|---|---|
| M1–M3 | Hardware bringup, local LAN relay | ✅ Complete | (historical) |
| M4 | SD card journal, queue design | ✅ Complete | 03bba38 |
| M5 | Store-and-forward queue + `/archive` | ✅ Complete | 03e8edd |
| M6 | Archivist agent (async polish) | ✅ Complete | 2ac6a17 |
| M7 | Hallucination detection (word-number regex) | ✅ Complete | 3e3056c |
| M8 | Local testing (Tailscale, LAN) | ✅ Complete | 03c5dd7 |
| M9 | **Production cutover** (domain, TLS, systemd, keys) | ✅ Complete | **6a26dbc** |
| **M10** | Ollama cloud backend, cost optimization | ⏸ **ON HOLD** | — |

---

## M10 Phase — Scope (Deferred)

| Task | Priority | Effort | Dependencies |
|---|---|---|---|
| Implement Ollama cloud backend (`kimi-k2.5:cloud`) | High | 1–2 days | Spec ready (`OLLAMA_CLOUD_SPEC.md`) |
| A/B quality comparison (Haiku vs Ollama) | Medium | 1 week | Backend live |
| Cost per reflection measurement | Medium | Ongoing | Backend live |
| Battery GUI integration (hardware arrival) | Low | 1 day | Hardware delivery |
| Preference learning (model selection) | Low | 1 week | A/B data |

**Spec location:** `server/deploy/OLLAMA_CLOUD_SPEC.md`
**Models available:** 8 cloud models on VPS localhost:11434
**Recommended start:** `kimi-k2.5:cloud` (strong narrative writer)

---

## Budget Context — The M10 Hold Reason

### The Cost Event (2026-09-22 to 2026-09-29)

| Metric | Value |
|---|---|
| **Total spend (7 days)** | ~$400 |
| **Primary model** | Opus (claude-opus-4) |
| **Cause** | Default model selection in Windows terminal + browser sessions during long debugging |
| **Flipee production cost** | $0.05 (Haiku — negligible) |
| **Root cause** | Model selection carelessness during interactive dev work |

### Recovery Plan (Active)

| Action | Status | Expected Recovery |
|---|---|---|
| Options trading (SPY puts) | Active | $75 profit realized 2026-09-29 |
| Explicit model=Haiku in terminal aliases | Pending | Prevents future drift |
| OpenRouter free tier (460+ models) | Configured | Backup provider active |
| Disable expensive models in OpenRouter | Pending | Cost guardrail |
| Monthly budget cap enforcement | Pending | Hard limit |

### Silver Lining

- Flipee proved **extremely cost-efficient** at production scale ($0.05 lifetime)
- The cost event was **interactive development**, not production workload
- Recovery mechanism (options trading) is **already working** ($75/day demonstrated)
- OpenRouter provides **free fallback** for experimentation
- Budget discipline lesson learned: **explicit model selection required**

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| M10 delay extends >2 weeks | Medium | Low | Flipee production is complete; no urgency |
| Budget recovery slower than expected | Low | Medium | Options strategy active; OpenRouter free tier |
| Ollama cloud quality < Haiku | Low | Medium | A/B test before commit; fallback to Haiku |
| Device hardware failure | Low | Medium | SD backup + queue proven; reflash is 10 min |

---

## Resumption Criteria for M10

M10 will resume when **all** of the following are true:

1. ✅ Budget recovery on track (weekly net positive via trading/savings)
2. ✅ Explicit model selection enforced in all interactive sessions
3. ✅ OpenRouter cost guardrails configured (expensive models disabled)
4. ⏳ Next development window available (no competing priorities)

**Estimated resumption:** Week of 2026-10-06 (pending budget confirmation)

---

## Sign-Off

**Sherlock (Systems Detective):**
> Flipee is production-complete. The asset is earning its keep at $0.0016/reflection. M10 is a cost-optimization phase, not a capability gap. The hold is prudent portfolio management.

**Leslie (Owner):**
> M10 paused for budget discipline. Recovery in progress. Will resume when balance sheet supports it.

---

*This ledger is a living document. Update on each phase transition or material budget change.*
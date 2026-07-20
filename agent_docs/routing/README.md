# Routing

Bifrost sidecar: pin each session to deterministic provider for prompt-cache
locality. Fixes Bifrost alpha-sort (every request starts `nvidia-1`, walks
`nvidia-1, nvidia-10, nvidia-2, ...` lexicographic not numeric).

Status: **v2 shipped (Bifrost-tfz)**. Session-pinned routing with global
cooldown; send-order/feedback logic lives in pure helpers in `state.py`
(`build_send_order`, `fallback_feedback`), wired from `proxy.py`. Pooled models
declared in `sidecar/pools.json`. Non-pooled = verbatim passthrough, no logs.

## Read these first

1. **[bifrost-routing-facts.md](bifrost-routing-facts.md)** — Bifrost routing
   mechanics: `provider/model` prefix, body `fallbacks` array, `routing_info`
   fields, what does NOT work. **Start here before touching routing logic.**
2. **[sidecar-routing-policy.md](sidecar-routing-policy.md)** — the sidecar's
   own send-order + cooldown policy: full ring (hot appended last), the two
   feedback paths (first-skipped cooled, not primary), why `fell_back` not
   `is_fallback`. **Read after the facts, before changing routing.**
3. **[session-identity.md](session-identity.md)** — how sidecar derives session:
   `prompt_cache_key` real signal (NOT `previous_response_id`); least-loaded
   pin assignment with random tie-break on cold start.
4. **[sidecar-runbook.md](sidecar-runbook.md)** — run + verify sidecar
   (incl. `python -m unittest sidecar.tests.test_routing -v`).

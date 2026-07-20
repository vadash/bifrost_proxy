# Routing

Bifrost sidecar: pin each session to deterministic provider for prompt-cache
locality. Fixes Bifrost alpha-sort (every request starts `nvidia-1`, walks
`nvidia-1, nvidia-10, nvidia-2, ...` lexicographic not numeric).

Status: **v2 shipped (Bifrost-tfz)**. `sidecar/proxy.py` does session-pinned
routing with global cooldown. Pooled models declared in `sidecar/pools.json`.
Non-pooled = verbatim passthrough, no logs.

## Read these first

1. **[bifrost-routing-facts.md](bifrost-routing-facts.md)** — Bifrost routing
   mechanics: `provider/model` prefix, body `fallbacks` array, `routing_info`
   fields, what does NOT work. **Start here before touching routing logic.**
2. **[session-identity.md](session-identity.md)** — how sidecar derives session:
   `prompt_cache_key` real signal (NOT `previous_response_id`).
3. **[sidecar-runbook.md](sidecar-runbook.md)** — run + verify sidecar.

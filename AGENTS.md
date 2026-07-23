# Agent Instructions

Bifrost AI Gateway with a Python sidecar that pins sessions to deterministic
providers for prompt-cache locality. Uses **bd (beads)** for issue tracking.

## Repository map

- `sidecar/` — stdlib routing proxy package (v2.2, Bifrost-tfz). Run with `python -m sidecar`. Listens on :8088, forwards to Bifrost :8080. Pooled models (declared in `sidecar/pools.json`, shuffled at startup for randomized fallback order; with `--reserve-bifrost N` the first N alpha-sorted providers are reserved for the Bifrost auto route and excluded from sidecar pooling) get session-pinned provider + cooldown routing; non-pooled models pass through verbatim. Pooled requests log to `sidecar/sidecar.log` (deleted by `start_sidecar.cmd` on each launch); `sidecar/capture.jsonl` is recorded only when `--capture` is passed (off by default). Modules: `__main__.py` (entrypoint+argparse), `config.py` (paths/tunables/pools), `state.py` (thr...
- `start_sidecar.cmd` — repo-root launcher for the sidecar.
- `start_bifrost.cmd` — launcher for Bifrost itself (npx, port 8080).
- `agent_docs/routing/` — **verified routing mechanics, session-identity derivation, sidecar runbook**. Read [`agent_docs/routing/README.md`](agent_docs/routing/README.md) before touching anything routing-related.

## Routing & architecture knowledge (durable)

The verified Bifrost routing facts, the sidecar's own send-order/cooldown
policy, and session-identity derivation live in
[`agent_docs/routing/`](agent_docs/routing/README.md) — not in beads, not in
memory. That is the authoritative home; read it first when working on the
sidecar. It covers: how Bifrost alpha-sorts providers, the `provider/model`
prefix trick, the body `fallbacks` array, `routing_info` extraction, why
`prompt_cache_key` (not `previous_response_id`) is the real session id, and the
post-v2 policy (full ring with hot appended last; first-skipped, not primary,
cooled on a 2xx fallback; `fell_back` not `is_fallback`), and the v2.2
startup decisions (per-pool shuffle for randomized cold-start fallback order;
`--reserve-bifrost N` to exclude the first N alpha-sorted providers for the
Bifrost auto route; `start_sidecar.cmd` deletes `sidecar.log` on each launch).

## Non-interactive shell

ALWAYS use non-interactive flags to avoid hanging on confirmation prompts:
`cp -f`, `mv -f`, `rm -rf` (not bare `cp`/`mv`/`rm`). For `ssh`/`scp` add
`-o BatchMode=yes`.

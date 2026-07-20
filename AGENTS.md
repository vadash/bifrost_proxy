# Agent Instructions

Bifrost AI Gateway with a Python sidecar that pins sessions to deterministic
providers for prompt-cache locality. Uses **bd (beads)** for issue tracking.

## Repository map

- `sidecar/proxy.py` — stdlib routing proxy (v2, Bifrost-tfz). Listens on :8088, forwards to Bifrost :8080. Pooled models (declared in `sidecar/pools.json`) get session-pinned provider + cooldown routing; non-pooled models pass through verbatim. Logs pooled-only to `sidecar/sidecar.log` + `sidecar/capture.jsonl`.
- `start_sidecar.cmd` — repo-root launcher for the sidecar.
- `start_bifrost.cmd` — launcher for Bifrost itself (npx, port 8080).
- `agent_docs/routing/` — **verified routing mechanics, session-identity derivation, sidecar runbook**. Read [`agent_docs/routing/README.md`](agent_docs/routing/README.md) before touching anything routing-related.

## Routing & architecture knowledge (durable)

The verified Bifrost routing facts and session-identity derivation live in
[`agent_docs/routing/`](agent_docs/routing/README.md) — not in beads, not in
memory. That is the authoritative home; read it first when working on the
sidecar. It covers: how Bifrost alpha-sorts providers, the `provider/model`
prefix trick, the body `fallbacks` array, `routing_info` extraction, and why
`prompt_cache_key` (not `previous_response_id`) is the real session id.

## Non-interactive shell

ALWAYS use non-interactive flags to avoid hanging on confirmation prompts:
`cp -f`, `mv -f`, `rm -rf` (not bare `cp`/`mv`/`rm`). For `ssh`/`scp` add
`-o BatchMode=yes`.

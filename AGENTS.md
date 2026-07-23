# Agent Instructions

Bifrost AI Gateway with a Python sidecar that pins sessions to deterministic
providers for prompt-cache locality. Uses **bd (beads)** for issue tracking.

## Repository map

- `sidecar/` — stdlib routing proxy (v2.2, Bifrost-tfz). `python -m sidecar`; listens :8088 → Bifrost :8080. Pooled models (`sidecar/pools.json`) get session-pinned routing + cooldown; non-pooled pass through verbatim. Decision log `sidecar/sidecar.log` (pooled only; deleted by `start_sidecar.cmd` on launch); `capture.jsonl` only with `--capture` (off by default).
- `start_sidecar.cmd` — repo-root launcher for the sidecar.
- `start_bifrost.cmd` — launcher for Bifrost itself (npx, port 8080).
- `agent_docs/routing/` — **verified routing mechanics, session-identity derivation, sidecar runbook**. Read [`agent_docs/routing/README.md`](agent_docs/routing/README.md) before touching anything routing-related.

## Routing knowledge (durable)

All verified routing mechanics, session-identity derivation, and post-v2
send-order/cooldown policy live in [`agent_docs/routing/`](agent_docs/routing/README.md)
— not in beads, not in memory. Read that index first when touching routing.

## Non-interactive shell

ALWAYS use non-interactive flags to avoid hanging on confirmation prompts:
`cp -f`, `mv -f`, `rm -rf` (not bare `cp`/`mv`/`rm`). For `ssh`/`scp` add
`-o BatchMode=yes`.

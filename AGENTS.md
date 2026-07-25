# Agent Instructions

Bifrost AI Gateway with a Python sidecar that pins sessions to deterministic
providers for prompt-cache locality. Uses **bd (beads)** for issue tracking.

## Repository map

- `sidecar-2/` — stdlib routing proxy. `python -m sidecar-2`; listens :8088 → Bifrost :8080. Pooled models (`sidecar-2/pools.json`) get session-pinned routing + cooldown; serving provider read from the response body by `routing_info.py::extract_provider`. Non-pooled pass through verbatim, except claude/sonnet/opus requests have empty `thinking` blocks stripped, OpenAI `reasoning_effort` rewritten to Bedrock-native `thinking.adaptive` + `output_config.effort`, and `max_completion_tokens` mirrored to `max_tokens` so Bedrock honors the cap (`sidecar-2/sanitize.py`, see `agent_docs/routing/request-sanitization.md`). Decision log `sidecar-2/sidecar.log` (pooled only; deleted by `start_sidecar.cmd` on launch); `capture.jsonl` only with `--capture` (off by default).
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

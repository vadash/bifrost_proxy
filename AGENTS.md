# Agent Instructions

Bifrost AI Gateway with a Python sidecar that pins sessions to deterministic
providers for prompt-cache locality. Uses **bd (beads)** for issue tracking.

## Repository map

- `sidecar/proxy.py` — stdlib passthrough capture proxy (v1, Bifrost-kh0). Listens on :8088, forwards to Bifrost :8080, logs every request to `sidecar/capture.jsonl`.
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

## Beads quick reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work atomically
bd close <id>         # Close completed work
bd remember "..."     # Persistent knowledge (not a substitute for agent_docs)
bd prime              # Refresh beads context
```

Use `bd` for ALL task tracking — do NOT use markdown TODO lists or TodoWrite.
Run `bd prime` for detailed command reference. Architecture in one line:
issues live in a local Dolt DB (`.beads/`); sync uses `refs/dolt/data` on your
git remote; `.beads/issues.jsonl` is a passive export.

## Non-interactive shell

ALWAYS use non-interactive flags to avoid hanging on confirmation prompts:
`cp -f`, `mv -f`, `rm -rf` (not bare `cp`/`mv`/`rm`). For `ssh`/`scp` add
`-o BatchMode=yes`.

## Git policy

Default profile is **conservative**: do not commit, push, or run Dolt sync
unless explicitly asked. At handoff, report changed files, validation, and
suggested next commands. Explicit user instruction overrides this block.

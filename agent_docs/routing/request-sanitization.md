# Request Sanitization (Claude-family fixes)

The sidecar rewrites Claude-family request bodies in two independent cases
beyond pooled routing, both gated on a `claude`/`sonnet`/`opus` model name
and applied to **pooled and passthrough requests alike**.

## Fix 1 — empty `thinking` blocks

### The failure

Anthropic Messages API and AWS Bedrock reject assistant turns whose
`thinking` block has missing or blank `thinking` text:

```
ValidationException: ***.***.***.***.***.thinking: Field required
```

Harnesses persist such blocks when a response aborts mid-thinking
(stream cut before any thinking text, but the block — often with a
`signature` — is already saved to history). Every subsequent request
replays the poisoned history and 400s again; the session is stuck.

### The fix

`sanitize_claude_request(body)` strips, in place, any
`{"type": "thinking", ...}` block whose `thinking` is missing, null, or
whitespace-only. Blocks with real thinking text pass through. An assistant
message left with `content: []` after stripping is dropped entirely (empty
content is itself a 400). Returns the removal count.

## Fix 2 — `reasoning_effort` shape

### The failure

OpenAI-format requests (`/v1/chat/completions`) carrying
`reasoning_effort: <low|medium|high>` get translated by agentrouter to
Bedrock's `thinking.enabled`, which Opus 4.x rejects:

```
ValidationException: "***.***.enabled" is not supported for this model.
Use "***.***.adaptive" and "output_config.effort" to control thinking behavior.
```

### The fix

`rewrite_claude_reasoning_effort(body)` converts the OpenAI field to
Bedrock's native shape **in place**, and returns `True` when it changed
anything:

- Drops `reasoning_effort`.
- Sets `thinking = {"adaptive": True}`.
- Sets `output_config.effort = <original value>`, preserving any other keys
  already on `output_config`.

**Skipped** when the request already has an explicit `thinking` dict — that's
an Anthropic-format request that already specifies thinking config directly
and must be forwarded as-is. Also a no-op when `reasoning_effort` is absent
or non-string.

## Wiring — `sidecar/sanitize.py` -> `proxy.py` step 2a

Both fixes run at `proxy.py` step **2a**, before pooled routing (2b):

- Gate: `model_needs_sanitize(model)` — model name contains `claude`,
  `sonnet`, or `opus` (case-insensitive). Non-Claude models and non-string
  model fields are never touched.
- The proxy runs both rewrites, ORs their "changed" flags, and
  **re-serializes only when something actually changed**, so clean
  passthrough traffic stays byte-verbatim.
- The gate looks at the **original** model name from the client, before
  pooled routing rewrites it to `provider/pool` form.

## Gotchas

- **Signature blocks are stripped too.** A `signature` does not rescue a
  blank `thinking` block — Bedrock rejects it regardless.
- User-role messages are never inspected for Fix 1; only `role: "assistant"`
  content lists are filtered.
- Fix 2 trusts that agentrouter passes `thinking`/`output_config` through to
  Bedrock unchanged. If a future agentrouter release re-translates those
  fields, the rewrite will need to be revisited.

Tests: `python -m unittest sidecar.tests.test_sanitize -v`.

# Request Sanitization (Claude thinking blocks)

The sidecar rewrites request bodies in exactly one case beyond pooled
routing: **empty `thinking` blocks in Claude-family requests**.

## The failure

Anthropic Messages API and AWS Bedrock reject assistant turns whose
`thinking` block has missing or blank `thinking` text:

```
ValidationException: ***.***.***.***.***.thinking: Field required
```

Harnesses persist such blocks when a response aborts mid-thinking
(stream cut before any thinking text, but the block — often with a
`signature` — is already saved to history). Every subsequent request
replays the poisoned history and 400s again; the session is stuck.

## The fix — `sidecar/sanitize.py`

Wired in `proxy.py` step **2a**, before pooled routing (2b), so it applies
to **pooled and passthrough requests alike**:

- Gate: `model_needs_sanitize(model)` — model name contains `claude`,
  `sonnet`, or `opus` (case-insensitive). Non-Claude models and non-string
  model fields are never touched.
- `sanitize_claude_request(body)` strips, in place, any
  `{"type": "thinking", ...}` block whose `thinking` is missing, null, or
  whitespace-only. Blocks with real thinking text pass through.
- An assistant message left with `content: []` after stripping is dropped
  entirely (empty content is itself a 400).
- Returns the removal count; the proxy **re-serializes only when > 0**,
  so clean passthrough traffic stays byte-verbatim.

## Gotchas

- **Signature blocks are stripped too.** A `signature` does not rescue a
  blank `thinking` block — Bedrock rejects it regardless.
- User-role messages are never inspected; only `role: "assistant"` content
  lists are filtered.
- The sanitize gate looks at the **original** model name from the client,
  before pooled routing rewrites it to `provider/pool` form.

Tests: `python -m unittest sidecar.tests.test_sanitize -v`.

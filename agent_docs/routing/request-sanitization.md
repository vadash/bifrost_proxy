# Request Sanitization

The sidecar rewrites request bodies beyond pooled routing. One fix runs
**unconditionally for every request** (all models, all providers); three
further fixes are gated on a `claude`/`sonnet`/`opus` model name and apply to
pooled and passthrough requests alike.

## Fix 0 — `max_tokens` / `max_completion_tokens` cap (unconditional)

### The failure

Some providers reject or silently truncate very large output budgets, and a
missing budget falls back to a provider default that can be far below what
the client needs. To keep behavior uniform, every request gets its output
token budget clamped to a hard ceiling.

### The fix

`cap_max_tokens(body)` runs for **every** request regardless of model or
upstream provider, in place:

- For each of `max_tokens` (Anthropic) and `max_completion_tokens` (OpenAI):
  if the field is missing, non-int, or greater than `_MAX_TOKENS_CAP` (16000),
  it is set to `16000`. Below-cap explicit values are left untouched.
- Both fields are always set to a (possibly equal) value after the cap runs,
  so every upstream variation reads the real budget.
- The cap runs *before* Fix 3 (`mirror_max_tokens`); a capped `max_tokens` is
  therefore already present, making the mirror a no-op — the desired end state.

Returns `True` when the body was modified.

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

`strip_empty_thinking(body)` strips, in place, any
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

`rewrite_reasoning_effort(body)` converts the OpenAI field to
Bedrock's native shape **in place**, and returns `True` when it changed
anything:

- Drops `reasoning_effort`.
- Sets `thinking = {"adaptive": True}`.
- Sets `output_config.effort = <original value>`, preserving any other keys
  already on `output_config`.

**Skipped** when the request already carries an explicit `thinking` dict —
that's an Anthropic-format request that already specifies thinking config
directly and must be forwarded as-is. The gate is *any* dict, including an
empty `{}`: an explicit `thinking: {}` counts as a deliberate client choice
and passes through untouched (the gate is `isinstance(body.get("thinking"),
dict)`, NOT `... and body["thinking"]` — a truthiness check would clobber
`{}` and contradict this contract). Also a no-op when `reasoning_effort` is
absent or non-string.

## Fix 3 — `max_tokens` mirroring

### The failure

Bifrost shows `max_completion_tokens=64000` in the request, but the response
comes back with `output_tokens=8192`, `stop_reason=max_tokens`. The
OpenAI-format body carries `max_completion_tokens` only (no `max_tokens`).
Bedrock's Anthropic Messages API reads `max_tokens` (not
`max_completion_tokens`); agentrouter doesn't translate the OpenAI field
name, so Bedrock falls back to its 8192 default and the model dies there
regardless of what the client asked for.

### The fix

`mirror_max_tokens(body)` mirrors the value under the Bedrock-native
field name, in place:

- Sets `max_tokens = max_completion_tokens` (preserving the original
  OpenAI field too, in case an upstream still consults it).
- **Skipped** when `max_tokens` is already set (explicit Anthropic-format
  request) or when `max_completion_tokens` is missing/non-int.

Returns `True` when the body was modified.

## Wiring — `sidecar-2/sanitize.py` -> `proxy.py` step 2a

The fixes run at `proxy.py` step **2a**, before pooled routing (2b), inside
the handler's `_sanitize_body(parsed)`:

- **Fix 0 (`cap_max_tokens`)** runs unconditionally for every parsed dict
  request — no model gate. It mutates `parsed` in place; since both the
  passthrough and the pooled/fast body rewrites serialize from `parsed`, the
  cap propagates to every path.
- **Fixes 1–3** run only when `model_needs_sanitize(model)` is true (model
  name contains `claude`, `sonnet`, or `opus`, case-insensitive). The gate
  sees the **original** client model name, before pooled routing rewrites it
  to `provider/pool` form.
- `_sanitize_body` ORs the "changed" flags from the cap and
  `sanitize_request_body` and **re-serializes only when something actually
  changed** (returns the new `bytes`, or `None` to leave clean passthrough
  byte-verbatim).

## Gotchas

- **Signature blocks are stripped too.** A `signature` does not rescue a
  blank `thinking` block — Bedrock rejects it regardless.
- User-role messages are never inspected for Fix 1; only `role: "assistant"`
  content lists are filtered.
- Fix 2 trusts that agentrouter passes `thinking`/`output_config` through to
  Bedrock unchanged. If a future agentrouter release re-translates those
  fields, the rewrite will need to be revisited.

Tests: `python -m unittest discover -s sidecar-2.tests -v`.

## Fix 4: DeepSeek-V4-Flash-Vision-Exp stream downgrade + SSE replay

The vision-exp preview server's streaming tool-call parser intermittently
emits tool-call arguments wrapped one level too deep: the accumulated
`tool_calls[].function.arguments` string parses to `{"arguments": {...}}`
(or carries a spurious `arguments` key beside the real fields) once the
conversation history contains a prior assistant tool call. Strict client
validators then reject every follow-up tool call. Non-streaming responses
from the same server are unaffected, and the sibling
`DeepSeek-V4-Flash-0731` does not exhibit the bug.

Because the corruption only becomes visible after the client accumulates the
full argument stream, it cannot be patched per-chunk. The sidecar therefore
downgrades the request instead:

1. `sanitize.py::downgrade_stream` — for model names containing
   `deepseek-v4-flash-vision-exp` (case-insensitive) on a `/chat/completions`
   path with `stream: true`: force `stream: false`, drop `stream_options`
   (`include_usage` is read by the proxy BEFORE the pop).
2. Upstream returns a normal buffered `chat.completion`. The wrap can still
   appear here on large conversations.
3. `sanitize.py::completion_to_sse` repairs each tool_call
   (`unwrap_tool_args`: a spurious `arguments` key holding the real
   fields — sole-key or beside other fields — is flattened; anything else
   passes verbatim), then replays the completion as synthesized OpenAI SSE
   (`text/event-stream`): role+content chunk, `reasoning_content` chunk
   (when present), one chunk per complete tool_call, finish chunk, optional
   trailing usage chunk (when the client asked `include_usage`), then
   `data: [DONE]`. Client-side accumulation sees a well-formed stream with
   flat arguments.

Scope guards: only OpenAI-format `/chat/completions` requests; a non-200 or
unparseable upstream body falls back to verbatim relay. Anthropic-format
`/v1/messages` passthrough is untouched (replay would emit the wrong event
shape). `/fast` never triggers: the downgrade only fires in
`_sanitize_body`, and vision-exp is not a pooled model.

Cost: TTFT for this one model becomes time-to-full-response (the sidecar
buffers the whole completion before the first byte). That is the trade —
correct tool calls beat incremental display.

Tests: `sidecar-2/tests/test_sanitize.py::TestStreamDowngrade` and
`TestCompletionToSse`. A live-API repro harness lives at `C:/tmp/dsbug_loop.py`
(not CI — it hits the real gateway).

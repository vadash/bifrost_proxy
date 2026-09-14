"""Request-body sanitization for provider 400 rejections.

Three independent fixes, all gated on a Claude-family model name (claude /
sonnet / opus, case-insensitive):

1. **Empty thinking blocks.** Anthropic Messages API + AWS Bedrock reject
   assistant turns that contain ``thinking`` blocks with empty or missing
   ``thinking`` text (``ValidationException: ...thinking: Field required``).
   Harnesses that stream responses often persist such blocks when a request
   aborts mid-thinking, then replay them forever — every retry 400s.
   ``strip_empty_thinking`` strips those degenerate blocks.

2. **reasoning_effort shape.** OpenAI-format requests carrying
   ``reasoning_effort`` get translated by agentrouter to Bedrock's
   ``thinking.enabled``, which Opus 4.x rejects (``"thinking.enabled" is not
   supported for this model. Use thinking.adaptive and output_config.effort``).
   ``rewrite_reasoning_effort`` converts the OpenAI field to Bedrock's
   native ``thinking={adaptive: true}`` + ``output_config.effort``.

3. **max_tokens mirroring.** Bedrock's Anthropic Messages API reads
   ``max_tokens``, not OpenAI's ``max_completion_tokens``; agentrouter
   doesn't translate the field, so requests cap at Bedrock's 8192 default
   (``stop_reason: max_tokens`` at exactly 8192 output tokens).
   ``mirror_max_tokens`` copies the value under the Bedrock-native name.

``cap_max_tokens`` runs unconditionally (every request, every model): when
``max_tokens`` (Anthropic) or ``max_completion_tokens`` (OpenAI) is missing or
exceeds ``_MAX_TOKENS_CAP`` both fields are forced to the cap so every upstream
variation reads the same budget. The three fixes below are then gated on a
Claude-family model name. All rewriters are deterministic and in-place
(mutating the parsed dict matches the proxy's pooled-rewrite pattern), and
only act when a rewrite is actually needed.
"""

from __future__ import annotations

import json

# Lowercase substrings matched against the request's model name.
_CLAUDE_MODEL_HINTS: tuple[str, ...] = ("claude", "sonnet", "opus")

# Hard ceiling applied to max_tokens / max_completion_tokens for every
# request regardless of model or upstream provider (see ``cap_max_tokens``).
_MAX_TOKENS_CAP: int = 16000


def model_needs_sanitize(model: object) -> bool:
    """True when the model name looks like a Claude-family model.

    Non-string models (missing key, null, numbers) never match — malformed
    requests are forwarded verbatim so the real validation error surfaces.
    """
    if not isinstance(model, str):
        return False
    low = model.lower()
    return any(hint in low for hint in _CLAUDE_MODEL_HINTS)


def _is_empty_thinking(block: object) -> bool:
    """True for a thinking block with missing or blank thinking text."""
    if not isinstance(block, dict) or block.get("type") != "thinking":
        return False
    text = block.get("thinking")
    return not (isinstance(text, str) and text.strip())


def strip_empty_thinking(body: dict) -> int:
    """Strip empty ``thinking`` blocks from assistant messages. In-place.

    Returns the number of blocks removed (0 = body already clean; callers
    use this to skip re-serialization). Assistant messages whose content
    becomes empty after stripping are dropped entirely — an empty
    ``content: []`` would itself be rejected by the API.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0

    removed = 0
    new_messages: list = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            new_messages.append(msg)
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            new_messages.append(msg)
            continue

        kept = [b for b in content if not _is_empty_thinking(b)]
        stripped = len(content) - len(kept)
        if stripped:
            removed += stripped
            if kept:
                msg["content"] = kept
                new_messages.append(msg)
            # else: content would be [] — drop the message wholesale.
        else:
            new_messages.append(msg)

    if removed:
        body["messages"] = new_messages
    return removed


def rewrite_reasoning_effort(body: dict) -> bool:
    """Convert OpenAI ``reasoning_effort`` to Bedrock-native thinking shape.

    Agentrouter translates a top-level ``reasoning_effort`` to Bedrock's
    ``thinking.enabled``, which Opus 4.x rejects with ``"thinking.enabled" is
    not supported for this model``. Bedrock wants ``thinking.adaptive`` +
    ``output_config.effort`` instead. Rewrite accordingly and drop the
    original OpenAI field so the upstream translator has nothing to misrender.

    Skips when the request already carries an explicit ``thinking`` dict —
    that's an Anthropic-format request that already specifies thinking config
    directly and should be forwarded as-is.

    Returns True when the body was modified (caller re-serializes).
    """
    effort = body.get("reasoning_effort")
    if not isinstance(effort, str):
        return False
    # Any explicit ``thinking`` dict -- even ``{}`` -- means the client specified
    # thinking config directly; forward as-is.
    if isinstance(body.get("thinking"), dict):
        return False

    body.pop("reasoning_effort", None)
    body["thinking"] = {"adaptive": True}
    out_cfg = body.get("output_config")
    if not isinstance(out_cfg, dict):
        out_cfg = {}
    out_cfg["effort"] = effort
    body["output_config"] = out_cfg
    return True


def mirror_max_tokens(body: dict) -> bool:
    """Copy ``max_completion_tokens`` -> ``max_tokens`` for Bedrock upstreams.

    Bedrock's Anthropic Messages API reads ``max_tokens`` (not OpenAI's
    ``max_completion_tokens``). When agentrouter translates an OpenAI-format
    request, it appears not to honor ``max_completion_tokens``, so Bedrock
    falls back to its 8192 default and the model dies with
    ``stop_reason: max_tokens`` at exactly 8192 output tokens regardless of
    what the client asked for. Mirroring the value under the Bedrock-native
    field name gets the real cap through.

    No-op when ``max_tokens`` is already set (explicit Anthropic-format
    request) or when ``max_completion_tokens`` is missing/non-int.

    Returns True when the body was modified (caller re-serializes).
    """
    if "max_tokens" in body:
        return False
    mct = body.get("max_completion_tokens")
    if not isinstance(mct, int) or isinstance(mct, bool):
        return False
    body["max_tokens"] = mct
    return True


def cap_max_tokens(body: dict) -> bool:
    """Cap ``max_tokens`` / ``max_completion_tokens`` to a fixed ceiling.

    Some providers reject or silently truncate very large output budgets, and
    missing budgets fall back to provider defaults that can be far below what
    the client needs. To keep behavior uniform across all models and
    providers, any of ``max_tokens`` (Anthropic) or ``max_completion_tokens``
    (OpenAI) that is missing or greater than ``_MAX_TOKENS_CAP`` is forced to
    the cap; both fields are then set to the same (capped) value so every
    upstream variation reads the real budget.

    Runs unconditionally (not gated on a Claude-family model) and runs *before*
    the Claude-only ``mirror_max_tokens`` — so a capped ``max_tokens`` is
    already present and mirroring becomes a no-op, which is exactly the
    desired end state.

    Non-dict inputs (raw non-JSON bodies) are a no-op and return False.

    Returns True when the body was modified (caller re-serializes).
    """
    if not isinstance(body, dict):
        return False
    changed = False
    for field in ("max_tokens", "max_completion_tokens"):
        current = body.get(field)
        if not isinstance(current, int) or isinstance(current, bool) or current > _MAX_TOKENS_CAP:
            if current != _MAX_TOKENS_CAP:
                body[field] = _MAX_TOKENS_CAP
                changed = True
        # Below-cap explicit values are left untouched.
    return changed


def sanitize_request_body(parsed: dict) -> bytes | None:
    """Run all body-rewrite fixes and return the re-serialized body if any applied.

    Composes the three in-place rewriters (``strip_empty_thinking``,
    ``rewrite_reasoning_effort``, ``mirror_max_tokens``) — the same sequence
    the proxy used to inline. Runs only when the parsed model is a
    Claude-family name (per ``model_needs_sanitize``); other models are
    forwarded verbatim.

    Returns the re-serialized ``bytes`` iff at least one rewrite touched the
    body, else ``None`` — the ``None`` signal lets the caller leave a clean
    passthrough byte-verbatim instead of needlessly re-serializing.
    """
    if not model_needs_sanitize(parsed.get("model")):
        return None
    changed = strip_empty_thinking(parsed) > 0
    changed = rewrite_reasoning_effort(parsed) or changed
    changed = mirror_max_tokens(parsed) or changed
    if not changed:
        return None

    return json.dumps(parsed).encode("utf-8")


# --- DeepSeek-V4-Flash-Vision-Exp streaming downgrade -----------------------
#
# The vision-exp preview server's streaming tool-call parser intermittently
# emits tool-call arguments wrapped one level too deep — the accumulated
# arguments string parses to ``{"arguments": {...}}`` (or carries a spurious
# ``arguments`` key beside the real fields) after any prior assistant tool
# call in the conversation. Non-streaming responses are unaffected, so for
# this model the proxy downgrades ``stream: true`` requests to non-streaming
# upstream and replays the finished completion back to the client as
# synthesized OpenAI SSE (``completion_to_sse``).

# Lowercase substrings matched against the request's model name.
_DOWNGRADE_MODEL_HINTS: tuple[str, ...] = ("deepseek-v4-flash-vision-exp",)


def model_needs_stream_downgrade(model: object) -> bool:
    """True when the model name looks like the vision-exp preview model."""
    low = str(model).lower() if isinstance(model, str) else ""
    return any(hint in low for hint in _DOWNGRADE_MODEL_HINTS)


def downgrade_stream(body: dict) -> bool:
    """Force ``stream: false`` for the vision-exp model. In-place.

    Also drops ``stream_options`` (meaningless upstream once non-streaming);
    the caller reads ``include_usage`` off the body BEFORE calling this when
    it needs to replay a trailing usage chunk.

    Returns True when the body was modified (caller re-serializes).
    """
    if not isinstance(body, dict) or not body.get("stream"):
        return False
    if not model_needs_stream_downgrade(body.get("model")):
        return False
    body["stream"] = False
    body.pop("stream_options", None)
    return True


def completion_to_sse(resp_body: bytes, include_usage: bool) -> bytes | None:
    """Replay a buffered non-streaming ``chat.completion`` as OpenAI SSE.

    One chunk per logical piece (role+content, ``reasoning_content``,
    each complete tool_call, finish_reason), an optional trailing usage
    chunk, then ``data: [DONE]``. Clients accumulate deltas exactly as they
    would from a native stream.

    Returns ``None`` when ``resp_body`` is not a parseable completion with
    choices — the caller then relays the body verbatim instead.
    """
    try:
        completion = json.loads(resp_body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(completion, dict) or not completion.get("choices"):
        return None

    base = {
        "id": completion.get("id"),
        "object": "chat.completion.chunk",
        "created": completion.get("created"),
        "model": completion.get("model"),
    }


    out = bytearray()
    for choice in completion["choices"]:
        idx = choice.get("index", 0)
        msg = choice.get("message") or {}

        def emit_choice(delta, finish_reason=None) -> None:
            event = {**base, "choices": [
                {"index": idx, "delta": delta, "finish_reason": finish_reason}
            ]}
            out.extend(b"data: " + json.dumps(event).encode("utf-8") + b"\n\n")

        reasoning = msg.get("reasoning_content")
        if reasoning:
            emit_choice({"role": "assistant", "reasoning_content": reasoning})
        emit_choice({"role": "assistant", "content": msg.get("content") or ""})
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            emit_choice({"tool_calls": [{
                "id": tc.get("id"),
                "index": i,
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", ""),
                },
            }]})
        emit_choice({}, choice.get("finish_reason"))
    if include_usage and completion.get("usage"):
        event = {**base, "choices": [], "usage": completion["usage"]}
        out.extend(b"data: " + json.dumps(event).encode("utf-8") + b"\n\n")
    out.extend(b"data: [DONE]\n\n")
    return bytes(out)

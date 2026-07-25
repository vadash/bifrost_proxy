"""Request-body sanitization for provider 400 rejections.

Two independent fixes, both gated on a Claude-family model name (claude /
sonnet / opus, case-insensitive):

1. **Empty thinking blocks.** Anthropic Messages API + AWS Bedrock reject
   assistant turns that contain ``thinking`` blocks with empty or missing
   ``thinking`` text (``ValidationException: ...thinking: Field required``).
   Harnesses that stream responses often persist such blocks when a request
   aborts mid-thinking, then replay them forever — every retry 400s.
   ``sanitize_claude_request`` strips those degenerate blocks.

2. **reasoning_effort shape.** OpenAI-format requests carrying
   ``reasoning_effort`` get translated by agentrouter to Bedrock's
   ``thinking.enabled``, which Opus 4.x rejects (``"thinking.enabled" is not
   supported for this model. Use thinking.adaptive and output_config.effort``).
   ``rewrite_claude_reasoning_effort`` converts the OpenAI field to Bedrock's
   native ``thinking={adaptive: true}`` + ``output_config.effort``.

Both are deterministic, in-place (mutating the parsed dict matches the proxy's
pooled-rewrite pattern), and only act when a rewrite is actually needed.
"""

from __future__ import annotations

# Lowercase substrings matched against the request's model name.
_CLAUDE_MODEL_HINTS: tuple[str, ...] = ("claude", "sonnet", "opus")


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


def sanitize_claude_request(body: dict) -> int:
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


def rewrite_claude_reasoning_effort(body: dict) -> bool:
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
    existing = body.get("thinking")
    if isinstance(existing, dict) and existing:
        return False

    body.pop("reasoning_effort", None)
    body["thinking"] = {"adaptive": True}
    out_cfg = body.get("output_config")
    if not isinstance(out_cfg, dict):
        out_cfg = {}
    out_cfg["effort"] = effort
    body["output_config"] = out_cfg
    return True

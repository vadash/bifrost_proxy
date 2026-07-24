"""Request-body sanitization for provider 400 rejections.

Claude-family models (Anthropic Messages API + AWS Bedrock) reject assistant
turns that contain ``thinking`` blocks with empty or missing ``thinking``
text: Bedrock raises ``ValidationException: ...thinking: Field required``.
Harnesses that stream responses often persist such blocks when a request
aborts mid-thinking, then replay them forever — every retry 400s.

This module strips those degenerate blocks for any request whose model name
mentions claude/sonnet/opus (case-insensitive). Blocks with real thinking
text are preserved; other content (text, tool_use, tool_result) is untouched.

Deterministic, in-place (mutates the parsed dict, matching the proxy's pooled
rewrite pattern), and only called when a rewrite is actually needed.
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

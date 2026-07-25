"""Session-identity derivation (pure functions, no global state).

The cascade resolves which "session" a request belongs to so the router can
pin it to one provider for prompt-cache locality. See
``agent_docs/routing/session-identity.md`` for the verified rationale.

"""

from __future__ import annotations

import hashlib
from typing import Any


def _first_user_str(content) -> str:
    """Join ``text`` fields across a list of content parts (OpenAI shapes)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                parts.append(str(part["text"]))
        if parts:
            return "".join(parts)
    return ""


def first_user_text(body: dict) -> str:
    """Extract the first user-turn text for the hash fallback.

    Handles both ``/v1/responses`` (``input``) and ``/v1/chat/completions``
    (``messages``). Returns ``""`` when no user text is present.
    """
    # /v1/responses: body["input"]
    inp = body.get("input")
    if isinstance(inp, str):
        return inp
    if isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            text = _first_user_str(item.get("content"))
            if text:
                return text

    # /v1/chat/completions: body["messages"]
    for msg in body.get("messages", []) or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _first_user_str(msg.get("content"))
        if text:
            return text
    return ""


def derive_session_key(body: dict) -> tuple[str, str]:
    """Return ``(session_key, source)`` via the cascade (first match wins):

    1. ``prompt_cache_key`` truthy -> (str(that), "cache_key")
    2. hash fallback -> ("h:" + sha256(instructions + "\\n" + first_user_text)[:32], "hash")
    """
    cache_key = body.get("prompt_cache_key")
    if cache_key:
        return (str(cache_key), "cache_key")

    instructions = body.get("instructions") or ""
    text = first_user_text(body)
    digest = hashlib.sha256(
        (instructions + "\n" + text).encode("utf-8")
    ).hexdigest()[:32]
    return ("h:" + digest, "hash")

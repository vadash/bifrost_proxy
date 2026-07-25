"""Serving-provider extraction from Bifrost response bodies (pure).

On the deployed Bifrost build, routing identity arrives in the BODY only, at
top-level ``extra_fields.routing_info`` -- in the terminal
``response.completed`` SSE event for ``/v1/responses`` streams, in the
penultimate (usage-carrying) chunk for ``/v1/chat/completions`` streams, and
at the body top level for non-streaming responses. No routing headers exist
on this build, and ``is_fallback``/``primary_provider`` are never emitted, so
the serving provider is the only routing signal available.

"No provider seen" is a NORMAL outcome (client disconnect / stream stall
before the terminal event), not an error -- callers treat ``None`` as "skip
fallback feedback".
"""

from __future__ import annotations

import json
from typing import Any


def _provider_of(parsed: Any) -> str | None:
    """Return ``extra_fields.routing_info.provider`` if a non-empty string."""
    if not isinstance(parsed, dict):
        return None
    routing_info = parsed.get("extra_fields")
    if not isinstance(routing_info, dict):
        return None
    routing_info = routing_info.get("routing_info")
    if not isinstance(routing_info, dict):
        return None
    provider = routing_info.get("provider")
    return provider if isinstance(provider, str) and provider else None


def extract_provider(body_bytes: bytes, *, is_stream: bool) -> str | None:
    """Return the provider that served the response, or None if not seen.

    Non-stream: parse the whole body once. Stream: walk SSE events in REVERSE
    (terminal ``response.completed`` / usage chunk found first) and return the
    first ``data:`` payload carrying a provider. All parse errors are
    swallowed per-object.
    """
    text = body_bytes.decode("utf-8", "replace")
    if not is_stream:
        try:
            return _provider_of(json.loads(text))
        except Exception:
            return None
    for event in reversed(text.split("\n\n")):
        for line in event.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                provider = _provider_of(json.loads(payload))
            except Exception:
                continue
            if provider is not None:
                return provider
    return None

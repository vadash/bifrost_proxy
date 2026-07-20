"""Response-metadata extraction from Bifrost upstream responses (pure).

Two shapes, both returning ``(routing_info, response_id)`` or ``(None, None)``:

* non-streaming JSON body -> ``extra_fields.routing_info`` + top-level ``id``;
* streaming SSE buffer    -> walk events in reverse, take the first
  ``response.completed``/``response.incomplete`` ``data:`` payload that
  yields a non-None ``routing_info``.
"""

from __future__ import annotations

import json
from typing import Any


def parse_response_meta_nonstream(resp_body: bytes) -> tuple[Any, Any]:
    """Extract ``(routing_info, response_id)`` from a non-streaming Bifrost
    response. ``routing_info`` from ``data.extra_fields.routing_info``;
    ``response_id`` from ``data.id``. Returns ``(None, None)`` on any
    exception or missing field.
    """
    try:
        data = json.loads(resp_body)
        routing_info = data.get("extra_fields", {}).get("routing_info")
        response_id = data.get("id")
        return (routing_info, response_id)
    except Exception:
        return (None, None)


def parse_response_meta_stream(sse_buf: bytes) -> tuple[Any, Any]:
    """Extract ``(routing_info, response_id)`` from a streaming SSE Bifrost
    response. Walk events in REVERSE; the terminal
    ``response.completed``/``response.incomplete`` event's JSON ``data:``
    payload carries ``extra_fields.routing_info`` and the id (``payload.id``
    or ``payload.response.id``). Return the first event that yields a
    non-None ``routing_info``, paired with whatever id that same event
    carried. Returns ``(None, None)`` on any exception or missing field.
    """
    try:
        text = sse_buf.decode("utf-8", "replace")
        # Split into SSE events (blank-line delimited).
        events = text.split("\n\n")
        for event in reversed(events):
            for line in event.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload:
                    continue
                try:
                    parsed = json.loads(payload)
                except Exception:
                    continue
                ri = parsed.get("extra_fields", {}).get("routing_info")
                if ri is not None:
                    rid = parsed.get("id")
                    if rid is None:
                        rid = parsed.get("response", {}).get("id")
                    return (ri, rid)
        return (None, None)
    except Exception:
        return (None, None)

"""Pure status/content-type predicates shared across routing helpers.

No globals, no state; the only place these two checks live so a future
provider shape change (e.g. a 3xx-as-success variant) edits one site.
"""

from __future__ import annotations


def is_2xx(status: int | None) -> bool:
    """True iff ``status`` is a real 2xx (None is not)."""
    return status is not None and 200 <= status < 300


def is_sse_content_type(value: str) -> bool:
    """True iff the Content-Type header declares an SSE stream."""
    return "text/event-stream" in value.lower()

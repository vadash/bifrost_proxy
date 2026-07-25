"""IO primitives: header redaction, request-body parsing, thread-safe JSONL appenders.

These are the only places that touch the filesystem for capture/log writes,
so the rest of the code never opens a file handle. Each ``JsonlWriter`` owns
its own lock -- replacing the old single ``CAPTURE_LOCK`` that ambiguously
guarded two different paths.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from .config import REDACT_HEADERS


def redact_headers(items) -> dict[str, Any]:
    """Return a dict of headers with sensitive values replaced with 'REDACTED'.

    Comparison is case-insensitive on the header name. ``items`` is any
    iterable of ``(name, value)`` pairs (e.g. ``self.headers.items()``).
    """
    out: dict[str, Any] = {}
    for name, value in items:
        if name.lower() in REDACT_HEADERS:
            out[name] = "REDACTED"
        else:
            out[name] = value
    return out


def parse_request_body(raw: bytes) -> Any:
    """Parse a request body to JSON if possible, else a truncated raw string.

    Parsed JSON is kept whole (so model/messages/instructions/previous_response_id
    are all visible); non-JSON bodies are truncated to 4 KB.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw.decode("utf-8", "replace")[:4096]


class JsonlWriter:
    """Thread-safe appender of JSON lines to a single path.

    One writer per file (capture vs. decision-log); each holds its own lock so
    concurrent writes to different files don't serialise against each other.
    """

    __slots__ = ("_path", "_lock")

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        """Append one JSON line to this writer's path."""
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()

    def safe(self, record: dict) -> None:
        """Like ``write`` but never raises -- best-effort logging."""
        try:
            self.write(record)
        except Exception:
            pass

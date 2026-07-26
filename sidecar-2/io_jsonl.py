"""IO primitives: header redaction, request-body parsing, thread-safe JSONL appenders.

These are the only places that touch the filesystem for capture/log writes,
so the rest of the code never opens a file handle. Each ``JsonlWriter`` owns
its own lock -- replacing the old single ``CAPTURE_LOCK`` that ambiguously
guarded two different paths.

A ``JsonlWriter`` holds a *persistent* append handle for its lifetime
(lazily opened on first write). ``write()`` flushes after every record, so a
process kill still leaves a complete line on disk; an optional
``_FsyncScheduler`` daemon thread calls ``os.fsync`` on all live writers
every ``FSYNC_INTERVAL_SECS`` so OS-buffered pages survive a power-loss
event without forcing a synchronous syscall per record.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from .config import FSYNC_INTERVAL_SECS, REDACT_HEADERS


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


class _FsyncScheduler:
    """Singleton daemon thread that periodically ``os.fsync``s live writers.

    Started lazily on first writer registration so importing ``io_jsonl``
    never spawns a thread (matters for tooling/tests). Each tick walks the
    live-writer set under each writer's own lock; errors are swallowed --
    fsync is best-effort, the per-record ``flush()`` already guarantees
    process-kill durability.
    """

    def __init__(self) -> None:
        self._writers: set[JsonlWriter] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def register(self, writer: JsonlWriter) -> None:
        if FSYNC_INTERVAL_SECS <= 0:
            return
        with self._lock:
            self._writers.add(writer)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="sidecar-fsync", daemon=True
                )
                self._thread.start()

    def unregister(self, writer: JsonlWriter) -> None:
        with self._lock:
            self._writers.discard(writer)

    def _run(self) -> None:
        while True:
            threading.Event().wait(FSYNC_INTERVAL_SECS)
            with self._lock:
                writers = list(self._writers)
            for w in writers:
                with w._lock:
                    fh = w._fh
                    if fh is None or fh.closed:
                        continue
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        # Closed/redirected handle, or platform that rejects
                        # fsync on this fd -- best-effort; per-record flush
                        # already persists the line. Do not spam logs.
                        pass


# Module-level singleton. Construction is cheap; the thread is the lazy bit.
_scheduler = _FsyncScheduler()


class JsonlWriter:
    """Thread-safe appender of JSON lines to a single path.

    One writer per file (capture vs. decision-log); each holds its own lock so
    concurrent writes to different files don't serialise against each other.
    Holds a persistent append handle for its lifetime (lazily opened on first
    write) so hot pooled paths don't pay an open()+close() syscall pair per
    record.
    """

    __slots__ = ("_path", "_lock", "_fh")

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._fh = None
        _scheduler.register(self)

    def _ensure_open(self) -> None:
        # Called under self._lock.
        if self._fh is None or self._fh.closed:
            self._fh = open(self._path, "a", encoding="utf-8")

    def write(self, record: dict) -> None:
        """Append one JSON line to this writer's path."""
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            self._ensure_open()
            self._fh.write(line)
            self._fh.flush()

    def safe(self, record: dict) -> None:
        """Like ``write`` but never raises -- best-effort logging."""
        try:
            self.write(record)
        except Exception:
            pass

    def close(self) -> None:
        """Flush and release the persistent handle. Idempotent."""
        with self._lock:
            if self._fh is not None and not self._fh.closed:
                try:
                    self._fh.flush()
                finally:
                    self._fh.close()
            self._fh = None
        _scheduler.unregister(self)

    def __del__(self) -> None:
        # Backstop for callers/tests that forget close(). Never raise from a
        # finalizer; swallow everything. Does not replace an explicit close().
        try:
            self.close()
        except Exception:
            pass

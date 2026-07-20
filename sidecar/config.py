"""Configuration and paths for the sidecar.

All paths, timeouts, and tunables live here as a single immutable
``SidecarConfig`` value object (SRP: this module knows nothing about routing
or HTTP). ``load_pools`` is co-located because it reads the pools path that
the config owns.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

_DIRNAME = os.path.dirname(os.path.abspath(__file__))

# Hop-by-hop headers per RFC 7230 §6.1 -- never forwarded end-to-end.
HOP_BY_HOP: frozenset[str] = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
})

# Request-header names whose values are redacted in capture.jsonl.
REDACT_HEADERS: frozenset[str] = frozenset({
    "authorization",
    "x-api-key",
    "apikey",
    "api-key",
})


@dataclass(frozen=True, slots=True)
class SidecarConfig:
    """Resolved configuration for one sidecar instance.

    Immutable so the handler thread can safely share a single reference.
    """

    listen_host: str = "127.0.0.1"
    listen_port: int = 8088
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 8080

    pools_path: str = os.path.join(_DIRNAME, "pools.json")
    log_path: str = os.path.join(_DIRNAME, "sidecar.log")
    capture_path: str = os.path.join(_DIRNAME, "capture.jsonl")
    capture_enabled: bool = False

    session_ttl: float = 3600.0   # inactivity TTL for pins / resp-id map (s)
    default_cooldown: float = 600.0  # provider cooldown duration (s)
    upstream_timeout: float = 600.0  # per-upstream request timeout (s)
    chunk_size: int = 8192         # stream relay chunk size (bytes)

    pools: dict[str, list[str]] = field(default_factory=dict)

    @property
    def upstream_addr(self) -> tuple[str, int]:
        return (self.upstream_host, self.upstream_port)

    @property
    def listen_addr(self) -> tuple[str, int]:
        return (self.listen_host, self.listen_port)


def load_pools(path: str) -> dict[str, list[str]]:
    """Read+parse ``pools.json``.

    On missing file or parse error, print a ``[sidecar] WARNING: ...`` line to
    stdout and return ``{}`` (pure passthrough, no pooled models).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data: Any = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("pools.json top-level must be a JSON object")
        return data
    except FileNotFoundError:
        print(f"[sidecar] WARNING: pools.json not found at {path} -> passthrough only")
        return {}
    except Exception as e:
        print(f"[sidecar] WARNING: pools.json parse error ({e!r}) -> passthrough only")
        return {}

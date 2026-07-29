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

# JsonlWriter fsync cadence (s). Timer-driven fsync is best-effort; per-record
# flush() already guarantees process-kill durability. 0 disables the scheduler.
FSYNC_INTERVAL_SECS: float = 5.0
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

    # Number of alpha-first providers to reserve for the Bifrost auto route
    # (excluded from sidecar pooling entirely). 0 = no reservation.
    reserve_bifrost: int = 0

    @property
    def upstream_addr(self) -> tuple[str, int]:
        return (self.upstream_host, self.upstream_port)

    @property
    def listen_addr(self) -> tuple[str, int]:
        return (self.listen_host, self.listen_port)


def load_pools(path: str, reserve_bifrost: int = 0) -> dict[str, list[str]]:
    """Read+parse ``pools.json``.

    On missing file or parse error, print a ``[sidecar] WARNING: ...`` line to
    stdout and return ``{}`` (pure passthrough, no pooled models).

    When ``reserve_bifrost > 0``, the FIRST pool (the first ``pools.json``
    entry) drops up to ``min(1, reserve_bifrost)`` alpha-sorted providers —
    reserving them for the Bifrost auto route (which alpha-sorts the same
    way so the sidecar never reaches them). The reservation is capped at 1
    so a pool is never left below its declared size minus 1; pools that
    would be emptied are skipped entirely. Only the first pool reserves,
    every other pool keeps its full provider list (so adding a second tier
    with a smaller provider list like the 2-provider ``kilo-auto/free``
    never has its providers stolen by the reservation). A notice is printed
    per pool when any providers are dropped.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data: Any = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("pools.json top-level must be a JSON object")
    except FileNotFoundError:
        print(f"[sidecar] WARNING: pools.json not found at {path} -> passthrough only")
        return {}
    except Exception as e:
        print(f"[sidecar] WARNING: pools.json parse error ({e!r}) -> passthrough only")
        return {}

    if reserve_bifrost > 0:
        reserved: set[str] = set()
        cap = min(1, reserve_bifrost)
        for idx, (model, provs) in enumerate(data.items()):
            if not isinstance(provs, list):
                continue
            # Only the first pool reserves; never drain a pool below 1.
            if idx != 0 or len(provs) <= 1 or cap < 1:
                continue
            sorted_provs = sorted(provs)
            keep = sorted_provs[cap:]
            for p in sorted_provs[:cap]:
                reserved.add(p)
            data[model] = keep
        if reserved:
            print(
                f"[sidecar] reserve_bifrost={reserve_bifrost}: reserved "
                f"{sorted(reserved)} (first pool only, cap 1) for the Bifrost auto route"
            )
    return data

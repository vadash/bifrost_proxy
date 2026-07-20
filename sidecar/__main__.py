"""Entrypoint: ``python -m sidecar``.

Parses CLI args, builds the immutable ``SidecarConfig``, loads pools, wires
``RoutingState`` + ``JsonlWriter``s onto the ``Sidecar`` server, and serves.

Notable change vs. legacy ``proxy.py``: ``capture.jsonl`` recording is OFF by
default. Pass ``--capture`` to enable it (``sidecar.log`` decision log stays
on for pooled requests either way -- it is the routing observability surface).
"""

from __future__ import annotations

import argparse
import os
import sys

from .config import SidecarConfig, load_pools
from .io_jsonl import JsonlWriter
from .proxy import Handler, Sidecar
from .state import RoutingState

_DEFAULT_DIR = os.path.dirname(os.path.abspath(__file__))


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sidecar",
        description=(
            "Bifrost routing sidecar: session-pinned provider routing for "
            "pooled models; transparent passthrough for everything else."
        ),
    )
    p.add_argument(
        "--listen", default="127.0.0.1:8088",
        metavar="HOST:PORT",
        help="bind address (default: 127.0.0.1:8088)",
    )
    p.add_argument(
        "--upstream", default="127.0.0.1:8080",
        metavar="HOST:PORT",
        help="Bifrost address (default: 127.0.0.1:8080)",
    )
    p.add_argument(
        "--pools", default=os.path.join(_DEFAULT_DIR, "pools.json"),
        metavar="PATH",
        help="pools.json path (default: sidecar/pools.json)",
    )
    p.add_argument(
        "--log", default=os.path.join(_DEFAULT_DIR, "sidecar.log"),
        metavar="PATH",
        help="decision-log path for pooled requests (default: sidecar/sidecar.log)",
    )
    p.add_argument(
        "--capture", default=None,
        metavar="PATH", nargs="?",
        const="__DEFAULT__",
        help=(
            "record one JSON line per pooled request. OFF by default. "
            "Pass with no value to use sidecar/capture.jsonl; pass a path to "
            "override."
        ),
    )
    p.add_argument(
        "--ttl", type=float, default=3600.0, metavar="SECS",
        help="session pin / response-id map inactivity TTL (default: 3600)",
    )
    p.add_argument(
        "--cooldown", type=float, default=600.0, metavar="SECS",
        help="default per-provider cooldown duration (default: 600)",
    )
    p.add_argument(
        "--upstream-timeout", type=float, default=600.0, metavar="SECS",
        help="per-upstream request timeout (default: 600)",
    )
    return p


def _split_host_port(s: str, default_host: str, default_port: int) -> tuple[str, int]:
    """Parse ``HOST:PORT`` with defaults; missing parts fall back to defaults."""
    if ":" in s:
        host, _, port = s.rpartition(":")
        return (host or default_host, int(port or default_port))
    # bare port number
    if s.isdigit():
        return (default_host, int(s))
    return (s or default_host, default_port)


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    listen_host, listen_port = _split_host_port(
        args.listen, "127.0.0.1", 8088
    )
    upstream_host, upstream_port = _split_host_port(
        args.upstream, "127.0.0.1", 8080
    )

    # Resolve --capture. OFF by default; "--capture" with no value uses the
    # default capture.jsonl path next to this package.
    capture_enabled = False
    capture_path = os.path.join(_DEFAULT_DIR, "capture.jsonl")
    if args.capture is not None:
        capture_enabled = True
        if args.capture != "__DEFAULT__":
            capture_path = args.capture

    pools = load_pools(args.pools)

    cfg = SidecarConfig(
        listen_host=listen_host,
        listen_port=listen_port,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        pools_path=args.pools,
        log_path=args.log,
        capture_path=capture_path,
        capture_enabled=capture_enabled,
        session_ttl=args.ttl,
        default_cooldown=args.cooldown,
        upstream_timeout=args.upstream_timeout,
        pools=pools,
    )

    state = RoutingState(cfg)
    capture_writer = JsonlWriter(cfg.capture_path)
    log_writer = JsonlWriter(cfg.log_path)

    print(
        f"[sidecar] listening on http://{cfg.listen_host}:{cfg.listen_port} "
        f"-> http://{cfg.upstream_host}:{cfg.upstream_port}  "
        f"pooled_models={len(pools)} capture={'ON -> ' + cfg.capture_path if capture_enabled else 'off'} "
        f"log={cfg.log_path}"
    )
    if pools:
        for model, provs in pools.items():
            print(f"[sidecar]   {model}: {len(provs)} providers")

    server = Sidecar(cfg.listen_addr, Handler)
    server.cfg = cfg
    server.state = state
    server.capture_writer = capture_writer
    server.log_writer = log_writer

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[sidecar] shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Sidecar: session-pinned routing proxy for Bifrost.

A stdlib-only reverse proxy that sits between LLM clients and Bifrost.
For "pooled" models (declared in ``pools.json``) it pins each session to a
deterministic upstream provider for prompt-cache locality, owns the fallback
order, and cools down failing providers globally. Non-pooled models pass
through verbatim with no state, no headers, no logs.

Run with::

    python -m sidecar            # capture off (default)
    python -m sidecar --capture  # record capture.jsonl
"""

__all__ = ["__version__"]
__version__ = "2.1.0"

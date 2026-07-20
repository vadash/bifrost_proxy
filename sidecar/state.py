"""In-memory routing state, fully encapsulated behind a thread-safe class.

Replaces the six module-level globals (``PINS``, ``RESP_MAP``, ``COOLDOWNS``,
``POOLS``) and the single shared ``STATE_LOCK`` from the old monolithic
proxy.py. Everything that mutates routing state goes through one object; the
HTTP handler receives it (dependency injection) so there is no hidden global
to reach for.

Three addressable maps, all guarded by one lock (routing decisions always
touch at least two of them together, so one lock beats three):

* ``pins``      : session_key -> {"pin": int, "seen": float}
                  pin = index into the pooled model's provider list;
                  seen = last activity epoch, refreshed every request.
* ``resp_map``  : response_id -> {"session": str, "seen": float}
                  enables the ``previous_response_id`` branch of the cascade.
* ``cooldowns``  : provider_name -> expiry_epoch  (hot iff expiry > now).

The config is held by reference so TTL / cooldown duration come from one
immutable source of truth.
"""

from __future__ import annotations

import random
import threading
import time

from .config import SidecarConfig
from .identity import derive_session_key


class RoutingState:
    """Thread-safe routing state for the sidecar.

    Every method that reads or mutates state takes ``self._lock``. Callers
    that need an atomic multi-field decision (e.g. assign-pin + cooldown +
    resp-map update) use the provided ``with state.lock():`` context manager
    so the whole block runs under one acquisition -- the same invariant the
    old ``STATE_LOCK`` gave, now explicit and local to this object.
    """

    __slots__ = ("_cfg", "_lock", "_rng", "pins", "resp_map", "cooldowns", "pools")

    def __init__(self, cfg: SidecarConfig):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._rng = random.Random()
        self.pins: dict[str, dict] = {}
        self.resp_map: dict[str, dict] = {}
        self.cooldowns: dict[str, float] = {}
        self.pools: dict[str, list[str]] = cfg.pools

    # ------------------------------------------------------------------
    # Lock context -- exposes the internal lock for compound decisions.
    # ------------------------------------------------------------------
    def lock(self) -> threading.Lock:
        """Return the state lock for externally-scoped compound decisions.

        Example::

            with state.lock():
                state.purge_expired(now)
                state.assign_pin(session_key, providers, now)
        """
        return self._lock

    def purge_expired(self, now: float) -> None:
        """Purge expired pins/resp_map (inactivity > session_ttl) and expired
        cooldowns (``now >= expiry``). Must be called under ``self.lock()``.
        """
        ttl = self._cfg.session_ttl
        expired_sessions = [
            k for k, v in self.pins.items() if now - v["seen"] > ttl
        ]
        for k in expired_sessions:
            del self.pins[k]
        expired_resps = [
            k for k, v in self.resp_map.items() if now - v["seen"] > ttl
        ]
        for k in expired_resps:
            del self.resp_map[k]
        expired_cd = [p for p, exp in self.cooldowns.items() if now >= exp]
        for p in expired_cd:
            del self.cooldowns[p]

    def cooldown_is_hot(self, provider: str, now: float) -> bool:
        """True iff ``provider`` is currently in cooldown.

        Must be called under ``self.lock()``.
        """
        return self.cooldowns.get(provider, 0) > now

    def cooldown_trigger(
        self, provider: str, now: float, secs: float | None = None
    ) -> None:
        """Put ``provider`` into cooldown. Re-trigger before expiry extends to
        the later of current/new expiry. Must be called under ``self.lock()``.
        """
        if secs is None:
            secs = self._cfg.default_cooldown
        self.cooldowns[provider] = max(
            self.cooldowns.get(provider, 0), now + secs
        )

    def derive_session_key(self, body: dict) -> tuple[str, str]:
        """Return ``(session_key, source)`` via the identity cascade.

        Must be called under ``self.lock()`` (it reads ``resp_map``).
        """
        return derive_session_key(body, self.resp_map)

    def assign_pin(
        self, session_key: str, providers: list[str], now: float
    ) -> int:
        """Return the pinned provider index for ``session_key``.

        If known, refresh ``seen`` and return the stored pin; else compute
        least-loaded start: fewest live pinned sessions among cold providers,
        tie -> uniform-random choice (so a fresh pool doesn't stampede every
        session onto the lowest-index provider on cold start); if all hot,
        fall back to all indices. Store + return the pin. Must be called
        under ``self.lock()``.
        """
        if session_key in self.pins:
            self.pins[session_key]["seen"] = now
            return self.pins[session_key]["pin"]

        load = [0] * len(providers)
        ttl = self._cfg.session_ttl
        for v in self.pins.values():
            idx = v["pin"]
            if 0 <= idx < len(providers) and now - v["seen"] <= ttl:
                load[idx] += 1

        candidates = [
            i for i in range(len(providers))
            if not self.cooldown_is_hot(providers[i], now)
        ]
        if not candidates:
            # desperate: all hot, still land somewhere
            candidates = list(range(len(providers)))
        min_load = min(load[i] for i in candidates)
        tied = [i for i in candidates if load[i] == min_load]
        pin = tied[0] if len(tied) == 1 else self._rng.choice(tied)
        self.pins[session_key] = {"pin": pin, "seen": now}
        return pin

    def re_pin(
        self, session_key: str, provider: str, providers: list[str],
        now: float,
    ) -> None:
        """Re-pin ``session_key`` to ``provider``'s index. No-op if the
        provider isn't in ``providers``. Must be called under ``self.lock()``.
        """
        if provider in providers:
            self.pins[session_key] = {
                "pin": providers.index(provider),
                "seen": now,
            }

    def build_send_order(
        self, providers: list[str], pin: int, now: float
    ) -> tuple[list[str], bool]:
        """Return ``(send_order, desperate)`` for a pooled request.

        Rotate ``providers`` to start at ``pin``; keep the WHOLE ring but move
        providers currently in cooldown to the END so Bifrost only reaches them
        as a last resort. ``desperate`` is True iff no cold provider exists.
        Must be called under ``self.lock()`` (reads ``cooldowns``).
        """
        ring = list(providers[pin:]) + list(providers[:pin])
        cold = [p for p in ring if not self.cooldown_is_hot(p, now)]
        hot = [p for p in ring if self.cooldown_is_hot(p, now)]
        return cold + hot, not cold

    def map_response(self, response_id: str, session_key: str, now: float) -> None:
        """Record ``response_id -> session`` for future prev-id lookups.

        Must be called under ``self.lock()``.
        """
        self.resp_map[response_id] = {"session": session_key, "seen": now}


    def is_pooled(self, model: str | None) -> bool:
        """True iff ``model`` is declared as a pool key in pools.json."""
        return model is not None and model in self.pools


def fallback_feedback(
    keep_list: list[str] | None,
    served: str | None,
    response_status: int | None,
) -> tuple[str | None, str | None]:
    """Decide the post-response action for the 2xx fallback path.

    Returns ``(repin_to, cooldown_provider)``:
    * ``repin_to`` — provider to re-pin the session to (the server that
      actually answered) iff Bifrost fell back off the forced primary, i.e.
      status is 2xx and ``served`` differs from the primary we sent
      (``keep_list[0]``). ``is_fallback`` from routing_info is intentionally
      NOT consulted — served-vs-forced-primary is the real signal.
    * ``cooldown_provider`` — the first provider AFTER the primary
      (``keep_list[1]``) iff it was actually skipped (``served`` is neither
      ``keep_list[0]`` nor ``keep_list[1]``); else ``None`` (never cool a
      provider that served).
    """
    if response_status is None or not (200 <= response_status < 300):
        return None, None
    if not keep_list or served is None or served == keep_list[0]:
        return None, None
    cooldown_provider = None
    if len(keep_list) > 1 and served != keep_list[1]:
        cooldown_provider = keep_list[1]
    return served, cooldown_provider

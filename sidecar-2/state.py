"""In-memory routing state, fully encapsulated behind a thread-safe class.

Replaces the module-level globals (``PINS``, ``COOLDOWNS``,
``POOLS``) and the single shared ``STATE_LOCK`` from the old monolithic
proxy.py. Everything that mutates routing state goes through one object; the
HTTP handler receives it (dependency injection) so there is no hidden global
to reach for.

Three addressable maps, all guarded by one lock (routing decisions always
touch at least two of them together, so one lock beats three):

* ``pins``      : session_key -> {"pin": int, "seen": float}
                  pin = index into the pooled model's provider list;
                  seen = last activity epoch, refreshed every request.
* ``cooldowns``  : provider_name -> expiry_epoch  (hot iff expiry > now).

The config is held by reference so TTL / cooldown duration come from one
immutable source of truth.
"""

from __future__ import annotations

import json
import random
import threading

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

    __slots__ = ("_cfg", "_lock", "_rng", "pins", "cooldowns", "pools")

    def __init__(self, cfg: SidecarConfig, *, shuffle_pools: bool = True):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._rng = random.Random()
        self.pins: dict[str, dict] = {}
        self.cooldowns: dict[str, float] = {}
        # Optionally shuffle each pool's providers once at startup so the
        # cold-start fallback order (the body ``fallbacks`` array) is
        # randomized across runs, not merely alpha-sorted.
        # ``build_send_order`` still rotates the ring to start at the session
        # pin, so the primary stays the pinned provider and the *sequence
        # after it* is the (optionally shuffled) ring. ``shuffle_pools=False``
        # keeps the declared order -- used by deterministic tests.
        if shuffle_pools:
            self.pools: dict[str, list[str]] = {
                model: self._rng.sample(list(provs), len(provs))
                for model, provs in cfg.pools.items()
            }
        else:
            self.pools = {
                model: list(provs) for model, provs in cfg.pools.items()
            }

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
        """Purge expired pins (inactivity > session_ttl) and expired
        cooldowns (``now >= expiry``). Must be called under ``self.lock()``.
        """
        ttl = self._cfg.session_ttl
        expired_sessions = [
            k for k, v in self.pins.items() if now - v["seen"] > ttl
        ]
        for k in expired_sessions:
            del self.pins[k]
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

    def _least_loaded(
        self,
        providers: list[str],
        now: float,
        exclude: frozenset[int],
    ) -> int | None:
        """Least-loaded cold provider index, skipping ``exclude``.

        Load counts every live pin (inactivity <= session_ttl) on the index,
        including second pins (``pin2``) from fast sessions so they count as
        load too. Cold candidates first; when all hot, fall back to ALL
        indices (desperate) minus ``exclude``. Tie -> uniform-random choice
        (so a fresh pool doesn't stampede every session onto the lowest-index
        provider on cold start). Returns ``None`` when no candidate remains
        (only possible with a non-empty ``exclude``). Must be called under
        ``self.lock()``.
        """
        load = [0] * len(providers)
        ttl = self._cfg.session_ttl
        for v in self.pins.values():
            if now - v["seen"] > ttl:
                continue
            for key in ("pin", "pin2"):
                idx = v.get(key)
                if idx is not None and 0 <= idx < len(providers):
                    load[idx] += 1

        candidates = [
            i for i in range(len(providers))
            if i not in exclude and not self.cooldown_is_hot(providers[i], now)
        ]
        if not candidates:
            # desperate: all hot, still land somewhere
            candidates = [
                i for i in range(len(providers)) if i not in exclude
            ]
        if not candidates:
            return None
        min_load = min(load[i] for i in candidates)
        tied = [i for i in candidates if load[i] == min_load]
        return tied[0] if len(tied) == 1 else self._rng.choice(tied)

    def assign_pin(
        self, session_key: str, providers: list[str], now: float
    ) -> int:
        """Return the pinned provider index for ``session_key``.

        If known, refresh ``seen`` and return the stored pin; else compute a
        least-loaded start (see ``_least_loaded``), store ``{"pin", "seen"}``
        (no ``pin2`` key -- fast sessions add it via ``assign_pin_pair``),
        and return the pin. Must be called under ``self.lock()``.
        """
        if session_key in self.pins:
            self.pins[session_key]["seen"] = now
            return self.pins[session_key]["pin"]

        pin = self._least_loaded(providers, now, frozenset())
        self.pins[session_key] = {"pin": pin, "seen": now}
        return pin

    def assign_pin_pair(
        self, session_key: str, providers: list[str], now: float
    ) -> tuple[int, int | None]:
        """Return ``(pin_a, pin_b)`` for a fast two-lane session.

        Existing record: refresh ``seen``; validate both pins are in range
        (out-of-range treated as missing). Missing ``pin2`` in a multi-provider
        pool gets a fresh least-loaded assignment excluding ``pin_a``. New
        session: both pins assigned least-loaded (``pin_b`` excludes
        ``pin_a``). ``pin_b`` is ``None`` only for a 1-provider pool. The
        record keeps the shape ``{"pin", "pin2", "seen"}`` (``pin2`` omitted
        for 1-provider pools). Must be called under ``self.lock()``.
        """
        record = self.pins.get(session_key)
        if record is not None:
            record["seen"] = now
            pin_a = record["pin"]
            if not 0 <= pin_a < len(providers):
                pin_a = self._least_loaded(providers, now, frozenset())
                record["pin"] = pin_a
            pin_b = record.get("pin2")
            if pin_b is not None and not 0 <= pin_b < len(providers):
                pin_b = None
            if pin_b is None and len(providers) > 1:
                pin_b = self._least_loaded(
                    providers, now, frozenset({pin_a})
                )
            if pin_b is not None:
                record["pin2"] = pin_b
            return pin_a, pin_b

        pin_a = self._least_loaded(providers, now, frozenset())
        pin_b = (
            self._least_loaded(providers, now, frozenset({pin_a}))
            if len(providers) > 1
            else None
        )
        record = {"pin": pin_a, "seen": now}
        if pin_b is not None:
            record["pin2"] = pin_b
        self.pins[session_key] = record
        return pin_a, pin_b

    def re_pin(
        self, session_key: str, provider: str, providers: list[str],
        now: float,
    ) -> None:
        """Re-pin ``session_key`` to ``provider``'s index. No-op if the
        provider isn't in ``providers``. Preserves an existing ``pin2``
        (fast sessions' second pin). Must be called under ``self.lock()``.
        """
        if provider in providers:
            old = self.pins.get(session_key) or {}
            record = {
                "pin": providers.index(provider),
                "seen": now,
            }
            if old.get("pin2") is not None:
                record["pin2"] = old["pin2"]
            self.pins[session_key] = record

    def re_pin_lane(
        self,
        session_key: str,
        provider: str,
        providers: list[str],
        lane: str,
        now: float,
    ) -> None:
        """Re-pin one lane slot (``"a"`` -> ``pin``, ``"b"`` -> ``pin2``)
        of ``session_key`` to ``provider``'s index, preserving the other slot
        and refreshing ``seen``. No-op when ``provider`` isn't in
        ``providers`` or the session record is gone. Must be called under
        ``self.lock()``.
        """
        if provider not in providers:
            return
        record = self.pins.get(session_key)
        if record is None:
            return
        record["seen"] = now
        if lane == "a":
            record["pin"] = providers.index(provider)
        else:
            record["pin2"] = providers.index(provider)

    def build_fast_lanes(
        self,
        providers: list[str],
        pin_a: int,
        pin_b: int | None,
        now: float,
    ) -> tuple[list[str], list[str], bool]:
        """Split the session's send-order ring odd/even into two lanes.

        ``ring, desperate = build_send_order(providers, pin_a, now)``; when
        ``pin_b`` is set, cold, and not already at ring position 0, it is
        swapped into position 1 (lane B's primary). Lane A takes
        ``ring[0::2][:3]``, lane B ``ring[1::2][:3]`` -- the odd/even split
        AND the upstream cap (primary + 2 fallbacks; see
        ``agent_docs/routing/sidecar-routing-policy.md``). ``lane_b`` is
        ``[]`` only for a 1-provider pool. Must be called under
        ``self.lock()`` (reads ``cooldowns``).
        """
        ring, desperate = self.build_send_order(providers, pin_a, now)
        if (
            pin_b is not None
            and providers[pin_b] != ring[0]
            and not self.cooldown_is_hot(providers[pin_b], now)
        ):
            idx = ring.index(providers[pin_b])
            ring[1], ring[idx] = ring[idx], ring[1]
        lane_a = ring[0::2][:3]
        lane_b = ring[1::2][:3]
        return lane_a, lane_b, desperate

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


def plan_pooled_request(
    state: RoutingState, parsed: dict, now: float
) -> dict | None:
    """Decide pooled routing for ``parsed`` and rewrite the body for upstream.

    Module-level free function (mirrors ``fallback_feedback``): takes
    ``state`` explicitly so it stays unit-testable from ``test_routing.py``
    via ``importlib`` like its sibling, without depending on a handler.

    Returns ``None`` iff the request's model isn't a declared pool key (the
    "non-pooled passthrough" signal the proxy acts on with no further work).
    Otherwise returns a dict describing the decision:

    * ``pooled_model``      — the declared pool key (e.g. ``"z-ai/glm-5.2"``)
    * ``providers``         — the pool's provider-name list
    * ``session_key``       — derived session identity (cache_key or h:<digest>)
    * ``session_source``   — ``"cache_key"`` or ``"hash"``
    * ``pin``               — pinned provider index for this session
    * ``keep_list``         — the full send-order ring (cold first, hot last),
      rotated to start at ``pin``. ``keep_list[0]`` is the forced primary.
    * ``desperate``         — True iff every provider was in cooldown
    * ``forward_body``      — re-serialized request ``bytes`` with
      ``model`` rewritten to ``"{primary}/{pooled}"`` and ``fallbacks``
      to ``["{p}/{pooled}" for p in keep_list[1:3]]`` (primary + 2
      fallbacks; the full ring stays in ``keep_list`` for pins/cooldowns).
    Locking matches the inline block this replaces: purge -> derive ->
    assign_pin -> build_send_order all run under ``state.lock()`` (the same
    atomic compound decision the handler used), then the body rewrite runs
    outside the lock — it's pure dict ops + ``json.dumps`` with no shared
    state, so holding the lock there would only serialise unrelated I/O.
    """
    if not isinstance(parsed, dict):
        return None
    pooled_model = parsed.get("model")
    if not state.is_pooled(pooled_model):
        return None

    providers = state.pools[pooled_model]
    with state.lock():
        state.purge_expired(now)
        session_key, session_source = derive_session_key(parsed)
        pin = state.assign_pin(session_key, providers, now)
        keep_list, desperate = state.build_send_order(providers, pin, now)

    parsed["model"] = f"{keep_list[0]}/{pooled_model}"
    # Send primary + 2 fallbacks only; full ring stays in keep_list for
    # pins/cooldowns (fallback_feedback still indexes keep_list[1]).
    parsed["fallbacks"] = [f"{p}/{pooled_model}" for p in keep_list[1:3]]
    forward_body = json.dumps(parsed).encode("utf-8")

    return {
        "pooled_model": pooled_model,
        "providers": providers,
        "session_key": session_key,
        "session_source": session_source,
        "pin": pin,
        "keep_list": keep_list,
        "desperate": desperate,
        "forward_body": forward_body,
    }

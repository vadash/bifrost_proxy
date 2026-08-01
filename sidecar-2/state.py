"""In-memory routing state, fully encapsulated behind a thread-safe class.

Replaces the module-level globals (``PINS``, ``COOLDOWNS``,
``POOLS``) and the single shared ``STATE_LOCK`` from the old monolithic
proxy.py. Everything that mutates routing state goes through one object; the
HTTP handler receives it (dependency injection) so there is no hidden global
to reach for.

Four addressable maps, all guarded by one lock (routing decisions always
touch at least two of them together, so one lock beats three):

* ``pins``      : session_key -> {"pin": int, "seen": float}
                  pin = index into the pooled model's provider list;
                  seen = last activity epoch, refreshed every request.
* ``cooldowns``  : provider_name -> expiry_epoch  (hot iff expiry > now).
* ``pool_circuit`` : pooled_model -> {"open_until": float, "backoff": float,
                  "probing": bool}. Pool-level circuit breaker. Upstream rate
                  limits are ACCOUNT-wide, not per-provider: a single
                  over-limit cools every provider of the pool at once, so
                  per-provider cooldowns alone cascade the whole ring into
                  permanent desperate mode (every provider trips within
                  seconds) and the sidecar then hammers upstream for the
                  cooldown duration. ``pool_circuit`` watches the WHOLE pool
                  and lets the proxy answer 429 locally instead of
                  forwarding, stopping the storm. See the circuit_* methods.

The config is held by reference so TTL / cooldown duration come from one
immutable source of truth.
"""

from __future__ import annotations

import json
import random
import threading

from .config import SidecarConfig
from .identity import derive_session_key
from .predicates import is_2xx


class RoutingState:
    """Thread-safe routing state for the sidecar.

    Every method that reads or mutates state takes ``self._lock``. Callers
    that need an atomic multi-field decision (e.g. assign-pin + cooldown +
    resp-map update) use the provided ``with state.lock():`` context manager
    so the whole block runs under one acquisition -- the same invariant the
    old ``STATE_LOCK`` gave, now explicit and local to this object.
    """

    __slots__ = (
        "_cfg", "_lock", "_rng", "pins", "cooldowns", "pools", "pool_circuit",
    )

    def __init__(self, cfg: SidecarConfig, *, shuffle_pools: bool = True):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._rng = random.Random()
        self.pins: dict[str, dict] = {}
        self.cooldowns: dict[str, float] = {}
        # Pool-level circuit breaker: pooled_model -> {"open_until": float,
        # "backoff": float, "probing": bool}. Upstream rate limits are
        # ACCOUNT-wide, not per-provider, so per-provider cooldowns alone
        # cascade the whole ring into permanent desperate mode; this map
        # watches the whole pool and lets the proxy short-circuit 429s
        # locally. See the circuit_* methods. Guarded by ``self._lock``.
        self.pool_circuit: dict[str, dict] = {}
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

        ``pool_circuit`` retention: a lapsed entry is NOT dropped the instant
        its window closes. It carries the escalating ``backoff`` that the
        next trip must resume from (doubling), so dropping it on lapse would
        destroy escalation across a lapse -- the next trip would restart at
        ``circuit_base`` instead of doubling. Keep a lapsed, non-probing entry
        until it is genuinely stale -- ``now >= open_until + circuit_max``
        (well past any plausible re-trip, since the largest auto-window is
        ``circuit_max``) -- so the escalation survives a recovery hole while
        the map still stays bounded. A half-open entry with a probe in flight
        (``probing == True``) is always kept until the probe resolves
        (reset/trip), regardless of the stale horizon: purge must not drop the
        gate and let a second probe sneak through.
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
        stale_horizon = self._cfg.circuit_max
        stale_circuits = [
            m for m, st in self.pool_circuit.items()
            if not st.get("probing")
            and now >= st.get("open_until", 0) + stale_horizon
        ]
        for m in stale_circuits:
            del self.pool_circuit[m]

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

        If known, refresh ``seen`` and return the stored pin (recomputing when
        the stored pin is missing or out of range, e.g. after the pool
        shrank via --reserve-bifrost); else compute a least-loaded start
        (see ``_least_loaded``), store ``{"pin", "seen"}`` (no ``pin2`` key
        -- fast sessions add it via ``assign_pin_pair``), and return the
        pin. Returns 0 as a last resort when the pool is empty. Must be
        called under ``self.lock()``.
        """
        record = self.pins.get(session_key)
        if record is not None:
            record["seen"] = now
            pin = record.get("pin")
            if isinstance(pin, int) and 0 <= pin < len(providers):
                return pin
        # Fresh or corrupted record: (re)assign least-loaded.
        pin = self._least_loaded(providers, now, frozenset())
        if pin is None:
            pin = 0  # only reachable with an empty pool (already rejected by
                     # pooled_gate; defensive fallback for a shrunken pool)
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
        if not providers:
            # Defensive: an empty pool cannot be pinned. Both lanes None
            # tells the caller (build_fast_lanes) to produce empty lanes;
            # plan_fast_request would have short-circuited via pooled_gate,
            # but guard against a pool that shrank between pin and plan.
            return None, None
        record = self.pins.get(session_key)
        if record is not None:
            record["seen"] = now
            pin_a = record.get("pin")
            if not isinstance(pin_a, int) or not 0 <= pin_a < len(providers):
                pin_a = self._least_loaded(providers, now, frozenset())
                record["pin"] = pin_a
            pin_b = record.get("pin2")
            if pin_b is not None and (
                not isinstance(pin_b, int) or not 0 <= pin_b < len(providers)
            ):
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

    def set_lane_pins(
        self, session_key: str, lane_a_idx: int, lane_b_idx: int | None, now: float
    ) -> None:
        """Write the lane-primary pins for a fast two-lane session.

        Replaces whatever ``assign_pin_pair`` stored: after the odd/even split +
        pin_b swap in ``build_fast_lanes``, the actual lane primaries can differ
        from the least-loaded pins ``assign_pin_pair`` chose, so the session must
        be re-pinned to the lane primaries this request actually sent. ``lane_b_idx``
        ``None`` (1-provider pool) omits ``pin2``, matching ``assign_pin_pair``'s
        record shape. Must be called under ``self.lock()``.
        """
        record = {"pin": lane_a_idx, "seen": now}
        if lane_b_idx is not None:
            record["pin2"] = lane_b_idx
        self.pins[session_key] = record

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

    # ------------------------------------------------------------------
    # Pool-level circuit breaker -- the circuit_* methods.
    #
    # Upstream rate limits are ACCOUNT-wide, not per-provider: a single
    # over-limit trips every provider of the pool at once, and per-provider
    # cooldowns alone cascade the whole ring into permanent desperate mode
    # (every provider hot within seconds). The circuit watches the WHOLE
    # pool: once every provider is hot (``all_hot``), ``circuit_trip`` opens
    # it so ``plan_pooled_request`` short-circuits and the proxy answers 429
    # locally instead of forwarding -- stopping the upstream storm. The open
    # window escalates (``circuit_base`` -> doubling -> ``circuit_max``) so a
    # sustained account limit backs off harder.
    #
    # All six methods MUST be called under ``self.lock()``; none of them
    # acquire the lock themselves.
    # ------------------------------------------------------------------
    def circuit_open(self, model: str, now: float) -> bool:
        """True iff the pool's circuit is OPEN (window not yet lapsed).

        Must be called under ``self.lock()``. A half-open entry (window lapsed
        but not yet reset) is NOT open here -- that's the ``circuit_probe``
        gate -- so an open circuit stops forwarding while a lapsed one admits
        a single probe.
        """
        st = self.pool_circuit.get(model)
        return st is not None and now < st["open_until"]

    def circuit_retry_after(self, model: str, now: float) -> float:
        """Seconds until the circuit reopens for ``model``.

        Must be called under ``self.lock()``. Returns the positive remainder
        ``open_until - now`` rounded UP to at least 1 while the circuit is
        open; 0 when closed or half-open (no forward; nothing to wait on).
        """
        st = self.pool_circuit.get(model)
        if st is None:
            return 0.0
        remaining = st["open_until"] - now
        if remaining <= 0:
            return 0.0
        return float(max(1, int(remaining) if remaining == int(remaining)
                          else int(remaining) + 1))

    def circuit_trip(
        self, model: str, now: float, retry_after: float | None = None
    ) -> None:
        """Open the circuit for ``model``.

        Must be called under ``self.lock()``. The open window is
        ``retry_after`` when upstream supplied one (an explicit hint honored
        verbatim), else the escalating auto window: ``circuit_base`` on the
        first trip, doubling on each later auto-trip, capped at
        ``circuit_max``. Re-tripping before the current window expires
        EXTENDS it to the later of current/new ``open_until`` (a sustained
        limit must not let an early re-trip shorten the backoff). Sets
        ``probing=False`` (any in-flight probe is moot once re-tripped). An
        explicit ``retry_after`` does not disturb the auto-escalation so a
        later auto-trip resumes escalating from where it left off.
        """
        if retry_after is not None and retry_after > 0:
            new_until = now + float(retry_after)
        else:
            st = self.pool_circuit.get(model)
            backoff = (
                self._cfg.circuit_base
                if st is None
                else min(st["backoff"] * 2.0, self._cfg.circuit_max)
            )
            new_until = now + backoff
        cur_until = self.pool_circuit.get(model, {}).get("open_until", 0.0)
        open_until = new_until if new_until > cur_until else cur_until
        st = self.pool_circuit.get(model)
        if st is None:
            st = {}
            self.pool_circuit[model] = st
        st["open_until"] = open_until
        if retry_after is None or retry_after <= 0:
            # auto-trip advances the escalation; explicit hints leave it.
            st["backoff"] = open_until - now
        elif "backoff" not in st:
            st["backoff"] = self._cfg.circuit_base
        st["probing"] = False

    def circuit_probe(self, model: str, now: float) -> bool:
        """Half-open gate: admit exactly ONE probe per open->closed transition.

        Must be called under ``self.lock()``. True exactly once when the entry
        exists but its window has lapsed (``now >= open_until``): sets
        ``probing=True`` so the caller lets a single real request through to
        test the limit -- a 2xx resets the circuit, a fresh failure re-trips.
        Subsequent calls return False until the probe resolves (reset/trip),
        so the pool can't flood upstream while half-open.
        """
        st = self.pool_circuit.get(model)
        if st is None or now < st["open_until"]:
            return False
        if st.get("probing"):
            return False
        st["probing"] = True
        return True

    def circuit_reset(self, model: str, providers: list[str]) -> bool:
        """Close the circuit and clear collateral provider cooldowns.

        Must be called under ``self.lock()``. Returns ``True`` iff a
        ``pool_circuit`` entry actually existed for ``model`` (the circuit was
        genuinely open/half-open): only then does it drop the entry AND remove
        every provider in ``providers`` from ``cooldowns`` -- those 600s
        per-provider cooldowns were collateral damage from an account-wide
        limit, not per-provider faults, so they must not keep the pool
        desperate once the account can serve again. Returns ``False`` (and
        touches nothing) when no entry existed, so an ordinary 2xx on a pool
        that never tripped does NOT nuke unrelated, legitimate per-provider
        cooldowns (dead/slow-provider cooling, the 2xx-fallback stampede
        cooldown). Callers distinguish 'recovered from a trip' from 'ordinary
        success' via the bool.
        """
        if model not in self.pool_circuit:
            return False
        del self.pool_circuit[model]
        for p in providers:
            self.cooldowns.pop(p, None)
        return True

    def all_hot(self, providers: list[str], now: float) -> bool:
        """True iff EVERY provider in ``providers`` is in cooldown.

        Must be called under ``self.lock()``. The signal the feedback path
        uses to decide whether a 429 was account-wide (trip the circuit) or a
        single-provider fault (cool one provider, try the next).
        """
        return bool(providers) and all(
            self.cooldown_is_hot(p, now) for p in providers
        )


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
    if not is_2xx(response_status):
        return None, None
    if not keep_list or served is None or served == keep_list[0]:
        return None, None
    cooldown_provider = None
    if len(keep_list) > 1 and served != keep_list[1]:
        cooldown_provider = keep_list[1]
    return served, cooldown_provider


def pooled_gate(state: RoutingState, parsed) -> tuple[str, list[str]] | None:
    """Return ``(pooled_model, providers)`` iff ``parsed`` is a dict whose
    model is a declared pool key, else ``None`` (passthrough signal)."""
    if not isinstance(parsed, dict):
        return None
    pooled_model = parsed.get("model")
    if not state.is_pooled(pooled_model):
        return None
    providers = state.pools[pooled_model]
    if not providers:
        # An empty pool (e.g. over-reserved by --reserve-bifrost) cannot
        # route; fall back to transparent passthrough so the request still
        # reaches Bifrost verbatim instead of 502'ing on an empty ring.
        return None
    return pooled_model, providers


def rewrite_body(
    body: dict, pooled_model: str, order: list[str], *, max_fallbacks: int = 2
) -> bytes:
    """Rewrite ``body`` IN PLACE: model -> ``{order[0]}/{pooled_model}``,
    fallbacks -> the next ``max_fallbacks`` providers. Returns the
    serialized bytes.

    ``max_fallbacks`` caps the body ``fallbacks`` array: the default 2 sends
    primary + 2 (``order[1:3]``); ``max_fallbacks=0`` sends the primary ONLY
    (``"fallbacks": []``) -- used in desperate mode so one client request
    makes exactly ONE upstream call instead of three, killing the 3x
    amplification that holds the account rate-limit window open. Slicing
    past the end is safe.
    """
    body["model"] = f"{order[0]}/{pooled_model}"
    body["fallbacks"] = [
        f"{p}/{pooled_model}" for p in order[1:1 + max_fallbacks]
    ]
    return json.dumps(body).encode("utf-8")


def plan_pooled_request(
    state: RoutingState, parsed: dict, now: float
) -> dict | None:
    """Decide pooled routing for ``parsed`` and rewrite the body for upstream.

    Module-level free function (mirrors ``fallback_feedback``): takes
    ``state`` explicitly so it stays unit-testable from ``test_routing.py``
    via ``importlib`` like its sibling, without depending on a handler.

    Returns ``None`` iff the request's model isn't a declared pool key (the
    "non-pooled passthrough" signal the proxy acts on with no further work).

    Circuit states (all decided atomically under ``state.lock()``):

    * OPEN (window not yet lapsed): the pool's account-wide rate-limit circuit
      is open -- the pool is rate-limited as a whole and forwarding would only
      hold the limit window open. Returns EARLY with ``pooled_model``,
      ``providers``, ``session_key``, ``session_source``, ``circuit_open_for``
      (seconds until the circuit reopens, the value the proxy emits as
      ``Retry-After``), ``probe=False``, and
      ``pin=None, keep_list=None, desperate=True`` -- and NO
      ``forward_body``: the proxy must answer 429 locally, never connect
      upstream. (``pin``/``keep_list`` are ``None`` because no send order is
      built while the circuit blocks forwarding; they're present so the return
      shape stays stable.)
    * HALF-OPEN (window lapsed, a ``pool_circuit`` entry still exists):
      consult ``circuit_probe``. Exactly ONE probe is admitted per
      open->closed transition: the request that wins the probe plans NORMALLY
      but is forced to ``max_fallbacks=0`` (a probe is a single upstream call
      -- one client request must not fan out 3 lanes to test a rate limit) and
      gets ``probe=True``. A request that loses the probe (another probe is
      already in flight) is short-circuited exactly like the OPEN case
      (``probe=False``, ``circuit_open_for`` set to
      ``max(1.0, circuit_retry_after(...))`` so it is >=1s) -- the pool cannot
      flood upstream while half-open. Without this gate the very first request
      after the open window lapses would resume forwarding at FULL
      concurrency and every queued session would hit the still-limited
      account at once, re-tripping instantly (a muted version of the original
      storm).
    * CLOSED (no ``pool_circuit`` entry): the normal path, ``probe=False``.

    ``probe`` (bool) is present on BOTH returned dict shapes so consumers can
    rely on the key.

    Circuit CLOSED (and the half-open probe that won): returns a dict:

    * ``pooled_model``      — the declared pool key (e.g. ``"z-ai/glm-5.2"``)
    * ``providers``         — the pool's provider-name list
    * ``session_key``       — derived session identity (cache_key or h:<digest>)
    * ``session_source``   — ``"cache_key"`` or ``"hash"``
    * ``pin``               — pinned provider index for this session
    * ``keep_list``         — the full send-order ring (cold first, hot last),
      rotated to start at ``pin``. ``keep_list[0]`` is the forced primary.
    * ``desperate``         — True iff every provider was in cooldown
    * ``probe``             — True iff this request is the half-open probe.
    * ``circuit_open_for``  — ``None`` (the forwardable case; a positive float
      only on the open / lost-probe early returns above).
    * ``forward_body``      — re-serialized request ``bytes``. When NOT
      desperate AND not a probe, ``fallbacks`` is primary + 2
      (``keep_list[1:3]``). When desperate (all providers hot) OR this request
      is the probe, the body is rewritten with ``max_fallbacks=0`` so it sends
      the primary ONLY (``"fallbacks": []``): one client request -> one
      upstream call, not three. The probe MUST be primary-only for the same
      reason desperate is: a multi-lane probe would re-flood the limit it is
      testing. This kills the 3x amplification that -- combined with the
      circuit -- held the account rate-limit window open for ~10 minutes.

    Locking matches the inline block this replaces: purge -> derive -> circuit
    decision -> assign_pin -> build_send_order all run under ``state.lock()``
    (the same atomic compound decision the handler used), then the body
    rewrite runs outside the lock — it's pure dict ops + ``json.dumps`` with
    no shared state, so holding the lock there would only serialise unrelated
    I/O.
    """
    gate = pooled_gate(state, parsed)
    if gate is None:
        return None
    pooled_model, providers = gate
    with state.lock():
        state.purge_expired(now)
        session_key, session_source = derive_session_key(parsed)
        if state.circuit_open(pooled_model, now):
            # Whole pool rate-limited: do NOT assign a pin or build a send
            # order (nothing to forward); answer locally with the wait.
            open_for = state.circuit_retry_after(pooled_model, now)
            return {
                "pooled_model": pooled_model,
                "providers": providers,
                "session_key": session_key,
                "session_source": session_source,
                "pin": None,
                "keep_list": None,
                "desperate": True,
                "probe": False,
                "circuit_open_for": open_for,
            }
        # Half-open: a lapsed entry still exists. Admit exactly one probe;
        # requests that lose the probe short-circuit like the open case so the
        # pool cannot flood upstream while half-open.
        if pooled_model in state.pool_circuit:
            if state.circuit_probe(pooled_model, now):
                probe = True
            else:
                # Another probe is already in flight: hold this request back
                # briefly (>=1s; circuit_retry_after is 0 once lapsed) instead
                # of racing the in-flight probe against the still-limited
                # account.
                return {
                    "pooled_model": pooled_model,
                    "providers": providers,
                    "session_key": session_key,
                    "session_source": session_source,
                    "pin": None,
                    "keep_list": None,
                    "desperate": True,
                    "probe": False,
                    "circuit_open_for": max(
                        1.0, state.circuit_retry_after(pooled_model, now)
                    ),
                }
        else:
            probe = False
        pin = state.assign_pin(session_key, providers, now)
        keep_list, desperate = state.build_send_order(providers, pin, now)

    # Desperate (all providers hot) OR the half-open probe -> primary only:
    # stop the 3x amplification that holds the account rate-limit window open
    # (a probe must likewise be a single upstream call to test a limit, not a
    # 3-lane fan-out). Non-desperate, non-probe keeps the primary + 2
    # fallbacks cap. Full ring stays in keep_list for pins/cooldowns
    # (fallback_feedback still indexes keep_list[1]).
    if desperate or probe:
        forward_body = rewrite_body(parsed, pooled_model, keep_list, max_fallbacks=0)
    else:
        forward_body = rewrite_body(parsed, pooled_model, keep_list)

    return {
        "pooled_model": pooled_model,
        "providers": providers,
        "session_key": session_key,
        "session_source": session_source,
        "pin": pin,
        "keep_list": keep_list,
        "desperate": desperate,
        "probe": probe,
        "circuit_open_for": None,
        "forward_body": forward_body,
    }

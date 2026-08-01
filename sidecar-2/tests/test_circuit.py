"""Tests for the pool-level rate-limit circuit breaker.

Defends the fix for the self-inflicted 429 storm in the sidecar routing proxy
(``sidecar-2/state.py``). The bug: NVIDIA rate-limits the ACCOUNT, not the
individual provider, so one over-limit cools ALL of a pool's providers at
once. The old per-provider cooldown feedback cascaded the whole 15-provider
ring into permanent ``desperate`` mode within ~39 seconds, and the sidecar
then hammered upstream for ~10 minutes -- each client request forwarding
primary + 2 fallbacks (3x amplification), every refused call holding the
rate-limit window open, recovery only when the 600s cooldowns expired.

Two fixes under test here:

1. **Pool-level circuit breaker** (``pool_circuit`` + the ``circuit_*``
   methods): once every provider is hot (``all_hot``), ``circuit_trip`` opens
   the WHOLE pool so ``plan_pooled_request`` short-circuits and the proxy
   answers 429 locally instead of forwarding -- stopping the upstream storm.
   The open window escalates (``circuit_base`` -> doubling ->
   ``circuit_max``); ``circuit_probe`` admits exactly one probe per
   open->closed transition; ``circuit_reset`` closes the circuit AND clears
   the collateral per-provider cooldowns.
2. **Desperate primary-only** (``plan_pooled_request`` + ``rewrite_body``
   ``max_fallbacks``): when desperate, one client request forwards the
   primary ONLY -- killing the 3x amplification.

Uses stdlib ``unittest`` only (matches the sidecar's stdlib-only constraint).
No sleeps, no network: explicit ``now`` floats drive every time-sensitive
assertion through the pure state helpers.
"""

from __future__ import annotations

import importlib
import json
import unittest

# ``sidecar-2`` is not a valid ``import`` statement identifier (hyphen), so
# load the modules via importlib -- matches test_routing.py.
_config = importlib.import_module("sidecar-2.config")
_state_mod = importlib.import_module("sidecar-2.state")
SidecarConfig = _config.SidecarConfig
RoutingState = _state_mod.RoutingState
plan_pooled_request = _state_mod.plan_pooled_request
rewrite_body = _state_mod.rewrite_body
plan_fast_request = importlib.import_module("sidecar-2.fast").plan_fast_request
# Pool used across the tests: nvidia-1 ... nvidia-5. Kept small so the
# "all providers hot" state is reached with five cooldown_trigger calls,
# and the desperate fallback count is unambiguous.
P = [f"nvidia-{i}" for i in range(1, 6)]
MODEL = "z-ai/glm-5.2"


def _state() -> RoutingState:
    """Fresh RoutingState over a 5-provider pool, default cooldown.

    ``shuffle_pools=False`` keeps the declared order so the circuit methods
    (which key off the model, not the order) and ``plan_pooled_request``'s
    body assertions stay deterministic. ``circuit_base``/``circuit_max`` keep
    their dataclass defaults (20.0 / 120.0) so the escalation math is exact.
    """
    cfg = SidecarConfig(pools={MODEL: list(P)}, default_cooldown=600.0)
    return RoutingState(cfg, shuffle_pools=False)


def _cool_all(s: RoutingState, now: float) -> None:
    """Put every pool provider into cooldown -- the precondition for tripping
    the account-wide circuit (``all_hot`` must be True)."""
    for p in P:
        s.cooldown_trigger(p, now)


class TestCircuitOpenAndRetryAfter(unittest.TestCase):
    """``circuit_open`` flips within the window and is false after; the
    corresponding ``circuit_retry_after`` counts down and hits 0."""

    def test_open_true_inside_window_false_after(self) -> None:
        # The real bug: the circuit must NOTICE the whole pool is rate-limited
        # and stay open long enough that the proxy stops forwarding instead of
        # hammering upstream for the cooldown duration.
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now)
        # First auto-trip opens for circuit_base (20s).
        self.assertTrue(s.circuit_open(MODEL, now))
        self.assertTrue(s.circuit_open(MODEL, now + 19.9))
        # Window lapsed -> not "open" (half-open now; circuit_probe's gate).
        self.assertFalse(s.circuit_open(MODEL, now + 20.0))
        self.assertFalse(s.circuit_open(MODEL, now + 999.0))

    def test_retry_after_counts_down_then_zero(self) -> None:
        # retry_after is what the proxy emits as the 429 Retry-After header;
        # it must be a positive int while open and 0 once closed/half-open.
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now)  # open_until = 1020.0
        self.assertEqual(s.circuit_retry_after(MODEL, now), 20.0)
        # fractional remainder rounds UP to >= 1 while still open.
        self.assertEqual(s.circuit_retry_after(MODEL, now + 19.25), 1.0)
        self.assertEqual(s.circuit_retry_after(MODEL, now + 20.0), 0.0)
        # No entry at all -> 0 (nothing to wait on).
        s2 = _state()
        self.assertEqual(s2.circuit_retry_after(MODEL, now), 0.0)


class TestEscalatingBackoff(unittest.TestCase):
    """Auto-trip windows escalate ``circuit_base`` -> doubling ->
    ``circuit_max``; re-trip before expiry extends rather than shortens."""

    def test_first_open_is_circuit_base(self) -> None:
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now)
        self.assertEqual(s.circuit_open(MODEL, now + 19.9), True)
        self.assertEqual(s.circuit_open(MODEL, now + 20.0), False)

    def test_each_later_trip_doubles(self) -> None:
        # A sustained account limit must back off HARDER, not re-circle at the
        # base window forever (that just re-trips every 20s and keeps the
        # window perpetually open).
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now)                 # 20s   open_until=1020
        s.circuit_trip(MODEL, now + 5.0)            # double -> 40s after now+5
        self.assertTrue(s.circuit_open(MODEL, now + 5.0 + 39.9))
        self.assertFalse(s.circuit_open(MODEL, now + 5.0 + 40.0))
        s.circuit_trip(MODEL, now + 10.0)          # double -> 80s after now+10
        self.assertTrue(s.circuit_open(MODEL, now + 10.0 + 79.9))
        self.assertFalse(s.circuit_open(MODEL, now + 10.0 + 80.0))

    def test_backoff_caps_at_circuit_max(self) -> None:
        # Escalation must not run away unbounded on a long outage.
        cfg = SidecarConfig(
            pools={MODEL: list(P)}, default_cooldown=600.0,
            circuit_base=20.0, circuit_max=120.0,
        )
        s = RoutingState(cfg, shuffle_pools=False)
        now = 1000.0
        # 20 -> 40 -> 80 -> 160 capped at 120.
        for _ in range(4):
            s.circuit_trip(MODEL, now)
        # The 4th auto-trip would double 80 to 160 but the cap holds at 120.
        self.assertTrue(s.circuit_open(MODEL, now + 119.9))
        self.assertFalse(s.circuit_open(MODEL, now + 120.0))

    def test_retrip_before_expiry_extends_not_shortens(self) -> None:
        # A re-trip while still open must take the LATER of current/new
        # open_until -- a fresh failure must not let an early re-trip pull the
        # backoff window IN (that would let a chatty client repeatedly shrink
        # its own wait).
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now)                 # open_until=1020, backoff=20
        # Second trip very early: new auto window = now+5 + 40 = 1045, which
        # is later than the current 1020 -> extends to 1045.
        s.circuit_trip(MODEL, now + 5.0)
        self.assertTrue(s.circuit_open(MODEL, now + 44.0))
        self.assertFalse(s.circuit_open(MODEL, now + 45.0))


class TestExplicitRetryAfter(unittest.TestCase):
    """Upstream-supplied ``Retry-After`` overrides the escalating window."""

    def test_explicit_retry_after_overrides_window(self) -> None:
        # If the upstream 429 carries Retry-After, honour it verbatim instead
        # of the escalating guess -- the upstream knows its own window.
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now, retry_after=50.0)
        self.assertTrue(s.circuit_open(MODEL, now + 49.9))
        self.assertFalse(s.circuit_open(MODEL, now + 50.0))

    def test_explicit_retry_after_does_not_advance_escalation(self) -> None:
        # An explicit hint must not disturb the auto-escalation counter, so a
        # later AUTO trip resumes escalating from where it left off (otherwise
        # a single hinted re-trip would reset the backoff to base and a
        # sustained limit would never escalate).
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now)             # backoff -> 20
        s.circuit_trip(MODEL, now + 5.0, retry_after=10.0)  # hint, escalation untouched
        # Next AUTO trip resumes from backoff=20 -> doubles to 40.
        s.circuit_trip(MODEL, now + 50.0)
        self.assertTrue(s.circuit_open(MODEL, now + 50.0 + 39.9))
        self.assertFalse(s.circuit_open(MODEL, now + 50.0 + 40.0))


class TestCircuitProbe(unittest.TestCase):
    """Half-open gate admits exactly ONE probe per open->closed transition."""

    def test_probe_true_once_after_window_lapses(self) -> None:
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now)             # open_until=1020
        # Still inside the window -> no probe yet.
        self.assertFalse(s.circuit_probe(MODEL, now + 10.0))
        # Window lapsed -> first probe admitted (sets probing=True).
        self.assertTrue(s.circuit_probe(MODEL, now + 20.0))

    def test_probe_false_on_second_call_until_resolved(self) -> None:
        # The whole point: while half-open only ONE probe goes upstream. A
        # second client request in the same half-open window must NOT also
        # forward -- that would re-flood upstream probing the same dead limit.
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now)            # open_until=1020
        self.assertTrue(s.circuit_probe(MODEL, now + 20.0))
        self.assertFalse(s.circuit_probe(MODEL, now + 20.0))
        self.assertFalse(s.circuit_probe(MODEL, now + 21.0))
        # The probe's 2xx resets the circuit -> a later trip starts fresh and
        # admits a fresh probe after its window lapses.
        s.circuit_reset(MODEL, P)
        s.circuit_trip(MODEL, now + 100.0)    # fresh 20s window
        self.assertTrue(s.circuit_probe(MODEL, now + 100.0 + 20.0))

    def test_retrip_clears_probe_flag(self) -> None:
        # A probe that fails re-trips the circuit; a subsequent probe must be
        # gated again until the NEW window lapses, not float through because
        # probing was left True.
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now)
        self.assertTrue(s.circuit_probe(MODEL, now + 20.0))   # probe out
        s.circuit_trip(MODEL, now + 20.0)                      # probe failed -> re-trip
        # probing now False again; the re-trip doubled the window to 40s
        # (open_until = now+60), so no probe admitted while that new window
        # is open...
        self.assertFalse(s.circuit_probe(MODEL, now + 20.0 + 5.0))
        self.assertFalse(s.circuit_probe(MODEL, now + 20.0 + 39.9))
        # ...but once the doubled window lapses, exactly one fresh probe is
        # admitted.
        self.assertTrue(s.circuit_probe(MODEL, now + 20.0 + 40.0))


class TestCircuitReset(unittest.TestCase):
    """``circuit_reset`` closes a tripped circuit AND clears its collateral
    cooldowns; returns ``False`` (no-op) on an untripped pool."""

    def test_reset_closes_circuit(self) -> None:
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now)
        self.assertTrue(s.circuit_open(MODEL, now))
        s.circuit_reset(MODEL, P)
        self.assertNotIn(MODEL, s.pool_circuit)
        self.assertEqual(s.circuit_retry_after(MODEL, now), 0.0)

    def test_reset_clears_collateral_cooldowns(self) -> None:
        # The 600s per-provider cooldowns were collateral damage from an
        # account-wide limit, NOT per-provider faults. Keeping them would
        # leave the pool desperate -- exactly the bug. Reset must drop them so
        # all_hot goes False.
        s = _state()
        now = 1000.0
        _cool_all(s, now)
        # Sanity: every provider cooled.
        for p in P:
            self.assertTrue(s.cooldown_is_hot(p, now))
        s.circuit_trip(MODEL, now)
        s.circuit_reset(MODEL, P)
        # Every collateral cooldown cleared -- the pool is no longer desperate.
        self.assertEqual(s.cooldowns, {})
        self.assertFalse(s.all_hot(P, now))

    def test_reset_on_untripped_pool_returns_false_and_keeps_cooldowns(self) -> None:
        # REGRESSION for D2: circuit_reset must NOT nuke legitimate,
        # unrelated per-provider cooldowns when no circuit was ever tripped
        # (a normal success does not signal account-wide recovery). Returns
        # False and leaves the cooldown on the dead provider intact.
        s = _state()
        now = 1000.0
        # A legitimate cooldown on P[0] (e.g. a dead/slow provider cooling, or
        # the 2xx-fallback stampede cooldown) -- NOT collateral from a trip.
        s.cooldown_trigger(P[0], now)
        self.assertTrue(s.cooldown_is_hot(P[0], now))
        self.assertNotIn(MODEL, s.pool_circuit)
        self.assertFalse(s.circuit_reset(MODEL, P))
        self.assertNotIn(MODEL, s.pool_circuit)
        # The legitimate cooldown SURVIVES -- not wiped by an ordinary 2xx.
        self.assertTrue(s.cooldown_is_hot(P[0], now))
        self.assertEqual(s.cooldowns, {P[0]: s.cooldowns[P[0]]})


class TestAllHot(unittest.TestCase):
    """``all_hot`` is False while any provider is cold, True only when all hot
    -- the account-wide limit signal the feedback path keys on."""

    def test_false_when_some_providers_cold(self) -> None:
        s = _state()
        now = 1000.0
        s.cooldown_trigger(P[0], now)
        s.cooldown_trigger(P[2], now)
        # P[1], P[3], P[4] still cold -> not all hot.
        self.assertFalse(s.all_hot(P, now))
        self.assertTrue(s.all_hot([P[0], P[2]], now))  # sub-list all hot

    def test_true_only_when_all_hot(self) -> None:
        s = _state()
        now = 1000.0
        _cool_all(s, now)
        self.assertTrue(s.all_hot(P, now))
        # A single cold provider breaks it.
        del s.cooldowns[P[3]]
        self.assertFalse(s.all_hot(P, now))

    def test_empty_list_not_all_hot(self) -> None:
        # ``all([])`` is vacuously True; the contract wants ``all_hot`` to be
        # False for an empty set so a malformed guard can't spuriously trip the
        # circuit.
        s = _state()
        self.assertFalse(s.all_hot([], 0.0))


class TestPlanPooledRequestCircuitOpen(unittest.TestCase):
    """``plan_pooled_request`` short-circuits with a positive
    ``circuit_open_for`` and NO ``forward_body`` when the circuit is open."""

    def test_open_returns_positive_wait_and_no_forward_body(self) -> None:
        # The proxy must answer 429 locally when the circuit is open -- so the
        # plan neither builds nor returns a forward body, and tells the proxy
        # how long to wait.
        s = _state()
        now = 1000.0
        _cool_all(s, now)
        s.circuit_trip(MODEL, now)             # open_until = 1020.0
        body = {"model": MODEL, "prompt_cache_key": "abc",
                "messages": [{"role": "user", "content": "hi"}]}
        plan = plan_pooled_request(s, body, now + 5.0)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan["pooled_model"], MODEL)
        self.assertEqual(plan["providers"], P)
        self.assertEqual(plan["session_key"], "abc")
        self.assertEqual(plan["session_source"], "cache_key")
        self.assertEqual(plan["pin"], None)
        self.assertEqual(plan["keep_list"], None)
        self.assertTrue(plan["desperate"])
        self.assertIsNone(plan.get("forward_body"))
        self.assertGreater(plan["circuit_open_for"], 0.0)
        # 20s window, 5s elapsed -> 15s remaining.
        self.assertAlmostEqual(plan["circuit_open_for"], 15.0)
        self.assertIsInstance(plan["circuit_open_for"], float)

    def test_open_body_untouched_no_upstream_rewrite(self) -> None:
        # Because there is no forward_body, the client's original body dict
        # must NOT have been rewritten in place (no ``model``/``fallbacks``
        # mutation) -- the proxy never touches upstream for an open circuit.
        s = _state()
        now = 1000.0
        _cool_all(s, now)
        s.circuit_trip(MODEL, now)
        body = {"model": MODEL, "prompt_cache_key": "abc", "messages": []}
        plan = plan_pooled_request(s, body, now)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertIsNone(plan.get("forward_body"))
        self.assertEqual(body["model"], MODEL)            # untouched
        self.assertNotIn("fallbacks", body)               # never added


class TestDesperatePrimaryOnlyRegression(unittest.TestCase):
    """REGRESSION for the real bug: with every provider cooled, one client
    request must make exactly ONE upstream call -- ``forward_body`` parses to
    ``fallbacks == []`` -- not three (primary + 2 fallbacks) which held the
    account rate-limit window open for ~10 minutes.

    NB: this path is reached when the COOLDOWNS are hot but the CIRCUIT is
    NOT yet tripped (e.g. the 429s are cooling providers but no all_hot trip
    has fired yet, or the circuit just expired). The desperate flag runs on
    cooldown state, independent of the circuit."""

    def test_desperate_emits_empty_fallbacks(self) -> None:
        # All providers cooled -> desperate -> primary only. This is the line
        # that deletes the 3x amplification.
        s = _state()
        now = 1000.0
        _cool_all(s, now)
        body = {"model": MODEL, "prompt_cache_key": "abc",
                "messages": [{"role": "user", "content": "hi"}]}
        # Circuit NOT tripped -> closed path -> forward body present.
        plan = plan_pooled_request(s, body, now)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan["desperate"])
        self.assertIsNone(plan["circuit_open_for"])   # circuit closed path
        self.assertIn("forward_body", plan)
        decoded = json.loads(plan["forward_body"])
        # Primary present, fallbacks EMPTY -> one upstream call, not three.
        expected_primary = f"{plan['keep_list'][0]}/{MODEL}"
        self.assertEqual(decoded["model"], expected_primary)
        self.assertEqual(decoded["fallbacks"], [])
        self.assertEqual(len(decoded["fallbacks"]), 0)

    def test_one_request_one_upstream_callback_count(self) -> None:
        # Stronger form of the regression: count the upstream calls the body
        # would provoke. Primary + len(fallbacks) == 1, never 3, when
        # desperate. This is the invariant that was broken.
        s = _state()
        now = 1000.0
        _cool_all(s, now)
        body = {"model": MODEL, "prompt_cache_key": "abc", "messages": []}
        plan = plan_pooled_request(s, body, now)
        assert plan is not None
        decoded = json.loads(plan["forward_body"])
        upstream_calls = 1 + len(decoded["fallbacks"])
        self.assertEqual(upstream_calls, 1,
                         "desperate request must not amplify to 3 upstream "
                         "calls -- that is the storm")


class TestNonDesperateKeepsTwoFallbacks(unittest.TestCase):
    """Non-desperate requests still send primary + 2 fallbacks (unchanged)."""

    def test_non_desperate_emits_two_fallbacks(self) -> None:
        # A healthy pool must NOT be throttled to primary-only by the fix --
        # the cap stays primary + 2 fallbacks exactly as before.
        s = _state()
        now = 1000.0
        # Fresh state: all providers cold (not in cooldown) -> desperate False.
        body = {"model": MODEL, "prompt_cache_key": "abc",
                "messages": [{"role": "user", "content": "hi"}]}
        plan = plan_pooled_request(s, body, now)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertFalse(plan["desperate"])
        self.assertIsNone(plan["circuit_open_for"])
        self.assertIn("forward_body", plan)
        decoded = json.loads(plan["forward_body"])
        # primary + exactly 2 fallbacks (the existing cap, untouched).
        expected_fallbacks = [
            f"{p}/{MODEL}" for p in plan["keep_list"][1:3]
        ]
        self.assertEqual(decoded["fallbacks"], expected_fallbacks)
        self.assertEqual(len(decoded["fallbacks"]), 2)
        self.assertEqual(
            decoded["model"], f"{plan['keep_list'][0]}/{MODEL}"
        )


class TestPurgeExpiredClearsCircuit(unittest.TestCase):
    """``purge_expired`` retains a lapsed circuit entry until it is genuinely
    stale so the escalating ``backoff`` survives a recovery hole.

    Dropping a closed entry the instant its window lapsed would destroy the
    escalation: the next trip would restart at ``circuit_base`` instead of
    doubling. A lapsed, non-probing entry is kept until
    ``now >= open_until + circuit_max`` (well past any plausible re-trip),
    so escalation survives a lapse while the map still stays bounded. A
    half-open entry with a probe in flight is always kept until the probe
    resolves -- purge must not drop the gate and let a second probe through.
    """

    def test_lapsed_entry_survives_short_lapse(self) -> None:
        # open_until=1020 (circuit_base=20). Lapsed but not yet stale: the
        # escalating backoff must survive so a later re-trip DOUBLES, not
        # restarts at base.
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now)
        self.assertIn(MODEL, s.pool_circuit)
        s.purge_expired(now + 20.0)            # just lapsed
        self.assertIn(MODEL, s.pool_circuit)
        # Still alive well past the lapse, but before the stale horizon
        # (open_until + circuit_max = 1020 + 120 = 1140 absolute; check at
        # 1139, just before the horizon).
        s.purge_expired(now + 139.0)
        self.assertIn(MODEL, s.pool_circuit)

    def test_lapsed_entry_dropped_only_at_stale_horizon(self) -> None:
        # Only once now >= open_until + circuit_max is the lapsed entry
        # genuinely stale (no plausible re-trip this late) and reclaimed.
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now)             # open_until=1020, horizon=1140
        s.purge_expired(now + 140.0)           # 1140 absolute == horizon
        self.assertNotIn(MODEL, s.pool_circuit)

    def test_half_open_entry_with_pending_probe_is_kept(self) -> None:
        # An entry awaiting its one probe must survive purge until the probe
        # lands (reset/trip); otherwise purge would drop the gate and let a
        # second probe sneak through.
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now)
        self.assertTrue(s.circuit_probe(MODEL, now + 20.0))  # probing=True
        s.purge_expired(now + 20.0)
        self.assertIn(MODEL, s.pool_circuit)
        self.assertTrue(s.pool_circuit[MODEL]["probing"])


class TestPlanPooledRequestHalfOpenProbe(unittest.TestCase):
    """REGRESSION for D1: the first request after the open window lapses is
    THE half-open probe (one upstream call), and the immediately following
    request is short-circuited so the pool cannot flood upstream while
    half-open."""

    def test_first_request_after_lapse_is_the_probe(self) -> None:
        # Trip -> lapse the window -> the very next request wins the probe:
        # probe True, a single upstream call (forward_body, fallbacks == []),
        # not the open short-circuit.
        s = _state()
        now = 1000.0
        _cool_all(s, now)                      # all hot -> desperate
        s.circuit_trip(MODEL, now)             # open_until = 1020
        # Advance past the open window: circuit_open is False but the entry
        # still exists (half-open).
        self.assertFalse(s.circuit_open(MODEL, now + 20.0))
        self.assertIn(MODEL, s.pool_circuit)
        body = {"model": MODEL, "prompt_cache_key": "abc",
                "messages": [{"role": "user", "content": "hi"}]}
        plan = plan_pooled_request(s, body, now + 20.0)
        self.assertIsNotNone(plan)
        assert plan is not None
        # THIS request is the probe.
        self.assertTrue(plan["probe"])
        # A probe is forwardable (single upstream call), NOT short-circuited.
        self.assertIsNone(plan["circuit_open_for"])
        self.assertIn("forward_body", plan)
        decoded = json.loads(plan["forward_body"])
        # A probe must be a SINGLE upstream call: primary present, fallbacks
        # EMPTY -- never a 3-lane fan-out to test a rate limit.
        self.assertEqual(decoded["model"], f"{plan['keep_list'][0]}/{MODEL}")
        self.assertEqual(decoded["fallbacks"], [])
        # The probe flag was set on the entry.
        self.assertTrue(s.pool_circuit[MODEL]["probing"])

    def test_second_request_while_half_open_is_short_circuited(self) -> None:
        # While the probe is in flight, the very next request must NOT also
        # forward -- it short-circuits like the open case (positive
        # circuit_open_for, no forward_body) so the pool cannot race the
        # in-flight probe against the still-limited account.
        s = _state()
        now = 1000.0
        _cool_all(s, now)
        s.circuit_trip(MODEL, now)             # open_until = 1020
        body = {"model": MODEL, "prompt_cache_key": "abc",
                "messages": [{"role": "user", "content": "hi"}]}
        # First request after lapse wins the probe. (A fresh dict each call:
        # rewrite_body mutates its parsed dict in place, so reusing it would
        # make the second pooled_gate reject the suffixed model key.)
        plan1 = plan_pooled_request(s, body, now + 20.0)
        assert plan1 is not None
        self.assertTrue(plan1["probe"])
        # Second request in the same half-open window: probe already in
        # flight -> short-circuit.
        body2 = {"model": MODEL, "prompt_cache_key": "abc",
                 "messages": [{"role": "user", "content": "hi"}]}
        plan2 = plan_pooled_request(s, body2, now + 20.1)
        self.assertIsNotNone(plan2)
        assert plan2 is not None
        self.assertFalse(plan2["probe"])
        self.assertIsNotNone(plan2["circuit_open_for"])
        self.assertGreaterEqual(plan2["circuit_open_for"], 1.0)
        self.assertNotIn("forward_body", plan2)


class TestEscalationSurvivesLapse(unittest.TestCase):
    """REGRESSION for D1: tripping, letting the window lapse, purging, and
    re-tripping must produce a window DOUBLE the first -- the escalating
    ``backoff`` survives a lapse because ``purge_expired`` retains the lapsed
    entry until the stale horizon. (Dropping it on lapse restarted the next
    trip at ``circuit_base`` -- escalation never escalated across a lapse.)"""

    def test_retrip_after_lapse_doubles_not_resets_to_base(self) -> None:
        # Default circuit_base=20, circuit_max=120.
        s = _state()
        now = 1000.0
        s.circuit_trip(MODEL, now)             # open_until = 1020, backoff 20
        self.assertEqual(s.pool_circuit[MODEL]["backoff"], 20.0)
        # Let the window lapse, then purge. A broken purge would drop the
        # lapsed entry here, losing the backoff=20 so the next trip restarts
        # at base 20.
        s.purge_expired(now + 20.0)
        self.assertIn(MODEL, s.pool_circuit)  # entry retained (stale-horizon)
        # Re-trip now: backoff must DOUBLE (40), so the new window opens for
        # 40s past the re-trip -- NOT 20s (which would mean escalation reset).
        s.circuit_trip(MODEL, now + 21.0)
        self.assertEqual(s.pool_circuit[MODEL]["backoff"], 40.0)
        self.assertTrue(s.circuit_open(MODEL, now + 21.0 + 39.9))
        self.assertFalse(s.circuit_open(MODEL, now + 21.0 + 40.0))


class TestFastDesperatePrimaryOnlyRegression(unittest.TestCase):
    """REGRESSION for D3: a desperate ``/fast`` plan must yield lane bodies
    with ``fallbacks == []`` -- one call per lane -- not the default 2
    fallbacks. Before the fix ``_lane_body`` ignored ``desperate`` and fanned
    out 2 lanes x 3 providers = 6 upstream calls, the exact amplification the
    circuit fix exists to kill."""

    def test_desperate_fast_lanes_are_primary_only(self) -> None:
        s = _state()
        now = 1000.0
        # Cool every provider -> both lanes build_send_order-desperate.
        _cool_all(s, now)
        parsed = {
            "model": MODEL,
            "prompt_cache_key": "desperate-fast",
            "messages": [{"role": "user", "content": "hi"}],
        }
        plan = plan_fast_request(s, parsed, now)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan["desperate"])
        # Lane A is primary-only: one upstream call, not three.
        body_a = json.loads(plan["body_a"])
        self.assertEqual(body_a["fallbacks"], [])
        self.assertEqual(body_a["model"], f"{plan['lane_a'][0]}/{MODEL}")
        # Lane B (if present) is primary-only too.
        if plan["body_b"] is not None:
            body_b = json.loads(plan["body_b"])
            self.assertEqual(body_b["fallbacks"], [])
            self.assertEqual(body_b["model"], f"{plan['lane_b'][0]}/{MODEL}")
        # One call per lane, never 3x amplification.
        calls_a = 1 + len(json.loads(plan["body_a"])["fallbacks"])
        self.assertEqual(calls_a, 1)


class TestRewriteBodyMaxFallbacks(unittest.TestCase):
    """``rewrite_body``'s ``max_fallbacks`` kwarg is the lever
    ``plan_pooled_request`` uses; cover it directly."""

    def test_default_two_fallbacks(self) -> None:
        body = {"model": MODEL, "messages": []}
        out = rewrite_body(body, MODEL, ["a", "b", "c", "d"])
        decoded = json.loads(out)
        self.assertEqual(decoded["fallbacks"], [f"b/{MODEL}", f"c/{MODEL}"])

    def test_zero_emits_empty_list(self) -> None:
        body = {"model": MODEL, "messages": []}
        out = rewrite_body(body, MODEL, ["a", "b", "c", "d"], max_fallbacks=0)
        self.assertEqual(json.loads(out)["fallbacks"], [])

    def test_three_uncaps_within_bounds(self) -> None:
        body = {"model": MODEL, "messages": []}
        out = rewrite_body(body, MODEL, ["a", "b", "c", "d"], max_fallbacks=3)
        self.assertEqual(
            json.loads(out)["fallbacks"],
            [f"b/{MODEL}", f"c/{MODEL}", f"d/{MODEL}"],
        )

    def test_slice_past_end_is_safe(self) -> None:
        # A 2-provider ring with max_fallbacks=2 forwards the one remaining
        # provider and slices no further; max_fallbacks=0 forwards none.
        body = {"model": MODEL, "messages": []}
        out = rewrite_body(body, MODEL, ["a", "b"], max_fallbacks=2)
        self.assertEqual(json.loads(out)["fallbacks"], [f"b/{MODEL}"])
        out0 = rewrite_body(body, MODEL, ["a", "b"], max_fallbacks=0)
        self.assertEqual(json.loads(out0)["fallbacks"], [])


if __name__ == "__main__":
    unittest.main()

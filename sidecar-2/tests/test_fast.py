"""Tests for the fast-race (`/fast/v1`) decision helpers.

Covers:

* ``RoutingState.assign_pin_pair`` / ``_least_loaded`` dual-pin load counting
* ``RoutingState.build_fast_lanes`` — odd/even ring split, pin_b swap, cap
* ``fast_lane_feedback`` — fast-lane cooldown rule (primary cooled when a
  fallback serves, skipped intermediates too)
* ``apply_fast_feedback`` — error-path cool + lane-slot re-pin
* ``is_complete`` — terminal SSE markers
* ``pick_winner`` — complete-first, biggest-partial, response-beats-exception
* ``plan_fast_request`` — disjoint lane bodies + bookkeeping record

Uses stdlib ``unittest`` only, importlib loading (``sidecar-2`` has a
hyphen), same conventions as ``test_routing.py``.
"""

from __future__ import annotations

import importlib
import json
import time
import unittest

_config = importlib.import_module("sidecar-2.config")
_state_mod = importlib.import_module("sidecar-2.state")
_fast = importlib.import_module("sidecar-2.fast")
_pooled = importlib.import_module("sidecar-2.pooled")
SidecarConfig = _config.SidecarConfig
RoutingState = _state_mod.RoutingState
fast_lane_feedback = _fast.fast_lane_feedback
is_complete = _fast.is_complete
pick_winner = _fast.pick_winner
plan_fast_request = _fast.plan_fast_request
apply_fast_feedback = _pooled.apply_fast_feedback

# Pool used across the tests: nvidia-1 ... nvidia-10.
P = [f"nvidia-{i}" for i in range(1, 11)]


def _state(providers=None) -> RoutingState:
    """Fresh RoutingState over a 10-provider pool, deterministic order."""
    cfg = SidecarConfig(
        pools={"z-ai/glm-5.2": list(providers or P)}, default_cooldown=600.0
    )
    return RoutingState(cfg, shuffle_pools=False)


class TestBuildFastLanes(unittest.TestCase):
    """``RoutingState.build_fast_lanes`` — odd/even split + pin_b swap."""

    def test_seven_provider_ring_from_pin_zero(self) -> None:
        # The user's example: ring 1-2-3-4-5-6-7 -> A [1,3,5], B [2,4,6].
        provs = P[:7]
        s = _state(provs)
        now = time.time()
        with s.lock():
            lane_a, lane_b, desperate = s.build_fast_lanes(provs, 0, 1, now)
        self.assertEqual(lane_a, [provs[0], provs[2], provs[4]])
        self.assertEqual(lane_b, [provs[1], provs[3], provs[5]])
        self.assertFalse(desperate)

    def test_pin_b_cold_swapped_into_ring_position_one(self) -> None:
        s = _state()
        now = time.time()
        # pin_b = 5 (nvidia-6), far from position 1 -> swapped there.
        with s.lock():
            lane_a, lane_b, _ = s.build_fast_lanes(P, 0, 5, now)
        self.assertEqual(lane_b[0], P[5])
        self.assertEqual(lane_a[0], P[0])

    def test_pin_b_hot_no_swap(self) -> None:
        s = _state()
        now = time.time()
        with s.lock():
            s.cooldown_trigger(P[5], now)
            lane_a, lane_b, _ = s.build_fast_lanes(P, 0, 5, now)
        # No swap: lane B takes ring[1] (P[1], cold) instead of hot P[5].
        self.assertEqual(lane_b[0], P[1])
        self.assertEqual(lane_a[0], P[0])

    def test_rotation_from_pin_three(self) -> None:
        s = _state()
        now = time.time()
        with s.lock():
            lane_a, lane_b, _ = s.build_fast_lanes(P, 3, 4, now)
        # Ring rotates to start at P[3]; pin_b=P[4] is already at ring[1].
        self.assertEqual(lane_a[0], P[3])
        self.assertEqual(lane_b[0], P[4])
        self.assertEqual(lane_a, [P[3], P[5], P[7]])

    def test_one_provider_pool_lane_b_empty(self) -> None:
        provs = P[:1]
        s = _state(provs)
        now = time.time()
        with s.lock():
            lane_a, lane_b, _ = s.build_fast_lanes(provs, 0, None, now)
        self.assertEqual(lane_a, provs)
        self.assertEqual(lane_b, [])

    def test_cap_holds_with_ten_cold_providers(self) -> None:
        s = _state()
        now = time.time()
        with s.lock():
            lane_a, lane_b, _ = s.build_fast_lanes(P, 0, 1, now)
        self.assertLessEqual(len(lane_a), 3)
        self.assertLessEqual(len(lane_b), 3)


class TestAssignPinPair(unittest.TestCase):
    """``RoutingState.assign_pin_pair`` — two distinct pins + continuity."""

    def test_new_session_two_distinct_pins(self) -> None:
        s = _state()
        now = time.time()
        with s.lock():
            pin_a, pin_b = s.assign_pin_pair("s1", P, now)
        self.assertIsNotNone(pin_b)
        self.assertNotEqual(pin_a, pin_b)

    def test_second_call_same_session_same_pair(self) -> None:
        s = _state()
        now = time.time()
        with s.lock():
            a1, b1 = s.assign_pin_pair("s1", P, now)
            a2, b2 = s.assign_pin_pair("s1", P, now + 1)
        self.assertEqual((a1, b1), (a2, b2))

    def test_one_provider_pool_pin_b_none(self) -> None:
        provs = P[:1]
        s = _state(provs)
        now = time.time()
        with s.lock():
            pin_a, pin_b = s.assign_pin_pair("s1", provs, now)
        self.assertEqual(pin_a, 0)
        self.assertIsNone(pin_b)

    def test_least_loaded_counts_pin2(self) -> None:
        s = _state()
        now = time.time()
        with s.lock():
            # Fill the pool with fast sessions: index 0 carries pin, index 1
            # carries pin2 — both must count as load for the next assignment.
            s.pins["f1"] = {"pin": 0, "pin2": 1, "seen": now}
            nxt = s._least_loaded(P, now, frozenset())
        self.assertNotIn(nxt, (0, 1))


class TestFastLaneFeedback(unittest.TestCase):
    """``fast_lane_feedback`` — the fast-lane cooldown rule."""

    def test_served_by_primary_no_feedback(self) -> None:
        self.assertEqual(
            fast_lane_feedback(["1", "3", "5"], "1", 200), (None, [])
        )

    def test_served_by_first_fallback_cools_primary(self) -> None:
        self.assertEqual(
            fast_lane_feedback(["1", "3", "5"], "3", 200), ("3", ["1"])
        )

    def test_served_by_second_fallback_cools_primary_and_skipped(self) -> None:
        self.assertEqual(
            fast_lane_feedback(["1", "3", "5"], "5", 200), ("5", ["1", "3"])
        )

    def test_non_2xx_no_feedback(self) -> None:
        self.assertEqual(
            fast_lane_feedback(["1", "3", "5"], "3", 500), (None, [])
        )
        self.assertEqual(
            fast_lane_feedback(["1", "3", "5"], "3", None), (None, [])
        )

    def test_served_none_no_feedback(self) -> None:
        self.assertEqual(
            fast_lane_feedback(["1", "3", "5"], None, 200), (None, [])
        )


class TestApplyFastFeedback(unittest.TestCase):
    """``apply_fast_feedback`` — error path cools + re-pins the lane slot."""

    def test_err_path_cools_primary_and_repins_lane_b(self) -> None:
        s = _state()
        now = time.time()
        with s.lock():
            s.assign_pin_pair("s1", P, now)
        lane_keep = [P[2], P[3], P[4]]
        repinned, circuit_note = apply_fast_feedback(
            s,
            session_key="s1",
            providers=P,
            lane="b",
            lane_keep=lane_keep,
            served_provider=None,
            response_status=500,
            error_str=None,
            pooled_model="z-ai/glm-5.2",
        )
        self.assertEqual(repinned, P[3])
        # 500 is per-provider only: no circuit event.
        self.assertIsNone(circuit_note)
        with s.lock():
            self.assertTrue(s.cooldown_is_hot(P[2], time.time()))
            self.assertEqual(s.pins["s1"]["pin2"], P.index(P[3]))

    def test_fallback_served_repins_and_keeps_stampede_cooldown(self) -> None:
        # When the circuit was NEVER tripped, a 2xx fallback must NOT nuke the
        # legitimate per-provider cooldowns. ``fast_lane_feedback`` for a 2xx
        # fallback off the primary cools the LAN PRIMARY (the stampede
        # cooldown) and re-pins to the server; ``circuit_reset`` is called but
        # returns False (no tripped circuit), so ``circuit_note`` stays None
        # and the freshly-triggered cooldown on the lane primary SURVIVES.
        # (Old WRONG behaviour asserted the cooldown was wiped + note "reset".)
        s = _state()
        now = time.time()
        with s.lock():
            s.assign_pin_pair("s1", P, now)
        lane_keep = [P[0], P[1], P[2]]
        self.assertIsNone(s.pool_circuit.get("z-ai/glm-5.2"))
        repinned, circuit_note = apply_fast_feedback(
            s,
            session_key="s1",
            providers=P,
            lane="a",
            lane_keep=lane_keep,
            served_provider=P[1],
            response_status=200,
            error_str=None,
            pooled_model="z-ai/glm-5.2",
        )
        # The lane re-pins to the server that answered.
        self.assertEqual(repinned, P[1])
        self.assertEqual(s.pins["s1"]["pin"], P.index(P[1]))
        # No circuit was tripped -> no recovery event; note stays None.
        self.assertIsNone(circuit_note)
        # The legitimate 2xx-fallback stampede cooldown on the lane primary
        # (P[0]) SURVIVES: an ordinary success does not signal account-wide
        # recovery, so collateral-cooldown clearing is not justified.
        with s.lock():
            self.assertTrue(s.cooldown_is_hot(P[0], time.time()))
        self.assertIsNone(s.pool_circuit.get("z-ai/glm-5.2"))

    def test_2xx_after_trip_resets_circuit_and_clears_collateral(self) -> None:
        # D2 recovery path: when the circuit WAS tripped, a 2xx really does
        # signal account-wide recovery. ``circuit_reset`` returns True ->
        # circuit_note == "reset" AND the collateral provider cooldowns are
        # cleared. This proves narrowed reset still recovers a real trip.
        s = _state()
        now = time.time()
        model = "z-ai/glm-5.2"
        with s.lock():
            s.assign_pin_pair("s1", P, now)
            # Trip the circuit: cool every provider (collateral), then trip.
            for p in P:
                s.cooldown_trigger(p, now)
            s.circuit_trip(model, now)
            self.assertTrue(s.circuit_open(model, now))
        lane_keep = [P[0], P[1], P[2]]
        repinned, circuit_note = apply_fast_feedback(
            s,
            session_key="s1",
            providers=P,
            lane="a",
            lane_keep=lane_keep,
            served_provider=P[1],
            response_status=200,
            error_str=None,
            pooled_model=model,
        )
        # Real recovery: circuit existed -> reset returns True -> "reset".
        self.assertEqual(circuit_note, "reset")
        self.assertEqual(repinned, P[1])
        # The collateral cooldowns are cleared (the pool is no longer
        # desperate after recovery), and the circuit entry is gone.
        with s.lock():
            for p in P:
                self.assertFalse(
                    s.cooldown_is_hot(p, time.time()),
                    f"collateral cooldown on {p} must be cleared on reset")
            self.assertIsNone(s.pool_circuit.get(model))


class TestIsComplete(unittest.TestCase):
    """``is_complete`` — terminal markers for buffered responses."""

    def test_non_stream_2xx_true(self) -> None:
        self.assertTrue(is_complete(200, False, b"{}", None))

    def test_stream_with_done_marker_true(self) -> None:
        body = b"data: {...}\n\ndata: [DONE]\n\n"
        self.assertTrue(is_complete(200, True, body, None))

    def test_stream_anthropic_message_stop_true(self) -> None:
        self.assertTrue(is_complete(200, True, b"event: message_stop\n", None))

    def test_truncated_stream_false(self) -> None:
        self.assertFalse(is_complete(200, True, b"data: {...}\n\n", None))

    def test_error_set_false(self) -> None:
        self.assertFalse(
            is_complete(200, False, b"{}", "IncompleteRead(...)")
        )

    def test_500_false(self) -> None:
        self.assertFalse(is_complete(500, False, b"oops", None))


def _rec(lane, status, body, finished, complete, error=None):
    return {
        "lane": lane,
        "status": status,
        "headers": [],
        "body": body,
        "is_stream": None,
        "served_provider": None,
        "error": error,
        "finished": finished,
        "complete": complete,
    }


class TestPickWinner(unittest.TestCase):
    """``pick_winner`` — complete-first, biggest-partial, response > error."""

    def test_complete_beats_bigger_partial(self) -> None:
        a = _rec("a", 200, b"x" * 100, 2.0, True)
        b = _rec("b", 200, b"y" * 999, 1.0, False)
        self.assertEqual(pick_winner([a, b]), 0)

    def test_two_partials_biggest_wins(self) -> None:
        a = _rec("a", 200, b"x" * 10, 1.0, False)
        b = _rec("b", 200, b"y" * 20, 2.0, False)
        self.assertEqual(pick_winner([a, b]), 1)

    def test_partial_tie_goes_lane_a(self) -> None:
        a = _rec("a", 200, b"x" * 10, 2.0, False)
        b = _rec("b", 200, b"y" * 10, 1.0, False)
        self.assertEqual(pick_winner([a, b]), 0)

    def test_both_errored_with_responses_earliest(self) -> None:
        a = _rec("a", 500, b"", 2.0, False)
        b = _rec("b", 429, b"", 1.0, False)
        self.assertEqual(pick_winner([a, b]), 1)

    def test_response_beats_transport_exception(self) -> None:
        a = _rec("a", None, b"", 1.0, False, error="TimeoutError")
        b = _rec("b", 500, b"", 2.0, False)
        self.assertEqual(pick_winner([a, b]), 1)

    def test_all_exceptions_lane_a(self) -> None:
        a = _rec("a", None, b"", 1.0, False, error="TimeoutError")
        b = _rec("b", None, b"", 2.0, False, error="TimeoutError")
        self.assertEqual(pick_winner([a, b]), 0)


class TestPlanFastRequest(unittest.TestCase):
    """``plan_fast_request`` — lane bodies + bookkeeping."""

    def test_bodies_disjoint_and_bookkeeping_written(self) -> None:
        s = _state()
        parsed = {
            "model": "z-ai/glm-5.2",
            "prompt_cache_key": "fast-test-1",
            "messages": [{"role": "user", "content": "hi"}],
        }
        plan = plan_fast_request(s, parsed, time.time())
        self.assertIsNotNone(plan)
        self.assertEqual(plan["pooled_model"], "z-ai/glm-5.2")
        self.assertNotEqual(plan["lane_a"][0], plan["lane_b"][0])

        body_a = json.loads(plan["body_a"])
        body_b = json.loads(plan["body_b"])
        self.assertEqual(body_a["model"], f"{plan['lane_a'][0]}/z-ai/glm-5.2")
        self.assertEqual(body_b["model"], f"{plan['lane_b'][0]}/z-ai/glm-5.2")
        self.assertEqual(
            body_a["fallbacks"],
            [f"{p}/z-ai/glm-5.2" for p in plan["lane_a"][1:]],
        )
        self.assertEqual(
            body_b["fallbacks"],
            [f"{p}/z-ai/glm-5.2" for p in plan["lane_b"][1:]],
        )
        # Lane bodies are independent copies (no shared mutable state).
        self.assertIsNot(body_a, body_b)

        rec = s.pins[plan["session_key"]]
        self.assertEqual(rec["pin"], P.index(plan["lane_a"][0]))
        self.assertEqual(rec["pin2"], P.index(plan["lane_b"][0]))

    def test_non_pooled_returns_none(self) -> None:
        s = _state()
        self.assertIsNone(
            plan_fast_request(s, {"model": "openai/gpt-5"}, time.time())
        )
        self.assertIsNone(plan_fast_request(s, None, time.time()))

    def test_one_provider_pool_body_b_none(self) -> None:
        provs = P[:1]
        s = _state(provs)
        parsed = {"model": "z-ai/glm-5.2", "prompt_cache_key": "k"}
        plan = plan_fast_request(s, parsed, time.time())
        self.assertEqual(plan["lane_b"], [])
        self.assertIsNone(plan["body_b"])
        self.assertIsNone(plan["pin_b"])


if __name__ == "__main__":
    unittest.main()

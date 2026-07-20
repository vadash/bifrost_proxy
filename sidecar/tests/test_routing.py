"""Tests for the sidecar routing helpers.

Covers the two pure decision functions introduced to split the inline routing
logic out of ``proxy.py``:

* ``RoutingState.build_send_order`` (Step 1) — rotates the provider ring to
  start at the pinned provider, keeps the WHOLE ring but moves hot
  (in-cooldown) providers to the END so Bifrost only reaches them last.
* ``fallback_feedback`` (Step 2) — decides, for a 2xx response served by a
  fallback, which provider to re-pin to and which (if any) to cool.

Plus a cooldown regression guarding ``build_send_order``'s dependency on
cooldown state.

Uses stdlib ``unittest`` only (matches the sidecar's stdlib-only constraint).
"""

from __future__ import annotations

import time
import unittest

from sidecar.config import SidecarConfig
from sidecar.state import RoutingState, fallback_feedback

# Pool used across the tests: nvidia-1 ... nvidia-10.
P = [f"nvidia-{i}" for i in range(1, 11)]


def _state() -> RoutingState:
    """Fresh RoutingState over a 10-provider pool with default cooldown."""
    cfg = SidecarConfig(pools={"z-ai/glm-5.2": list(P)}, default_cooldown=600.0)
    return RoutingState(cfg)


class TestBuildSendOrder(unittest.TestCase):
    """``RoutingState.build_send_order`` — send-order + desperate flag."""

    def test_no_cooldowns_pin_zero_returns_full_ring(self) -> None:
        s = _state()
        now = time.time()
        with s.lock():
            send_order, desperate = s.build_send_order(P, 0, now)
        # Rotation from 0 == the pool itself; nothing hot -> full ring, not desperate.
        self.assertEqual(send_order, P)
        self.assertFalse(desperate)

    def test_no_cooldowns_pin_three_rotates(self) -> None:
        s = _state()
        now = time.time()
        with s.lock():
            send_order, desperate = s.build_send_order(P, 3, now)
        expected = ["nvidia-4", "nvidia-5", "nvidia-6", "nvidia-7", "nvidia-8",
                    "nvidia-9", "nvidia-10", "nvidia-1", "nvidia-2", "nvidia-3"]
        self.assertEqual(send_order, expected)
        self.assertFalse(desperate)

    def test_one_hot_provider_appended_last(self) -> None:
        s = _state()
        now = time.time()
        s.cooldown_trigger("nvidia-2", now)
        with s.lock():
            send_order, desperate = s.build_send_order(P, 0, now)
        # Hot provider moved to the very end, full ring preserved, not desperate.
        self.assertEqual(send_order[-1], "nvidia-2")
        self.assertNotIn("nvidia-2", send_order[:-1])
        self.assertEqual(len(send_order), 10)
        self.assertFalse(desperate)

    def test_all_hot_is_desperate_keeps_full_ring(self) -> None:
        s = _state()
        now = time.time()
        for p in P:
            s.cooldown_trigger(p, now)
        with s.lock():
            send_order, desperate = s.build_send_order(P, 0, now)
        # Every provider is hot -> rotation order (all appended as hot), desperate.
        self.assertEqual(send_order, P)
        self.assertTrue(desperate)


class TestFallbackFeedback(unittest.TestCase):
    """``fallback_feedback`` — post-2xx-fallback re-pin + cooldown decision."""

    K = ["nvidia-1", "nvidia-3", "nvidia-4", "nvidia-5"]

    def test_fallback_skips_one_cools_first_skipped(self) -> None:
        # served by nvidia-4 (skipped nvidia-3) -> re-pin to nvidia-4, cool nvidia-3.
        self.assertEqual(
            fallback_feedback(self.K, "nvidia-4", 200),
            ("nvidia-4", "nvidia-3"),
        )

    def test_served_is_primary_no_action(self) -> None:
        # served == forced primary -> no fallback, nothing to do.
        self.assertEqual(
            fallback_feedback(self.K, "nvidia-1", 200),
            (None, None),
        )

    def test_served_is_second_never_cooled(self) -> None:
        # served == keep_list[1] (the first-skip candidate served immediately)
        # -> re-pin, but never cool a provider that served.
        self.assertEqual(
            fallback_feedback(self.K, "nvidia-3", 200),
            ("nvidia-3", None),
        )

    def test_429_is_not_fallback_success(self) -> None:
        self.assertEqual(
            fallback_feedback(self.K, "nvidia-4", 429),
            (None, None),
        )

    def test_503_is_not_fallback_success(self) -> None:
        self.assertEqual(
            fallback_feedback(self.K, "nvidia-4", 503),
            (None, None),
        )

    def test_singleton_keeps_list_served_self_no_action(self) -> None:
        self.assertEqual(
            fallback_feedback(["nvidia-1"], "nvidia-1", 200),
            (None, None),
        )

    def test_empty_or_none_keeps_list_no_action(self) -> None:
        self.assertEqual(fallback_feedback(None, "nvidia-4", 200), (None, None))
        self.assertEqual(fallback_feedback([], "nvidia-4", 200), (None, None))


class TestCooldownRegression(unittest.TestCase):
    """Guards ``build_send_order``'s read of cooldown state via cooldown_trigger."""

    def test_trigger_then_purge_clears_hot(self) -> None:
        s = _state()
        now = time.time()
        s.cooldown_trigger("nvidia-2", now)
        # Hot immediately after trigger.
        self.assertTrue(s.cooldown_is_hot("nvidia-2", now))
        # After the cooldown window expires + a purge, it is no longer hot.
        later = now + 601
        s.purge_expired(later)
        self.assertFalse(s.cooldown_is_hot("nvidia-2", later))
        self.assertNotIn("nvidia-2", s.cooldowns)


class TestColdStartRandomization(unittest.TestCase):
    """On a fresh pool, sessions must spread across providers -- not all
    stampede onto the lowest-index one. ``assign_pin`` breaks least-loaded
    ties uniformly at random."""

    def test_fresh_start_spreads_across_providers(self) -> None:
        s = _state()
        now = time.time()
        pins = set()
        with s.lock():
            for i in range(50):
                pins.add(s.assign_pin(f"session-{i}", P, now))
        # With 50 fresh sessions on a 10-provider pool and all loads tied at 0,
        # random tie-breaking must spread them across more than one provider.
        # (P(all 50 picked identical index) == (1/10)^49 -- a guard, not luck.)
        self.assertGreater(len(pins), 1)

    def test_single_candidate_deterministic(self) -> None:
        # When only one cold candidate exists, no randomness -- pick it.
        s = _state()
        now = time.time()
        for p in P[1:]:  # cool nvidia-2..nvidia-10, leaving only nvidia-1 cold
            s.cooldown_trigger(p, now)
        with s.lock():
            pin = s.assign_pin("session-x", P, now)
        self.assertEqual(pin, 0)


if __name__ == "__main__":
    unittest.main()

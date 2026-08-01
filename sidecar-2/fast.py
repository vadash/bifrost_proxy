"""Fast-race (`/fast/v1`) decision helpers: pure functions + planner.

Mirrors the pure-function style of ``plan_pooled_request`` /
``fallback_feedback`` in ``state.py``: no globals, ``state`` passed
explicitly. These cover the fast endpoint's extra decisions on top of the
single path:

* ``plan_fast_request`` — build the two disjoint lanes (odd/even ring split)
  and per-lane request bodies for the race.
* ``fast_lane_feedback`` — per-lane cooldown/re-pin rule (deliberately
  different from the single path: the lane primary IS cooled when a
  fallback serves).
* ``is_complete`` — did a buffered response actually finish (terminal SSE
  marker for streams)?
* ``pick_winner`` — choose which lane's buffered response to relay
  (complete-first, then biggest partial).
"""

from __future__ import annotations

import copy

from .identity import derive_session_key
from .predicates import is_2xx
from .state import RoutingState, pooled_gate, rewrite_body


def plan_fast_request(
    state: RoutingState, parsed: dict, now: float
) -> dict | None:
    """Decide fast two-lane routing for ``parsed`` and build lane bodies.

    Returns ``None`` iff ``parsed`` isn't a dict or its model isn't a
    declared pool key (the non-pooled passthrough signal -- the proxy then
    forwards verbatim as a single request, same as the non-fast path).

    Otherwise, under ``state.lock()``: purge -> derive session key ->
    circuit decision -> ``assign_pin_pair`` -> ``build_fast_lanes``, then
    write the bookkeeping record so the pins always track the actual lane
    primaries. The body rewrite runs outside the lock (same rationale as
    ``plan_pooled_request``): each lane gets a deep-copied body with
    ``model = "{lane_primary}/{pooled}"`` and ``fallbacks`` for the rest of
    the lane (lanes are already <= 3, so the upstream cap holds).

    Circuit states (mirroring ``plan_pooled_request`` so the ``/fast`` lane
    never bypasses the breaker):

    * OPEN: the race is NOT built -- an early-return dict carries
      ``circuit_open_for`` (seconds until re-open for a probe) plus
      ``pooled_model`` / ``providers`` / ``session_key`` / ``session_source``
      so ``_proxy_fast`` answers 429 locally. Without this the two disjoint
      lanes would double the 3x amplification the circuit exists to stop.
    * HALF-OPEN: consult ``circuit_probe``. If it returns False a probe is
      already in flight on the single path -- short-circuit with
      ``circuit_open_for`` (``max(1.0, circuit_retry_after(...))``) rather
      than racing two lanes against the still-limited account. If it returns
      True this ``/fast`` request IS the probe: the race still runs, but each
      lane is forced to ``max_fallbacks=0`` (primary-only) so the probe does
      not fan out 2 lanes x 3 providers = 6 upstream calls into the limit it
      is testing.
    * CLOSED: the normal race.

    DESPERATE (all providers hot): even on the normal race path, ``_lane_body``
    passes ``max_fallbacks=0`` so a desperate ``/fast`` request sends one
      call per lane (primary-only) instead of three -- mirroring
      ``plan_pooled_request`` and killing the same 3x-per-lane amplification
      the single-path fix targets.
    """
    gate = pooled_gate(state, parsed)
    if gate is None:
        return None
    pooled_model, providers = gate

    session_key, session_source = derive_session_key(parsed)

    with state.lock():
        state.purge_expired(now)
        if state.circuit_open(pooled_model, now):
            # Pool is rate-limited end to end; do not race. Emit the same
            # short-circuit shape as plan_pooled_request so /fast answers
            # locally instead of sending two more refused lanes upstream.
            return {
                "pooled_model": pooled_model,
                "providers": providers,
                "session_key": session_key,
                "session_source": session_source,
                "pin_a": None,
                "pin_b": None,
                "lane_a": [],
                "lane_b": [],
                "desperate": True,
                "body_a": None,
                "body_b": None,
                "circuit_open_for": state.circuit_retry_after(
                    pooled_model, now
                ),
            }
        # Half-open: a lapsed entry still exists. Admit the probe / hold the
        # race back so /fast cannot flood upstream while half-open.
        if pooled_model in state.pool_circuit:
            if state.circuit_probe(pooled_model, now):
                probe = True
            else:
                # A probe is already in flight (the single path admitted it).
                # Short-circuit exactly like the open case rather than racing
                # two more lanes into the still-limited account.
                return {
                    "pooled_model": pooled_model,
                    "providers": providers,
                    "session_key": session_key,
                    "session_source": session_source,
                    "pin_a": None,
                    "pin_b": None,
                    "lane_a": [],
                    "lane_b": [],
                    "desperate": True,
                    "body_a": None,
                    "body_b": None,
                    "circuit_open_for": max(
                        1.0, state.circuit_retry_after(pooled_model, now)
                    ),
                }
        else:
            probe = False
        pin_a, pin_b = state.assign_pin_pair(session_key, providers, now)
        if pin_a is None or not providers:
            # Empty pool: pooled_gate should have rejected this already;
            # guard returns None so the proxy falls back to passthrough
            # instead of IndexError'ing on lane_a[0].
            return None
        lane_a, lane_b, desperate = state.build_fast_lanes(
            providers, pin_a, pin_b, now
        )
        state.set_lane_pins(
            session_key,
            providers.index(lane_a[0]),
            providers.index(lane_b[0]) if lane_b else None,
            now,
        )
        # Desperate OR this is the half-open probe -> each lane is primary
        # only so one /fast request does not fan out 2 lanes x 3 providers.
        force_primary_only = desperate or probe

    def _lane_body(lane: list[str]) -> bytes:
        return rewrite_body(
            copy.deepcopy(parsed), pooled_model, lane,
            max_fallbacks=0 if force_primary_only else 2,
        )

    return {
        "pooled_model": pooled_model,
        "providers": providers,
        "session_key": session_key,
        "session_source": session_source,
        "pin_a": providers.index(lane_a[0]),
        "pin_b": providers.index(lane_b[0]) if lane_b else None,
        "lane_a": lane_a,
        "lane_b": lane_b,
        "desperate": desperate,
        "body_a": _lane_body(lane_a),
        "body_b": _lane_body(lane_b) if lane_b else None,
        "circuit_open_for": None,
    }


def fast_lane_feedback(
    lane_keep: list[str],
    served: str | None,
    response_status: int | None,
) -> tuple[str | None, list[str]]:
    """Fast-lane 2xx feedback rule -> ``(repin_to, cooldown_providers)``.

    Deliberately different from the single path's ``fallback_feedback``:
    when a fallback serves, the lane primary IS cooled (plus every skipped
    intermediate before the server); the server itself is never cooled.

    * Not 2xx (``response_status`` None or outside 200..299) -> ``(None, [])``.
    * Empty lane, ``served is None``, or the primary served -> ``(None, [])``.
    * ``served`` in ``lane_keep[1:]`` -> ``(served, lane_keep[:i])`` where
      ``i = lane_keep.index(served)`` (lane ``[1,3,5]`` served by ``3`` ->
      cool ``[1]``; served by ``5`` -> cool ``[1,3]``).
    * ``served`` outside the lane (unexpected) -> ``(served, [lane_keep[0]])``.
    """
    if not is_2xx(response_status):
        return None, []
    if not lane_keep or served is None or served == lane_keep[0]:
        return None, []
    if served in lane_keep[1:]:
        return served, lane_keep[: lane_keep.index(served)]
    return served, [lane_keep[0]]


def is_complete(
    status: int | None,
    is_stream: bool,
    body: bytes,
    error: str | None,
) -> bool:
    """True iff the buffered response finished fully.

    ``False`` on transport error or non-2xx. Non-stream 2xx is complete by
    definition (``resp.read()`` returned). Stream 2xx is complete iff the
    buffered SSE carries a terminal marker: ``data: [DONE]``
    (chat/completions), ``message_stop`` (Anthropic), or
    ``response.completed`` (responses API).
    """
    if error is not None:
        return False
    if not is_2xx(status):
        return False
    if not is_stream:
        return True
    return (
        b"data: [DONE]" in body
        or b"message_stop" in body
        or b"response.completed" in body
    )


def pick_winner(records: list[dict]) -> int:
    """Index of the winning lane record. ``records[0]`` is lane A.

    First matching rule wins:
    1. Any ``complete`` record -> smallest ``finished`` (ties -> lane A).
    2. Else any 2xx record with a non-empty body -> biggest body (ties ->
       lane A). This is the biggest-partial rule.
    3. Else any record with an actual HTTP status (a real error response
       beats a transport exception) -> smallest ``finished``.
    4. Else ``0`` (the caller fabricates a 502).
    """
    complete = [r for r in records if r["complete"]]
    if complete:
        return records.index(min(complete, key=lambda r: r["finished"]))
    partial = [
        r
        for r in records
        if is_2xx(r["status"]) and r["body"]
    ]
    if partial:
        return records.index(max(partial, key=lambda r: len(r["body"])))
    responded = [r for r in records if r["status"] is not None]
    if responded:
        return records.index(min(responded, key=lambda r: r["finished"]))
    return 0

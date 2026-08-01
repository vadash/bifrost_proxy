"""Post-response concerns for pooled requests: feedback + logging.

These free functions were lifted verbatim out of ``Handler`` (Step 3 of
the proxy.py refactor) so the HTTP-transport module stays thin. They take
``state``/``cfg``/the writers/``headers``/``command``/``path`` explicitly
instead of reading them off ``self`` -- the only change is parameter
plumbing, no behaviour. ``write_logs`` was split into the two specialised
writers below; no alias remains.

* ``apply_feedback`` / ``apply_fast_feedback`` — adjust pin + cooldowns
  after Bifrost replies (shared core ``_apply_feedback``), and drive the
  pool-level circuit breaker (429 trip / 2xx reset) under the same lock.
  Both return ``(repin, circuit_note)``.

Both are pooled-only; non-pooled requests never reach either (the proxy's
``pooled_model is not None`` gate fast-paths them out).
"""

from __future__ import annotations

from datetime import datetime
import time

from .config import SidecarConfig
from .io_jsonl import JsonlWriter, redact_headers
from .state import RoutingState, fallback_feedback
from .fast import fast_lane_feedback


def _is_error_path(error_str: str | None, response_status: int | None) -> bool:
    """Whole-chain-failure path: transport error, 5xx, or 429.

    The 429 case feeds BOTH the per-provider cooldown (handled here, of the
    forced primary) AND the pool-level circuit breaker (handled by the
    caller, which trips the whole pool when ``all_hot`` fires). A 5xx or
    transport error is per-provider only and does not touch the circuit.
    """
    return (
        error_str is not None
        or (response_status is not None and response_status >= 500)
        or (response_status == 429)
    )


def _apply_feedback(
    state: RoutingState,
    *,
    primary: str,
    repin_to: str | None,
    cool_providers: list[str],
    advance_provider: str | None,
    error_str: str | None,
    response_status: int | None,
    mutate,
    providers: list[str],
    pooled_model: str,
    retry_after: float | None = None,
) -> tuple[str | None, str | None]:
    """Shared post-response feedback: on 2xx fallback cool ``cool_providers``
    and re-pin to ``repin_to``; on the error path cool ``primary`` and advance
    to ``advance_provider``. ``mutate(provider, now)`` performs the actual
    pin-slot write (``state.re_pin`` or ``state.re_pin_lane``).

    Pool-level circuit breaker lives inside the SAME ``state.lock()`` block,
    never a second acquisition:

    * 429 -- after cooling ``primary`` the account-wide limit is the likely
      cause, so when EVERY ``providers`` is now hot (``state.all_hot``) the
      pool circuit trips (``state.circuit_trip``) and the returned note is
      ``"tripped"``. Upstream ``retry_after`` (seconds) is forwarded when the
      header was present, else the circuit uses its escalating backoff. A 429
      that re-trips while half-open goes straight through ``circuit_trip``,
      which clears the in-flight ``probing`` flag -- no special-case needed.
    * 2xx served (``repin_to is not None`` OR a plain 2xx) -- Collateral
      per-provider cooldown clearing is ONLY justified when the circuit was
      actually open/tripped: those 600s cooldowns were collateral damage from
      an account-wide limit, so on genuine recovery ``circuit_reset`` closes
      the circuit AND drops them, returning ``"reset"``. A 2xx on a pool whose
      circuit never tripped returns ``False`` and touches nothing -- a normal
      successful fallback must NOT nuke unrelated, legitimate per-provider
      cooldowns (dead/slow-provider cooling, the 2xx-fallback stampede
      cooldown); in that case ``circuit_note`` stays ``None``.

    Returns ``(provider re-pinned to or None, circuit_note)`` where
    ``circuit_note`` is ``"tripped"`` / ``"reset"`` / ``None`` (``None`` is the
    common case -- no circuit event, cooldowns left as they were).
    """
    now_fb = time.time()
    repinned: str | None = None
    circuit_note: str | None = None
    with state.lock():
        state.purge_expired(now_fb)
        if repin_to is not None:
            for p in cool_providers:
                state.cooldown_trigger(p, now_fb)
            mutate(repin_to, now_fb)
            repinned = repin_to
            if state.circuit_reset(pooled_model, providers):
                circuit_note = "reset"
        elif _is_error_path(error_str, response_status):
            state.cooldown_trigger(primary, now_fb)
            if advance_provider is not None:
                mutate(advance_provider, now_fb)
                repinned = advance_provider
            if response_status == 429 and state.all_hot(providers, now_fb):
                state.circuit_trip(pooled_model, now_fb, retry_after)
                circuit_note = "tripped"
        else:
            # Plain 2xx served by the primary (no fallback, no error): if the
            # circuit was genuinely open/tripped, the account limit has now
            # cleared -> close it and drop the stale per-provider cooldowns it
            # left behind. A 2xx on an untripped pool does NOT touch
            # cooldowns (no collateral damage to undo).
            if response_status is not None and 200 <= response_status < 300:
                if state.circuit_reset(pooled_model, providers):
                    circuit_note = "reset"
    return repinned, circuit_note


def apply_feedback(
    state: RoutingState,
    *,
    session_key: str,
    providers: list[str],
    keep_list: list[str],
    served_provider: str | None,
    response_status: int | None,
    error_str: str | None,
    pooled_model: str,
    retry_after: float | None = None,
) -> tuple[str | None, str | None]:
    """Adjust pin + cooldowns after Bifrost replies.

    ``served_provider`` is the provider name extracted from the response
    body (or None when the terminal event never arrived -- a normal
    outcome, in which case the fallback path is skipped).

    ``pooled_model`` is the pool key; ``retry_after`` is the upstream
    ``Retry-After`` (seconds, or None) threaded from the proxy so a 429
    that trips the pool circuit honours the server's backoff hint.

    Returns ``(provider actually re-pinned to this request, circuit_note)``.
    ``repin`` is ``repin_to`` (or ``keep_list[1]`` on the whole-chain
    failure path), else ``None`` when nothing was re-pinned -- the decided
    value the logger records verbatim. ``circuit_note`` is ``"tripped"`` /
    ``"reset"`` / ``None`` -- threaded into the decision log's ``circuit``
    field by the proxy. Collateral per-provider cooldown clearing ONLY happens
    on genuine circuit recovery (a 2xx that actually closes a tripped
    circuit); an ordinary 2xx that finds no circuit leaves cooldowns as-is.
    """
    repin_to, cool_provider = fallback_feedback(
        keep_list, served_provider, response_status
    )
    return _apply_feedback(
        state,
        primary=keep_list[0],
        repin_to=repin_to,
        cool_providers=[cool_provider] if cool_provider is not None else [],
        advance_provider=keep_list[1] if len(keep_list) > 1 else None,
        error_str=error_str,
        response_status=response_status,
        mutate=lambda provider, now: state.re_pin(
            session_key, provider, providers, now
        ),
        providers=providers,
        pooled_model=pooled_model,
        retry_after=retry_after,
    )


def apply_fast_feedback(
    state: RoutingState,
    *,
    session_key: str,
    providers: list[str],
    lane: str,
    lane_keep: list[str],
    served_provider: str | None,
    response_status: int | None,
    error_str: str | None,
    pooled_model: str,
    retry_after: float | None = None,
) -> tuple[str | None, str | None]:
    """Adjust one lane's pin slot + cooldowns after its race leg replies.

    Same shape and lock discipline as ``apply_feedback``, but uses the
    fast-lane rule (``fast_lane_feedback``): on 2xx fallback the lane
    primary AND skipped intermediates are cooled and the lane re-pins to
    the server; on the error path (transport error, 5xx, or 429 -- the
    same predicate as ``apply_feedback``) the lane primary is cooled and
    the lane advances to ``lane_keep[1]``. Other non-2xx (4xx except 429)
    produce no feedback -- mirrors the single path deliberately.

    Pool-level circuit: a 429 with every provider hot trips the pool circuit
    (note ``"tripped"``). A 2xx resets it (note ``"reset"``) ONLY when the
    circuit was genuinely tripped -- collateral per-provider cooldown clearing
    happens solely on real recovery; an ordinary 2xx on an untripped pool
    leaves cooldowns as-is (note ``None``). ``pooled_model`` /
    ``retry_after`` are plumbed straight into ``_apply_feedback``. Returns
    ``(provider re-pinned to, circuit_note)``.
    """
    repin_to, cool_list = fast_lane_feedback(
        lane_keep, served_provider, response_status
    )
    return _apply_feedback(
        state,
        primary=lane_keep[0],
        repin_to=repin_to,
        cool_providers=cool_list,
        advance_provider=lane_keep[1] if len(lane_keep) > 1 else None,
        error_str=error_str,
        response_status=response_status,
        mutate=lambda provider, now: state.re_pin_lane(
            session_key, provider, providers, lane, now
        ),
        providers=providers,
        pooled_model=pooled_model,
        retry_after=retry_after,
    )


def write_capture(
    cfg: SidecarConfig,
    capture_writer: JsonlWriter,
    headers,
    command: str,
    path: str,
    *,
    request_body_parsed,
    response_status: int | None,
    is_stream: bool | None,
    served_provider: str | None,
    error_str: str | None,
) -> None:
    """Append one capture.jsonl record (only when --capture was passed)."""
    if not cfg.capture_enabled:
        return
    record = {
        "ts": datetime.now().isoformat(),
        "method": command,
        "path": path,
        "request_headers": redact_headers(headers.items()),
        "request_body": request_body_parsed,
        "response_status": response_status,
        "streaming": is_stream,
        "served_provider": served_provider,
    }
    if error_str is not None:
        record["error"] = error_str
    capture_writer.safe(record)


def write_decision_log(
    state: RoutingState,
    log_writer: JsonlWriter,
    *,
    session_key: str,
    session_source: str,
    pin: int,
    keep_list: list[str] | None,
    served_provider: str | None,
    repin: str | None,
    response_status: int | None,
    desperate: bool,
    circuit: str | None = None,
) -> None:
    """Append one sidecar.log decision line (always, for pooled requests).

    ``circuit`` records the pool-level circuit-breaker event for this
    request: ``"open"`` when the sidecar answered locally without
    forwarding, ``"tripped"`` when a 429 opened the circuit, ``"reset"``
    when a 2xx closed it, ``"probe"`` for a half-open probe sent through.
    Absent/None -> no circuit event (the common case).
    """
    served = served_provider
    fell_back = (
        served is not None
        and bool(keep_list)
        and served != keep_list[0]
    )
    now_log = time.time()
    with state.lock():
        hot = [
            p for p in keep_list
            if state.cooldown_is_hot(p, now_log)
        ] if keep_list is not None else []
    log_writer.safe({
        "ts": datetime.now().isoformat(),
        "session": session_key[:12] if session_key else None,
        "source": session_source,
        "pin": pin,
        "primary": keep_list[0] if keep_list else None,
        "ring": keep_list if keep_list else None,
        "cooldowns": hot,
        "served": served,
        "fell_back": fell_back,
        "repin": repin,
        "status": response_status,
        "circuit": circuit,
    })

"""Post-response concerns for pooled requests: feedback + logging.

These two free functions were lifted verbatim out of ``Handler`` (Step 3 of
the proxy.py refactor) so the HTTP-transport module stays thin. They take
``state``/`cfg``/the two writers/``headers``/``command``/``path`` explicitly
instead of reading them off ``self`` -- the only change is parameter
plumbing, no behaviour.

* ``apply_feedback`` — adjust pin + cooldowns after Bifrost replies.
* ``write_logs`` — emit capture.jsonl (if enabled) + sidecar.log for pooled
  requests.

Both are pooled-only; non-pooled requests never reach either (the proxy's
``pooled_model is not None`` gate fast-paths them out).
"""

from __future__ import annotations

from datetime import datetime
import time

from .config import SidecarConfig
from .io_jsonl import JsonlWriter, redact_headers
from .state import RoutingState, fallback_feedback


def apply_feedback(
    state: RoutingState,
    *,
    session_key: str,
    providers: list[str],
    keep_list: list[str],
    served_provider: str | None,
    response_status: int | None,
    error_str: str | None,
) -> str | None:
    """Adjust pin + cooldowns after Bifrost replies.

    ``served_provider`` is the provider name extracted from the response
    body (or None when the terminal event never arrived -- a normal
    outcome, in which case the fallback path is skipped).

    Returns the provider actually re-pinned to this request (``repin_to``,
    or ``keep_list[1]`` on the whole-chain-failure path), else ``None`` when
    nothing was re-pinned -- the decided value the logger records verbatim.
    """
    now_fb = time.time()
    served = served_provider

    repin_to, cool_provider = fallback_feedback(
        keep_list, served, response_status
    )

    repinned: str | None = None
    with state.lock():
        state.purge_expired(now_fb)
        err_path = (
            error_str is not None
            or (response_status is not None and response_status >= 500)
            or (response_status == 429)
        )
        if repin_to is not None:
            # Bifrost walked to a fallback -> cool the first skipped
            # provider (stampede target); follow the server that answered.
            if cool_provider is not None:
                state.cooldown_trigger(cool_provider, now_fb)
            state.re_pin(session_key, repin_to, providers, now_fb)
            repinned = repin_to
        elif err_path:
            # Whole chain failed: cool the forced primary; advance one step.
            state.cooldown_trigger(keep_list[0], now_fb)
            if len(keep_list) > 1:
                state.re_pin(session_key, keep_list[1], providers, now_fb)
                repinned = keep_list[1]
    return repinned


def write_logs(
    state: RoutingState,
    cfg: SidecarConfig,
    capture_writer: JsonlWriter,
    log_writer: JsonlWriter,
    headers,
    command: str,
    path: str,
    *,
    session_key: str,
    session_source: str,
    pin: int,
    keep_list: list[str] | None,
    served_provider: str | None,
    repin: str | None,
    response_status: int | None,
    is_stream: bool | None,
    request_body_parsed,
    desperate: bool,
    error_str: str | None,
) -> None:
    """Emit capture.jsonl (if enabled) + sidecar.log for pooled requests.

    Non-pooled requests are transparent -- no capture.jsonl, no
    sidecar.log, no state, no headers.
    """
    # --- capture.jsonl (only if --capture was passed) ---
    if cfg.capture_enabled:
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

    # --- sidecar.log decision line (always for pooled) ---
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
        "desperate": desperate,
    })

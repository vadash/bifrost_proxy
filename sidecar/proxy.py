"""HTTP layer: the request handler + threaded server.

This module is intentionally thin -- it only deals with reading the request,
forwarding bytes to Bifrost, and relaying the response. All routing
decisions go through the injected ``RoutingState``; all file IO goes through
the injected ``JsonlWriter``s; all tunables come from the immutable
``SidecarConfig`` carried on the ``Sidecar`` server instance. The handler
reads them off ``self.server`` (standard ``ThreadingHTTPServer`` wiring),
so no module-level global is ever reached for.

Behaviour is byte-for-byte identical to legacy proxy.py for both pooled and
non-pooled requests; the only change is *where* each concern lives.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import HOP_BY_HOP, SidecarConfig
from .io_jsonl import JsonlWriter, parse_request_body, redact_headers
from .meta import parse_response_meta_nonstream, parse_response_meta_stream
from .state import RoutingState


class Sidecar(ThreadingHTTPServer):
    """One thread per connection so concurrent subagents don't block each other.

    Collaborators are attached as attributes and read by the handler via
    ``self.server`` -- dependency injection without monkey-patching stdlib.
    """

    daemon_threads = True

    # populated by main() before serve_forever()
    cfg: SidecarConfig        # immutable tunables + paths
    state: RoutingState       # thread-safe routing state (pins/map/cooldowns)
    capture_writer: JsonlWriter  # capture.jsonl appender (offline unless --capture)
    log_writer: JsonlWriter   # sidecar.log appender (always on, pooled only)


class Handler(BaseHTTPRequestHandler):
    """Forward to Bifrost.

    Pooled models get session-pinned routing; everything else is a transparent
    passthrough (verbatim bytes, no state, no headers, no logs).
    """

    protocol_version = "HTTP/1.1"

    # --- HTTP verb dispatch ---------------------------------------------------
    def do_GET(self): self.proxy()
    def do_POST(self): self.proxy()
    def do_PUT(self): self.proxy()
    def do_DELETE(self): self.proxy()
    def do_PATCH(self): self.proxy()
    def do_OPTIONS(self): self.proxy()
    def do_HEAD(self): self.proxy()

    # Silence default stderr logging (we do our own capture).
    def log_message(self, *a):
        pass

    # --- Convenience accessors for the injected collaborators -----------------
    @property
    def _cfg(self) -> SidecarConfig:
        return self.server.cfg

    @property
    def _state(self) -> RoutingState:
        return self.server.state

    @property
    def _capture(self) -> JsonlWriter:
        return self.server.capture_writer

    @property
    def _log(self) -> JsonlWriter:
        return self.server.log_writer

    # --- Helpers --------------------------------------------------------------
    def _build_forward_headers(self) -> dict[str, str]:
        """Copy request headers except hop-by-hop / host / content-length."""
        out: dict[str, str] = {}
        for name, value in self.headers.items():
            ln = name.lower()
            if ln in HOP_BY_HOP:
                continue
            if ln == "host":
                continue
            if ln == "content-length":
                continue
            out[name] = value  # preserve Authorization verbatim upstream
        return out

    @staticmethod
    def _filter_response_headers(getheaders) -> list[tuple[str, str]]:
        """Relay response headers except hop-by-hop and content-length."""
        out = []
        for name, value in getheaders():
            ln = name.lower()
            if ln in HOP_BY_HOP:
                continue
            if ln == "content-length":
                continue
            out.append((name, value))
        return out

    # --- Post-response feedback (pooled only) ---------------------------------
    def _apply_feedback(
        self, *, pooled_model, session_key, providers, keep_list,
        routing_info, response_id, response_status, error_str,
    ) -> None:
        """Adjust pin + cooldowns after Bifrost replies, and map resp-id.

        Mirrors the legacy "6b. Post-response feedback" block exactly, now
        expressed against the injected ``RoutingState``.
        """
        state = self._state
        now_fb = time.time()
        served = None
        primary = None
        is_fallback = None
        if isinstance(routing_info, dict) and routing_info:
            served = routing_info.get("provider")
            primary = routing_info.get("primary_provider")
            is_fallback = routing_info.get("is_fallback")

        with state.lock():
            state.purge_expired(now_fb)
            err_path = (
                error_str is not None
                or (response_status is not None and response_status >= 500)
                or (response_status == 429)
            )
            if (
                response_status is not None
                and 200 <= response_status < 300
                and is_fallback
                and served is not None
                and primary is not None
                and served != primary
            ):
                # Bifrost walked to a fallback -> primary failed downstream.
                state.cooldown_trigger(primary, now_fb)
                # Re-pin: follow the provider that actually served.
                state.re_pin(session_key, served, providers, now_fb)
            elif err_path:
                # Cool the primary we forced; advance pin one step.
                state.cooldown_trigger(keep_list[0], now_fb)
                if len(keep_list) > 1:
                    state.re_pin(session_key, keep_list[1], providers, now_fb)

            # Map response id -> session for future prev_response_id lookups.
            if response_id is not None:
                state.map_response(response_id, session_key, now_fb)

    # --- Logging (pooled only) ------------------------------------------------
    def _write_logs(
        self, *, pooled_model, session_key, session_source, pin, keep_list,
        routing_info, response_status, is_stream, request_body_parsed,
        has_previous_response_id, desperate, error_str,
    ) -> None:
        """Emit capture.jsonl (if enabled) + sidecar.log for pooled requests.

        Non-pooled requests are transparent -- no capture.jsonl, no
        sidecar.log, no state, no headers.
        """
        cfg = self._cfg
        state = self._state

        # --- capture.jsonl (only if --capture was passed) ---
        if cfg.capture_enabled:
            record = {
                "ts": datetime.now().isoformat(),
                "method": self.command,
                "path": self.path,
                "request_headers": redact_headers(self.headers.items()),
                "request_body": request_body_parsed,
                "has_previous_response_id": (
                    has_previous_response_id
                    if isinstance(request_body_parsed, dict)
                    else False
                ),
                "response_status": response_status,
                "streaming": is_stream,
                "routing_info": routing_info,
            }
            if error_str is not None:
                record["error"] = error_str
            self._capture.safe(record)

        # --- sidecar.log decision line (always for pooled) ---
        served = None
        is_fallback_val = None
        if isinstance(routing_info, dict) and routing_info:
            served = routing_info.get("provider")
            is_fallback_val = routing_info.get("is_fallback")
        # Reconstruct repin from pin/served for observability.
        now_log = time.time()
        with state.lock():
            entry = state.pins.get(session_key)
            repin = None
            if entry is not None:
                repin_idx = entry["pin"]
                providers = state.pools.get(pooled_model)
                if providers is not None and 0 <= repin_idx < len(providers):
                    repin = providers[repin_idx]
            hot = [
                p for p in keep_list
                if state.cooldown_is_hot(p, now_log)
            ] if keep_list is not None else []
        self._log.safe({
            "ts": datetime.now().isoformat(),
            "session": session_key[:12] if session_key else None,
            "source": session_source,
            "pin": pin,
            "primary": keep_list[0] if keep_list else None,
            "ring": keep_list if keep_list else None,
            "cooldowns": hot,
            "served": served,
            "is_fallback": is_fallback_val,
            "repin": repin,
            "status": response_status,
            "desperate": desperate,
        })

    # --- Core forwarding logic -----------------------------------------------
    def proxy(self):
        conn = None
        response_line_sent = False
        response_status = None
        is_stream = None
        routing_info = None
        response_id = None
        request_body_parsed = None
        has_previous_response_id = False
        error_str = None

        # --- Pooled-routing bookkeeping (only meaningful when pooled_model set) ---
        pooled_model = None       # the POOLS key (e.g. "z-ai/glm-5.2") or None
        forward_body = None       # bytes to forward upstream
        session_key = None
        session_source = None
        providers = None          # the pooled model's provider-name list
        pin = None
        keep_list = None          # kept[] ring used in the request + step-8 feedback
        kept = None
        desperate = False

        try:
            # 1. Read request body.
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""

            # Parse request body early so it's available in error paths.
            request_body_parsed = parse_request_body(body)
            if isinstance(request_body_parsed, dict):
                prv = request_body_parsed.get("previous_response_id")
                has_previous_response_id = prv is not None
            else:
                has_previous_response_id = False

            # 2. Build forward headers.
            fwd_headers = self._build_forward_headers()

            # --- 2b. Optional pooled routing: rewrite model+fallbacks,
            # pick pin, set state. ---
            cfg = self._cfg
            state = self._state
            forward_body = body  # default: verbatim passthrough
            if (
                isinstance(request_body_parsed, dict)
                and state.is_pooled(request_body_parsed.get("model"))
            ):
                pooled_model = request_body_parsed["model"]
                providers = state.pools[pooled_model]
                now = time.time()
                with state.lock():
                    state.purge_expired(now)
                    session_key, session_source = state.derive_session_key(
                        request_body_parsed
                    )
                    pin = state.assign_pin(session_key, providers, now)

                    # Ring: rotate starting at the pinned provider.
                    ring_names = list(providers[pin:]) + list(providers[:pin])
                    kept = [
                        p for p in ring_names
                        if not state.cooldown_is_hot(p, now)
                    ]
                    if not kept:
                        desperate = True
                        kept = list(ring_names)  # full ring, request still goes out
                    keep_list = list(kept)

                    # Rewrite request body dict: model -> "primary/pooled",
                    # fallbacks -> rest.
                    request_body_parsed["model"] = (
                        f"{kept[0]}/{pooled_model}"
                    )
                    request_body_parsed["fallbacks"] = [
                        f"{p}/{pooled_model}" for p in kept[1:]
                    ]
                    new_body = json.dumps(request_body_parsed).encode("utf-8")
                    forward_body = new_body

            # 3. Forward to upstream.
            conn = self._open_upstream()
            conn.request(self.command, self.path, body=forward_body, headers=fwd_headers)
            resp = conn.getresponse()

            # 4. Streaming detection.
            ctype = resp.getheader("Content-Type", "")
            is_stream = "text/event-stream" in ctype.lower()

            # 5. Status line.
            self.send_response(resp.status)
            response_line_sent = True
            response_status = resp.status

            # 6. Relay response headers except hop-by-hop and content-length.
            for name, value in self._filter_response_headers(resp.getheaders):
                self.send_header(name, value)

            # Pooled-only: expose sidecar routing decision to the client.
            if pooled_model is not None:
                self.send_header("x-sidecar-session", session_key[:12])
                self.send_header("x-sidecar-pin", keep_list[0])

            sse_buf = bytearray()
            if is_stream:
                # Stream: Connection: close, relay chunks as they arrive.
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = resp.read(cfg.chunk_size)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    sse_buf.extend(chunk)
            else:
                # Non-stream: buffer full body, send Content-Length.
                resp_body = resp.read()
                self.send_header("Content-Length", str(len(resp_body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(resp_body)
                routing_info, response_id = parse_response_meta_nonstream(resp_body)

            if is_stream:
                routing_info, response_id = parse_response_meta_stream(bytes(sse_buf))

            # --- 6b. Post-response feedback (pooled requests only). ---
            if pooled_model is not None:
                self._apply_feedback(
                    pooled_model=pooled_model,
                    session_key=session_key,
                    providers=providers,
                    keep_list=keep_list,
                    routing_info=routing_info,
                    response_id=response_id,
                    response_status=response_status,
                    error_str=error_str,
                )

            # 7. Connection close.
            self.close_connection = True

        except Exception as e:
            error_str = repr(e)
            if not response_line_sent:
                try:
                    self.send_error(502, "sidecar upstream error")
                except Exception:
                    pass

        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

            # Logging: pooled only. Non-pooled requests are transparent --
            # no capture.jsonl, no sidecar.log, no state, no headers.
            if pooled_model is not None:
                self._write_logs(
                    pooled_model=pooled_model,
                    session_key=session_key,
                    session_source=session_source,
                    pin=pin,
                    keep_list=keep_list,
                    routing_info=routing_info,
                    response_status=response_status,
                    is_stream=is_stream,
                    request_body_parsed=request_body_parsed,
                    has_previous_response_id=has_previous_response_id,
                    desperate=desperate,
                    error_str=error_str,
                )

            # Force connection close; never let an exception escape.
            self.close_connection = True

    def _open_upstream(self):
        import http.client
        cfg = self._cfg
        return http.client.HTTPConnection(
            cfg.upstream_host, cfg.upstream_port, timeout=cfg.upstream_timeout
        )

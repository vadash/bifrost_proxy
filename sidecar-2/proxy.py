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
import sys
import traceback
import http.client
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import HOP_BY_HOP, SidecarConfig
from .fast import is_complete, pick_winner, plan_fast_request
from .io_jsonl import JsonlWriter, parse_request_body
from .pooled import apply_fast_feedback, apply_feedback, write_capture, write_decision_log
from .predicates import is_sse_content_type
from .routing_info import extract_provider
from .sanitize import cap_max_tokens, model_needs_sanitize, sanitize_request_body
from .state import RoutingState, plan_pooled_request


class Sidecar(ThreadingHTTPServer):
    """One thread per connection so concurrent subagents don't block each other.

    Collaborators are attached as attributes and read by the handler via
    ``self.server`` -- dependency injection without monkey-patching stdlib.
    """

    daemon_threads = True

    # populated by main() before serve_forever()
    cfg: SidecarConfig        # immutable tunables + paths
    state: RoutingState       # thread-safe routing state (pins/cooldowns)
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
    def _filter_response_headers(headers) -> list[tuple[str, str]]:
        """Relay response headers except hop-by-hop and content-length."""
        out = []
        for name, value in headers:
            ln = name.lower()
            if ln in HOP_BY_HOP:
                continue
            if ln == "content-length":
                continue
            out.append((name, value))
        return out

    # --- Core forwarding logic -----------------------------------------------
    def proxy(self):
        self._response_line_sent = False
        conn = None
        # `/fast/v1/...` races pooled models over two disjoint lanes; the
        # prefix is stripped and everything else flows through unchanged.
        fast_mode = self.path.startswith("/fast/")
        upstream_path = self.path[len("/fast"):] if fast_mode else self.path
        response_status = None
        is_stream = None
        served_provider = None
        request_body_parsed = None
        error_str = None

        # --- Pooled-routing bookkeeping (only meaningful when pooled_model set) ---
        pooled_model = None       # the POOLS key (e.g. "z-ai/glm-5.2") or None
        forward_body = None       # bytes to forward upstream
        session_key = None
        session_source = None
        providers = None          # the pooled model's provider-name list
        pin = None
        keep_list = None          # kept[] ring used in the request + step-8 feedback
        desperate = False
        repin = None             # provider re-pinned this request (feedback), for logging

        try:
            # 1. Read request body.
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""

            # Parse request body early so it's available in error paths.
            request_body_parsed = parse_request_body(body)

            # 2. Build forward headers.
            fwd_headers = self._build_forward_headers()

            state = self._state
            forward_body = body  # default: verbatim passthrough

            # 2a. Request-body fixes (pooled AND passthrough):
            #   - cap max_tokens / max_completion_tokens at the hard ceiling
            #     for *every* request (missing or oversize -> 16000), all
            #     models and upstream providers
            #   - Claude-only: strip empty thinking blocks, rewrite OpenAI
            #     reasoning_effort -> Bedrock thinking.adaptive + output_config,
            #     mirror max_completion_tokens -> max_tokens
            # Re-serialize only when something actually changed, so clean
            # passthrough stays byte-verbatim.
            new = self._sanitize_body(request_body_parsed)
            if new is not None:
                forward_body = new

            # --- 2b. Optional pooled routing: rewrite model+fallbacks,
            # pick pin, set state. Fast mode races two lanes instead. ---
            if fast_mode:
                fast_plan = plan_fast_request(
                    state, request_body_parsed, time.time()
                )
                if fast_plan is not None:
                    self._proxy_fast(
                        fast_plan, fwd_headers, upstream_path,
                        request_body_parsed,
                    )
                    return
            else:
                plan = plan_pooled_request(state, request_body_parsed, time.time())
                if plan is not None:
                    pooled_model = plan["pooled_model"]
                    providers = plan["providers"]
                    session_key = plan["session_key"]
                    session_source = plan["session_source"]
                    pin = plan["pin"]
                    keep_list = plan["keep_list"]
                    desperate = plan["desperate"]
                    forward_body = plan["forward_body"]

            # Pooled-only headers to expose to the client after the relay.
            pooled_headers = (
                {"session": session_key[:12], "pin": keep_list[0]}
                if pooled_model is not None
                else None
            )

            # 3-6. Forward, detect stream, relay status/headers/body, extract provider.
            response_status, is_stream, served_provider = self._forward_and_relay(
                upstream_path, fwd_headers, forward_body, pooled_headers
            )

            # --- 6b. Post-response feedback (pooled requests only). ---
            if pooled_model is not None:
                repin = apply_feedback(
                    self._state,
                    session_key=session_key,
                    providers=providers,
                    keep_list=keep_list,
                    served_provider=served_provider,
                    response_status=response_status,
                    error_str=error_str,
                )

            # 7. Connection close.
            self.close_connection = True

        except Exception as e:
            error_str = repr(e)
            # Surface routing crashes to stderr so they're not silently
            # swallowed into a bare 502 (the sidecar.log decision line only
            # fires from the finally block for pooled requests that reached
            # upstream; a pre-upstream exception leaves no other trace).
            print(f"[sidecar] proxy() exception: {e!r}", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            if not self._response_line_sent:
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
                write_capture(
                    self._cfg, self._capture, self.headers,
                    self.command, self.path,
                    request_body_parsed=request_body_parsed,
                    response_status=response_status,
                    is_stream=is_stream,
                    served_provider=served_provider,
                    error_str=error_str,
                )
                write_decision_log(
                    self._state, self._log,
                    session_key=session_key,
                    session_source=session_source,
                    pin=pin,
                    keep_list=keep_list,
                    served_provider=served_provider,
                    repin=repin,
                    response_status=response_status,
                    desperate=desperate,
                )

            # Force connection close; never let an exception escape.
            self.close_connection = True

    def _sanitize_body(self, parsed) -> bytes | None:
        """Return a re-serialized body when ``parsed`` needed fixing, else
        ``None`` (caller keeps the original verbatim bytes).

        Two stages, the first unconditional and the second Claude-gated:

        1. ``cap_max_tokens`` forces ``max_tokens`` / ``max_completion_tokens``
           to the hard ceiling for *every* request (all models, all upstream
           providers) when the value is missing or exceeds the cap.
        2. ``sanitize_request_body`` applies the Claude-only thinking /
           reasoning_effort / max_tokens-mirror rewrites.

        Because both rewriters mutate ``parsed`` in place, a single full
        re-serialize at the end carries every changed field downstream.
        """
        if not isinstance(parsed, dict):
            return None
        changed = cap_max_tokens(parsed)
        if model_needs_sanitize(parsed.get("model")):
            changed = sanitize_request_body(parsed) is not None or changed
        if not changed:
            return None
        return json.dumps(parsed).encode("utf-8")

    def _forward_and_relay(
        self,
        upstream_path: str,
        fwd_headers: dict[str, str],
        forward_body: bytes,
        pooled_headers: dict | None,
    ) -> tuple[int | None, bool | None, str | None]:
        """Forward to Bifrost and relay the response back to the client.

        Steps 3-6 of the legacy ``proxy()``: open the upstream, send the
        request, detect streaming, emit the status line + filtered headers
        (plus the two ``x-sidecar-*`` headers from ``pooled_headers`` when
        pooled), relay the body (stream chunks vs buffered
        ``Content-Length``), then ``extract_provider`` on the buffered bytes
        and close the connection. Returns ``(status, is_stream,
        served_provider)`` -- all ``None`` when an error escapes the caller's
        ``except`` block (status was never read).
        """
        conn = None
        try:
            conn, resp, response_status, resp_headers, is_stream = self._open_upstream_meta(
                upstream_path, self.command, forward_body, fwd_headers,
            )

            # 4. Status line.
            self.send_response(response_status)
            self._response_line_sent = True

            # 6. Relay response headers except hop-by-hop and content-length.
            for name, value in self._filter_response_headers(resp.getheaders()):
                self.send_header(name, value)

            # Pooled-only: expose sidecar routing decision to the client.
            if pooled_headers is not None:
                self.send_header("x-sidecar-session", pooled_headers["session"])
                self.send_header("x-sidecar-pin", pooled_headers["pin"])

            served_provider = None
            sse_buf = bytearray()
            if is_stream:
                # Stream: Connection: close, relay chunks as they arrive.
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = resp.read(self._cfg.chunk_size)
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
                served_provider = extract_provider(resp_body, is_stream=False)

            if is_stream:
                served_provider = extract_provider(bytes(sse_buf), is_stream=True)

            return response_status, is_stream, served_provider
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _proxy_fast(self, plan, fwd_headers, upstream_path, request_body_parsed):
        """Race a pooled request over two disjoint lanes and relay the winner.

        One daemon thread per lane buffers the full upstream response, then
        applies its own feedback + logging (two sidecar.log records per fast
        request, one per lane, path keeps the ``/fast`` marker). The main
        handler thread answers as soon as a lane finishes *complete*; only
        when the first finisher is premature-truncated does it wait for the
        second lane (needed for biggest-partial-wins). The loser keeps
        running as a daemon -- never cancelled, never joined.
        """

        cfg = self._cfg
        state = self._state
        providers = plan["providers"]
        session_key = plan["session_key"]

        lanes = [("a", plan["lane_a"], plan["body_a"])]
        if plan["body_b"] is not None:
            lanes.append(("b", plan["lane_b"], plan["body_b"]))

        q: queue.Queue = queue.Queue()

        def run_lane(lane: str, lane_keep: list[str], lane_body: bytes) -> None:
            conn = None
            status = None
            resp_headers: list = []
            body = b""
            is_stream = None
            error = None
            try:
                conn, resp, status, resp_headers, is_stream = self._open_upstream_meta(
                    upstream_path, self.command, lane_body, fwd_headers,
                )
                try:
                    if is_stream:
                        buf = bytearray()
                        while True:
                            chunk = resp.read(cfg.chunk_size)
                            if not chunk:
                                break
                            buf.extend(chunk)
                        body = bytes(buf)
                    else:
                        body = resp.read()
                except http.client.IncompleteRead as e:
                    # Premature end: salvage the partial body.
                    body = e.partial or b""
                    error = repr(e)
            except Exception as e:
                body = b""
                error = repr(e)
                status = None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            finished = time.time()

            served_provider = (
                extract_provider(body, is_stream=bool(is_stream))
                if body else None
            )
            complete = is_complete(status, bool(is_stream), body, error)
            repin = apply_fast_feedback(
                state,
                session_key=session_key,
                providers=providers,
                lane=lane,
                lane_keep=lane_keep,
                served_provider=served_provider,
                response_status=status,
                error_str=error,
            )
            write_capture(
                cfg, self._capture, self.headers,
                self.command, self.path,
                request_body_parsed=request_body_parsed,
                response_status=status,
                is_stream=is_stream,
                served_provider=served_provider,
                error_str=error,
            )
            write_decision_log(
                state, self._log,
                session_key=session_key,
                session_source=plan["session_source"],
                pin=providers.index(lane_keep[0]),
                keep_list=lane_keep,
                served_provider=served_provider,
                repin=repin,
                response_status=status,
                desperate=plan["desperate"],
            )
            q.put({
                "lane": lane,
                "status": status,
                "headers": resp_headers,
                "body": body,
                "is_stream": is_stream,
                "served_provider": served_provider,
                "error": error,
                "finished": finished,
                "complete": complete,
            })

        for lane_spec in lanes:
            threading.Thread(
                target=run_lane, args=lane_spec, daemon=True
            ).start()

        # Each lane thread always pushes exactly one record and upstream
        # reads are bounded by cfg.upstream_timeout, so q.get() cannot hang.
        r1 = q.get()
        if r1["complete"] or len(lanes) == 1:
            winner = r1
        else:
            r2 = q.get()
            ordered = sorted((r1, r2), key=lambda r: r["lane"])
            winner = ordered[pick_winner(ordered)]

        if winner["status"] is None:
            self.send_error(502, "sidecar upstream error")
            self.close_connection = True
            return

        self.send_response(winner["status"])
        for name, value in self._filter_response_headers(
            winner["headers"]
        ):
            self.send_header(name, value)
        self.send_header("x-sidecar-session", session_key[:12])
        pin_header = plan["lane_a"][0]
        if plan["lane_b"]:
            pin_header += "," + plan["lane_b"][0]
        self.send_header("x-sidecar-pin", pin_header)
        self.send_header("x-sidecar-fast-winner", winner["lane"])
        # Buffered body: whole stream arrives at once (TTFT is sacrificed
        # by design on /fast); SSE keeps its text/event-stream type.
        self.send_header("Content-Length", str(len(winner["body"])))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(winner["body"])
        self.close_connection = True

    def _open_upstream(self):
        cfg = self._cfg
        return http.client.HTTPConnection(
            cfg.upstream_host, cfg.upstream_port, timeout=cfg.upstream_timeout
        )

    def _open_upstream_meta(self, path, command, body, headers):
        """Open the upstream conn, send the request, return
        ``(conn, resp, status, resp_headers, is_stream)``.

        Shared by the single path (``_forward_and_relay``) and each fast lane
        (``_proxy_fast.run_lane``). The caller owns the response read loop --
        the single path relays incrementally, the fast path buffers fully --
        so nothing about the read is shared here.
        """
        conn = self._open_upstream()
        conn.request(command, path, body=body, headers=headers)
        resp = conn.getresponse()
        ctype = resp.getheader("Content-Type", "")
        return conn, resp, resp.status, list(resp.getheaders()), is_sse_content_type(ctype)

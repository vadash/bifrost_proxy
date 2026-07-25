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

import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import HOP_BY_HOP, SidecarConfig
from .io_jsonl import JsonlWriter, parse_request_body
from .pooled import apply_feedback, write_logs
from .routing_info import extract_provider
from .sanitize import model_needs_sanitize, sanitize_request_body
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

    # --- Core forwarding logic -----------------------------------------------
    def proxy(self):
        conn = None
        response_line_sent = False
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

            cfg = self._cfg
            state = self._state
            forward_body = body  # default: verbatim passthrough

            # 2a. Claude-family fixes (pooled AND passthrough):
            #   - strip empty thinking blocks ("thinking: Field required" 400)
            #   - rewrite OpenAI reasoning_effort -> Bedrock thinking.adaptive
            #     + output_config.effort ("thinking.enabled is not supported" 400)
            #   - mirror max_completion_tokens -> max_tokens so Bedrock honors
            #     the cap instead of defaulting to 8192
            # Re-serialize only when something actually changed, so clean
            # passthrough stays byte-verbatim.
            if isinstance(request_body_parsed, dict) and model_needs_sanitize(
                request_body_parsed.get("model")
            ):
                new = sanitize_request_body(request_body_parsed)
                if new is not None:
                    forward_body = new

            # --- 2b. Optional pooled routing: rewrite model+fallbacks,
            # pick pin, set state. ---
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
                served_provider = extract_provider(resp_body, is_stream=False)

            if is_stream:
                served_provider = extract_provider(bytes(sse_buf), is_stream=True)

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
                write_logs(
                    self._state,
                    self._cfg,
                    self._capture,
                    self._log,
                    self.headers,
                    self.command,
                    self.path,
                    session_key=session_key,
                    session_source=session_source,
                    pin=pin,
                    keep_list=keep_list,
                    served_provider=served_provider,
                    repin=repin,
                    response_status=response_status,
                    is_stream=is_stream,
                    request_body_parsed=request_body_parsed,
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

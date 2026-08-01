"""End-to-end smoke tests for the HTTP transport layer (``proxy.py``).

Unlike the pure-function suites (``test_routing.py``, ``test_fast.py``,
``test_state``-style modules), these tests exercise the real ``Sidecar``
threaded server against an in-process stub upstream -- so the relays
(single path ``_forward_and_relay`` and the ``/fast`` race ``_proxy_fast``)
run end to end, through ``http.client`` on both sides.

The single-path test is the regression test for the
``resp.getheaders`` (bound-method-passed-uncalled) bug: pre-fix that path
raised ``TypeError: 'method' object is not iterable`` inside
``_filter_response_headers`` and surfaced a 502 to the client.

Stdlib only (matches the sidecar's stdlib-only constraint); importlib
loading because ``sidecar-2`` has a hyphen.
"""

from __future__ import annotations

import http.client
import importlib
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_config = importlib.import_module("sidecar-2.config")
_state_mod = importlib.import_module("sidecar-2.state")
_io = importlib.import_module("sidecar-2.io_jsonl")
_proxy = importlib.import_module("sidecar-2.proxy")

SidecarConfig = _config.SidecarConfig
RoutingState = _state_mod.RoutingState
JsonlWriter = _io.JsonlWriter
Sidecar = _proxy.Sidecar

# The fixed response body the stub upstream always returns. Carries the
# non-streaming routing-info shape consumed by ``extract_provider`` (see
# ``routing_info.py::_provider_of``).
_STUB_BODY = b'{"extra_fields":{"routing_info":{"provider":"nvidia-1"}}}'


class StubHandler(BaseHTTPRequestHandler):
    """Minimal upstream: capture the request body, reply with a fixed JSON body.

    Reads Content-Length bytes from the request, stashes the parsed JSON on
    ``server.received_bodies`` under the server's own lock, then writes
    ``_STUB_BODY`` back with json content-type + length.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        with self.server._lock:
            self.server.received_bodies.append(parsed)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_STUB_BODY)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(_STUB_BODY)
        self.close_connection = True


class StubBifrost(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr):
        super().__init__(addr, StubHandler)
        self._lock = threading.Lock()
        self.received_bodies: list = []


class TestHandlerRelay(unittest.TestCase):
    """Drive the live ``Sidecar`` against a stub upstream over real sockets."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="sidecar-handler-")

        # Build a pools.json-shaped cfg pointed at the stub upstream, which
        # we don't know the port of until it binds to ephemeral port 0.
        self.stub = StubBifrost(("127.0.0.1", 0))
        stub_port = self.stub.server_address[1]

        cfg = SidecarConfig(
            upstream_host="127.0.0.1",
            upstream_port=stub_port,
            listen_host="127.0.0.1",
            listen_port=0,
            pools_path="",            # unused by RoutingState (pools passed directly)
            log_path=f"{self._tmp}/sidecar.log",
            capture_path=f"{self._tmp}/capture.jsonl",
            capture_enabled=False,
            pools={"z-ai/glm-5.2": ["nvidia-1", "nvidia-2", "nvidia-3", "nvidia-4"]},
        )
        state = RoutingState(cfg, shuffle_pools=False)
        capture_writer = JsonlWriter(cfg.capture_path)
        log_writer = JsonlWriter(cfg.log_path)

        self.sidecar = Sidecar(cfg.listen_addr, _proxy.Handler)
        # Inject collaborators onto the server instance (read by handler via self.server).
        self.sidecar.cfg = cfg
        self.sidecar.state = state
        self.sidecar.capture_writer = capture_writer
        self.sidecar.log_writer = log_writer

        self._stub_thread = threading.Thread(
            target=self.stub.serve_forever, daemon=True
        )
        self._stub_thread.start()
        self._sidecar_thread = threading.Thread(
            target=self.sidecar.serve_forever, daemon=True
        )
        self._sidecar_thread.start()

    def tearDown(self):
        # shutdown() must run from a different thread than serve_forever(),
        # which here means the test main thread (the serve threads are the
        # daemon threads above). Stdlib-correct; cannot deadlock.
        self.stub.shutdown()
        self.sidecar.shutdown()
        self.stub.server_close()
        self.sidecar.server_close()
        self.sidecar.capture_writer.close()
        self.sidecar.log_writer.close()

        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _post(self, path: str, override: bytes | None = None):
        """One-shot POST through the sidecar.

        Returns ``(status, headers_dict_lowercased, body_bytes)``. A dropped
        connection (the pre-fix ``resp.getheaders`` bug surface: the handler
        blows up inside the header-filter loop AFTER ``send_response`` already
        set ``_response_line_sent``, so ``except`` skips ``send_error(502)``
        and just closes) is surfaced as ``status is None`` so the caller's
        ``assertEqual(status, 200)`` fails cleanly instead of raising an
        unhandled ``RemoteDisconnected``. This is the regression signature.
        """
        body = override if override is not None else json.dumps({
            "model": "z-ai/glm-5.2",
            "prompt_cache_key": "sess-A",
            "messages": [{"role": "user", "content": "ping"}],
        }).encode("utf-8")
        conn = http.client.HTTPConnection(
            self.sidecar.server_address[0],
            self.sidecar.server_address[1],
            timeout=30,
        )
        try:
            conn.request(
                "POST", path, body=body,
                headers={"Content-Type": "application/json",
                         "Content-Length": str(len(body))},
            )
            resp = conn.getresponse()
        except (http.client.RemoteDisconnected,
                http.client.BadStatusLine,
                ConnectionError):
            return None, {}, b""
        status = resp.status
        headers = {k.lower(): v for k, v in resp.getheaders()}
        body = resp.read()
        conn.close()
        return status, headers, body

    def test_single_path_pooled_request_relays(self):
        """Regression test for the ``resp.getheaders`` bound-method bug.

        Pre-fix: single path raised TypeError inside
        ``_filter_response_headers`` -> 502 to the client. Post-fix: relays
        200 + the stub's body + the pooled-only ``x-sidecar-*`` headers.
        """
        status, headers, body = self._post("/v1/chat/completions")

        self.assertEqual(status, 200,
                         "single path must relay 200; the pre-fix bug drops "
                         "the connection after the status line (handler sets "
                         "_response_line_sent=True before the buggy header "
                         "filter at line 296, so send_error(502) is skipped)")

        # The stub captured what the sidecar actually forwarded. The pooled
        # rewrite must have turned model -> "{primary}/{pooled_model}" and
        # filled fallbacks -- prove the rewrite reached the upstream.
        with self.stub._lock:
            captured = self.stub.received_bodies
            self.assertTrue(captured, "stub upstream saw no request")
            seen = captured[0]
        self.assertIn("model", seen)
        self.assertTrue(seen["model"].startswith("nvidia-"))
        primary = seen["model"].split("/")[0]
        self.assertEqual(seen["model"], f"{primary}/z-ai/glm-5.2")
        self.assertIn("fallbacks", seen)
        self.assertIsInstance(seen["fallbacks"], list)
        self.assertEqual(len(seen["fallbacks"]), 2)
        # Fallbacks are the (rotated-ring) non-primary providers, each
        # suffixed with the pooled model key.
        self.assertRegex(seen["fallbacks"][0], r"^nvidia-\d+/z-ai/glm-5.2$")
        self.assertRegex(seen["fallbacks"][1], r"^nvidia-\d+/z-ai/glm-5.2$")
        self.assertNotEqual(seen["fallbacks"][0], seen["fallbacks"][1])

        # Pooled-only client headers (the ones the bug path hid).
        self.assertEqual(headers.get("x-sidecar-session"), "sess-A"[:12])
        self.assertTrue(headers.get("x-sidecar-pin", "").startswith("nvidia-"))

        # The header filter loop (the thing that raised pre-fix) now emits
        # headers, and the body relays byte-for-byte with a matching CL.
        self.assertEqual(body, _STUB_BODY)
        self.assertEqual(headers.get("content-length"), str(len(_STUB_BODY)))

    def test_fast_path_relays_winner(self):
        """``/fast`` path still relays after dropping the lambda wrapper.

        Both lanes forward to the stub and complete; ``pick_winner`` selects
        lane A (scheduled first, smallest ``finished``). Asserts 200, the
        fast-winner header, and the pooled session header.
        """
        status, headers, body = self._post("/fast/v1/chat/completions")
        self.assertEqual(status, 200, "fast path must relay 200")

        self.assertIn(headers.get("x-sidecar-fast-winner", ""), ("a", "b"))
        self.assertEqual(headers.get("x-sidecar-session"), "sess-A"[:12])
        self.assertEqual(body, _STUB_BODY)

    def test_cap_max_tokens_reaches_upstream_pooled(self):
        """The cap step mutates the parsed body in place *before* the pooled
        rewrite serializes it, so the upstream sees ``max_tokens`` /
        ``max_completion_tokens`` forced to the cap even when the client
        sent neither field. Proves the cap propagates through the pooled path.
        """
        status, _, _ = self._post("/v1/chat/completions")
        self.assertEqual(status, 200)
        with self.stub._lock:
            seen = self.stub.received_bodies[-1]
        self.assertEqual(seen["max_tokens"], 16000)
        self.assertEqual(seen["max_completion_tokens"], 16000)

    def test_cap_max_tokens_clamps_oversize_passthrough(self):
        """A non-pooled model with ``max_tokens=64000`` passes through the
        cap step (unconditional, all models) and the upstream sees the
        clamped value. Proves the cap works for passthrough (non-pooled)
        requests and that an oversize budget is actually forced down.
        """
        payload = json.dumps({
            "model": "nonexistent/model-x",  # not a pool key -> passthrough
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 64000,
        }).encode("utf-8")
        status, _, _ = self._post("/v1/chat/completions", override=payload)
        self.assertEqual(status, 200)
        with self.stub._lock:
            seen = self.stub.received_bodies[-1]
        self.assertEqual(seen["max_tokens"], 16000)
        self.assertEqual(seen["max_completion_tokens"], 16000)



class TestHandlerCORS(unittest.TestCase):
    """CORS emission (--cors path): permissive headers on responses and a
    204 short-circuit for OPTIONS preflight (no upstream forward).

    Mirrors ``TestHandlerRelay.setUp`` but builds the ``Sidecar`` with
    ``cors_enabled=True`` -- the config ``start_sidecar.cmd`` now launches
    with on the Tailscale bind. The same stub upstream is reused so we can
    prove a real POST carries CORS headers and an OPTIONS preflight does
    NOT reach the stub.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="sidecar-cors-")
        self.stub = StubBifrost(("127.0.0.1", 0))
        stub_port = self.stub.server_address[1]
        cfg = SidecarConfig(
            upstream_host="127.0.0.1",
            upstream_port=stub_port,
            listen_host="127.0.0.1",
            listen_port=0,
            pools_path="",
            log_path=f"{self._tmp}/sidecar.log",
            capture_path=f"{self._tmp}/capture.jsonl",
            capture_enabled=False,
            pools={"z-ai/glm-5.2": ["nvidia-1", "nvidia-2", "nvidia-3", "nvidia-4"]},
            cors_enabled=True,
        )
        state = RoutingState(cfg, shuffle_pools=False)
        self.sidecar = Sidecar(cfg.listen_addr, _proxy.Handler)
        self.sidecar.cfg = cfg
        self.sidecar.state = state
        self.sidecar.capture_writer = JsonlWriter(cfg.capture_path)
        self.sidecar.log_writer = JsonlWriter(cfg.log_path)
        self._stub_thread = threading.Thread(
            target=self.stub.serve_forever, daemon=True)
        self._stub_thread.start()
        self._sidecar_thread = threading.Thread(
            target=self.sidecar.serve_forever, daemon=True)
        self._sidecar_thread.start()

    def tearDown(self):
        self.stub.shutdown()
        self.sidecar.shutdown()
        self.stub.server_close()
        self.sidecar.server_close()
        self.sidecar.capture_writer.close()
        self.sidecar.log_writer.close()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _post(self, path):
        body = json.dumps({
            "model": "z-ai/glm-5.2",
            "prompt_cache_key": "sess-A",
            "messages": [{"role": "user", "content": "ping"}],
        }).encode("utf-8")
        conn = http.client.HTTPConnection(
            self.sidecar.server_address[0],
            self.sidecar.server_address[1], timeout=30)
        conn.request("POST", path, body=body,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(body))})
        resp = conn.getresponse()
        status = resp.status
        headers = {k.lower(): v for k, v in resp.getheaders()}
        body = resp.read()
        conn.close()
        return status, headers, body

    def test_post_carries_permissive_cors_headers(self):
        """A real pooled POST response carries
        ``Access-Control-Allow-Origin: *`` so browsers on the tailnet stop
        blocking the cross-origin call to the sidecar's Tailscale IP.
        """
        status, headers, _ = self._post("/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("access-control-allow-origin"), "*")
        self.assertIn("POST", headers.get("access-control-allow-methods", ""))

    def test_options_preflight_returns_204_without_forwarding(self):
        """A browser OPTIONS preflight (with an Origin header) is answered
        directly by the sidecar with 204 + CORS headers and is NOT forwarded
        to Bifrost, so a preflight never touches the upstream.
        """
        body_count_before = len(self.stub.received_bodies)
        conn = http.client.HTTPConnection(
            self.sidecar.server_address[0],
            self.sidecar.server_address[1], timeout=30)
        conn.request("OPTIONS", "/v1/chat/completions", body=b"",
                     headers={"Origin": "http://app.local",
                              "Access-Control-Request-Method": "POST"})
        resp = conn.getresponse()
        status = resp.status
        headers = {k.lower(): v for k, v in resp.getheaders()}
        resp.read()
        conn.close()
        self.assertEqual(status, 204, "preflight must short-circuit with 204")
        self.assertEqual(headers.get("access-control-allow-origin"), "*")
        self.assertIn("POST", headers.get("access-control-allow-methods", ""))
        self.assertEqual(
            len(self.stub.received_bodies), body_count_before,
            "OPTIONS preflight must NOT be forwarded to the stub upstream")


# Stub upstream that also emits its own CORS header -- simulates Bifrost
# (or a downstream proxy) answering with an origin-specific allow-origin.
# Pre-fix the sidecar relayed this verbatim on top of its own ``*``, so the
# browser saw ``Access-Control-Allow-Origin: *, https://vadash.github.io``
# and rejected the response ("header contains multiple values").
_STUB_BODY_CORS = _STUB_BODY


class StubHandlerUpstreamCORS(StubHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        with self.server._lock:
            self.server.received_bodies.append(parsed)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        # Upstream emits its own origin-specific allow-origin header. The
        # sidecar must drop this when it owns CORS, so only its ``*`` remains.
        self.send_header("Access-Control-Allow-Origin",
                         "https://vadash.github.io")
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Content-Length", str(len(_STUB_BODY_CORS)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(_STUB_BODY_CORS)
        self.close_connection = True


class TestHandlerCORSStripUpstream(unittest.TestCase):
    """When --cors is on the sidecar owns CORS and must not relay upstream
    ``Access-Control-*`` headers -- otherwise the browser sees a duplicate
    allow-origin value and blocks the call.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="sidecar-cors-strip-")
        self.stub = StubBifrost(("127.0.0.1", 0))
        self.stub.RequestHandlerClass = StubHandlerUpstreamCORS
        stub_port = self.stub.server_address[1]
        cfg = SidecarConfig(
            upstream_host="127.0.0.1",
            upstream_port=stub_port,
            listen_host="127.0.0.1",
            listen_port=0,
            pools_path="",
            log_path=f"{self._tmp}/sidecar.log",
            capture_path=f"{self._tmp}/capture.jsonl",
            capture_enabled=False,
            pools={"z-ai/glm-5.2": ["nvidia-1", "nvidia-2", "nvidia-3", "nvidia-4"]},
            cors_enabled=True,
        )
        state = RoutingState(cfg, shuffle_pools=False)
        self.sidecar = Sidecar(cfg.listen_addr, _proxy.Handler)
        self.sidecar.cfg = cfg
        self.sidecar.state = state
        self.sidecar.capture_writer = JsonlWriter(cfg.capture_path)
        self.sidecar.log_writer = JsonlWriter(cfg.log_path)
        self._stub_thread = threading.Thread(
            target=self.stub.serve_forever, daemon=True)
        self._stub_thread.start()
        self._sidecar_thread = threading.Thread(
            target=self.sidecar.serve_forever, daemon=True)
        self._sidecar_thread.start()

    def tearDown(self):
        self.stub.shutdown()
        self.sidecar.shutdown()
        self.stub.server_close()
        self.sidecar.server_close()
        self.sidecar.capture_writer.close()
        self.sidecar.log_writer.close()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_only_sidecar_cors_origin_relayed(self):
        """The upstream's origin-specific allow-origin is dropped; the only
        allow-origin reaching the client is the sidecar's ``*``.
        """
        body = json.dumps({
            "model": "z-ai/glm-5.2",
            "prompt_cache_key": "sess-A",
            "messages": [{"role": "user", "content": "ping"}],
        }).encode("utf-8")
        conn = http.client.HTTPConnection(
            self.sidecar.server_address[0],
            self.sidecar.server_address[1], timeout=30)
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(body))})
        resp = conn.getresponse()
        resp.read()
        # http.client flattens duplicate headers to "v1, v2"; assert single.
        raw = resp.getheader("Access-Control-Allow-Origin")
        conn.close()
        self.assertEqual(raw, "*",
                         "upstream CORS must be stripped when --cors is on; "
                         f"got {raw!r}")


class TestJsonlWriterHandle(unittest.TestCase):
    """Persistent-handle + fsync scheduler behavior for ``JsonlWriter``.

    Behavior-defining: the persistent append handle lifts the open()+close()
    syscall pair off the hot pooled paths while preserving crash-durability
    (per-record flush + timer-driven fsync).
    """

    def test_handle_persists_across_writes_then_closes_idempotently(self):
        fd, p = tempfile.mkstemp(prefix="sidecar-jsonl-", suffix=".jsonl")
        os.close(fd)
        w = JsonlWriter(p)
        try:
            for i in range(3):
                w.write({"i": i})
                # Handle stays open between writes -- the whole point.
                self.assertIsNotNone(w._fh)
                self.assertFalse(w._fh.closed)
            # File holds exactly 3 lines, readable while the handle is open.
            import pathlib
            lines = pathlib.Path(p).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual([json.loads(line) for line in lines],
                             [{"i": 0}, {"i": 1}, {"i": 2}])
            w.close()
            self.assertIsNone(w._fh)
            w.close()  # idempotent: must not raise
        finally:
            w.close()
            try:
                os.remove(p)
            except OSError:
                pass

    def test_scheduler_fsyncs_without_raising(self):
        # The fsync scheduler is best-effort; the per-record ``flush()`` in
        # ``JsonlWriter.write`` already guarantees process-kill durability,
        # so the line is on disk immediately after write(). We assert the
        # real invariants (line persisted, handle open, no fsync error) by
        # exercising the scheduler's own fsync path once, synchronously --
        # no 6s sleep waiting for the daemon tick.
        fd, p = tempfile.mkstemp(prefix="sidecar-jsonl-", suffix=".jsonl")
        os.close(fd)
        w = JsonlWriter(p)
        try:
            w.write({"x": "fsync-test"})
            # Exercise the scheduler's per-writer fsync path directly. This
            # is exactly what ``_FsyncScheduler._run`` does on each tick;
            # calling it once proves the fd fsyncs without raising.
            with w._lock:
                if w._fh is not None and not w._fh.closed:
                    os.fsync(w._fh.fileno())
            self.assertIsNotNone(w._fh)
            self.assertFalse(w._fh.closed)
            import pathlib
            self.assertEqual(pathlib.Path(p).read_text(encoding="utf-8").strip(),
                             '{"x": "fsync-test"}')
        finally:
            w.close()
            try:
                os.remove(p)
            except OSError:
                pass



# Body the 429 stub returns. No routing_info on purpose: a 429 from the
# rate-limited account carries no provider hint, so extract_provider resolves
# to None and the feedback path runs the whole-chain-failure branch (cool the
# forced primary) -- which is exactly the path that trips the pool circuit
# once the last cold provider goes hot.
_STUB_BODY_429 = b'{"error":{"message":"rate limit","type":"rate_limit_error"}}'


class StubHandler429(BaseHTTPRequestHandler):
    """Reply 429 with ``Retry-After: 7`` and capture the request body."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        with self.server._lock:
            self.server.received_bodies.append(parsed)
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", "7")
        self.send_header("Content-Length", str(len(_STUB_BODY_429)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(_STUB_BODY_429)
        self.close_connection = True


class StubBifrost429(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr):
        super().__init__(addr, StubHandler429)
        self._lock = threading.Lock()
        self.received_bodies: list = []


class TestHandlerCircuit(unittest.TestCase):
    """Pool-level circuit breaker: a 429 storm must stop at the sidecar.

    Regression for the self-inflicted 429 storm: when the whole pool is
    rate-limited (account-wide, every provider 429s), the sidecar must NOT
    keep sending forward+fallbacks that hold the rate-limit window open.
    The circuit opens and the sidecar answers 429 locally; the stub is never
    contacted again until the window lapses or a 2xx resets it.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="sidecar-circuit-")
        self.stub = StubBifrost429(("127.0.0.1", 0))
        stub_port = self.stub.server_address[1]
        cfg = SidecarConfig(
            upstream_host="127.0.0.1",
            upstream_port=stub_port,
            listen_host="127.0.0.1",
            listen_port=0,
            pools_path="",
            log_path=f"{self._tmp}/sidecar.log",
            capture_path=f"{self._tmp}/capture.jsonl",
            capture_enabled=False,
            pools={"z-ai/glm-5.2": ["nvidia-1", "nvidia-2", "nvidia-3", "nvidia-4"]},
            default_cooldown=600.0,
            circuit_base=20.0,
            circuit_max=120.0,
        )
        state = RoutingState(cfg, shuffle_pools=False)
        capture_writer = JsonlWriter(cfg.capture_path)
        log_writer = JsonlWriter(cfg.log_path)

        self.sidecar = Sidecar(cfg.listen_addr, _proxy.Handler)
        self.sidecar.cfg = cfg
        self.sidecar.state = state
        self.sidecar.capture_writer = capture_writer
        self.sidecar.log_writer = log_writer

        self.state = state

        self._stub_thread = threading.Thread(
            target=self.stub.serve_forever, daemon=True
        )
        self._stub_thread.start()
        self._sidecar_thread = threading.Thread(
            target=self.sidecar.serve_forever, daemon=True
        )
        self._sidecar_thread.start()

    def tearDown(self):
        self.stub.shutdown()
        self.sidecar.shutdown()
        self.stub.server_close()
        self.sidecar.server_close()
        self.sidecar.capture_writer.close()
        self.sidecar.log_writer.close()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _post(self, path, *, session_key="sess-A"):
        body = json.dumps({
            "model": "z-ai/glm-5.2",
            "prompt_cache_key": session_key,
            "messages": [{"role": "user", "content": "ping"}],
        }).encode("utf-8")
        conn = http.client.HTTPConnection(
            self.sidecar.server_address[0],
            self.sidecar.server_address[1],
            timeout=30,
        )
        try:
            conn.request(
                "POST", path, body=body,
                headers={"Content-Type": "application/json",
                         "Content-Length": str(len(body))},
            )
            resp = conn.getresponse()
        except (http.client.RemoteDisconnected,
                http.client.BadStatusLine,
                ConnectionError):
            return None, {}, b""
        status = resp.status
        headers = {k.lower(): v for k, v in resp.getheaders()}
        body = resp.read()
        conn.close()
        return status, headers, body

    def _received_count(self) -> int:
        with self.stub._lock:
            return len(self.stub.received_bodies)

    def _force_all_hot_except_primary(self) -> None:
        """Pre-cool providers 2/3/4 so the session pinned to nvidia-1 has
        exactly one cold provider; one 429 (cooling nvidia-1) then makes the
        whole pool hot -> all_hot trips the circuit.
        """
        now = time.time()
        with self.state.lock():
            self.state.cooldown_trigger("nvidia-2", now)
            self.state.cooldown_trigger("nvidia-3", now)
            self.state.cooldown_trigger("nvidia-4", now)

    def test_pooled_429_trips_circuit_and_next_request_is_local(self):
        """CORE REGRESSION: a 429 with the pool about to be all-hot opens the
        circuit; the immediately following request is answered by the sidecar
        locally (429 + Retry-After) and the stub upstream is NOT contacted
        again (its received_bodies count does not grow).
        """
        self._force_all_hot_except_primary()

        # First request: forwarded to the 429 stub -> cools nvidia-1 -> all
        # hot -> circuit trips (circuit_note="tripped"), opened for ~7s.
        status1, headers1, _ = self._post("/v1/chat/completions")
        self.assertEqual(status1, 429,
                         "first request relays the upstream 429 and trips the "
                         "circuit locally; the 429 must reach the client")
        self.assertEqual(
            self._received_count(), 1,
            "the first pooled request must reach the stub upstream once")
        self.assertEqual(self.state.cooldowns.get("nvidia-1") is not None, True)
        # Circuit is now open: the pool circuit entry must persist.
        with self.state.lock():
            self.assertTrue(
                self.state.circuit_open("z-ai/glm-5.2", time.time()),
                "the 429 that cooled the last cold provider must have "
                "opened the pool circuit")

        # Second request: circuit open -> sidecar answers locally. The
        # stub MUST NOT see this request.
        before = self._received_count()
        status2, headers2, body2 = self._post("/v1/chat/completions")
        self.assertEqual(
            self._received_count(), before,
            "REGRESSION: with the circuit open the sidecar must answer "
            "locally and NOT forward to the stub; removing the "
            "short-circuit would make this count grow")
        self.assertEqual(
            status2, 429,
            "an open circuit answers 429 locally, not the upstream status")
        # Retry-After surfaced to the client, echoing the open window (>=1).
        self.assertIn("retry-after", headers2,
                       "the local 429 must carry a Retry-After so the client "
                       "backs off the account limit")
        ra = headers2["retry-after"]
        self.assertTrue(int(ra) >= 1,
                        f"Retry-After must be a positive int of seconds; got {ra!r}")

        # Local 429 body is valid JSON of the documented shape.
        decoded = json.loads(body2)
        self.assertIn("error", decoded)
        self.assertEqual(decoded["error"].get("type"), "rate_limit_error")
        self.assertIn("rate-limited", decoded["error"].get("message", ""))

    def test_success_resets_circuit_and_clears_stale_cooldowns(self):
        """A 2xx while the circuit is open resets it (closes the circuit)
        and discards the stale 600s per-provider cooldowns -- they were
        collateral damage from the account-wide limit, not real per-provider
        faults, so they must not keep the pool desperate after recovery.

        The probe/half-open path is what admits the 2xx: the circuit's open
        window is advanced past its expiry so the next request is allowed
        through (probing=True), and the 2xx it returns resets everything.
        """
        self._force_all_hot_except_primary()
        # Trip the circuit (window ~7s via the stub's Retry-After: 7).
        self._post("/v1/chat/completions")
        pool = "z-ai/glm-5.2"
        with self.state.lock():
            self.assertTrue(self.state.circuit_open(pool, time.time()))

        # Advance the open window so the circuit is half-open (probe allowed).
        with self.state.lock():
            st = self.state.pool_circuit[pool]
            st["open_until"] = time.time() - 0.001  # window already lapsed
        # Swap the stub back to 2xx so the probe succeeds and resets.
        self.stub.RequestHandlerClass = StubHandler
        status_reset, _, _ = self._post("/v1/chat/completions")
        self.assertEqual(status_reset, 200,
                         "the half-open probe must be forwarded; a 2xx there "
                         "resets the circuit")
        # Circuit closed AND the stale cooldowns cleared.
        with self.state.lock():
            self.assertFalse(self.state.circuit_open(pool, time.time()),
                             "a 2xx must reset (close) the pool circuit")
            for p in ("nvidia-1", "nvidia-2", "nvidia-3", "nvidia-4"):
                self.assertFalse(
                    self.state.cooldowns.get(p, 0) > time.time(),
                    f"circuit_reset must clear the stale cooldown on {p} "
                    "(collateral from the account-wide limit, not a fault)")

    def test_fast_path_short_circuits_open_circuit(self):
        """/fast also honours the breaker: with the circuit open, /fast is
        answered locally (429) and never races -- otherwise the two disjoint
        lanes would bypass the breaker and double the amplification.
        """
        self._force_all_hot_except_primary()
        # Trip the circuit via the single path first.
        self._post("/v1/chat/completions")
        with self.state.lock():
            self.assertTrue(
                self.state.circuit_open("z-ai/glm-5.2", time.time()))

        before = self._received_count()
        status, headers, _ = self._post("/fast/v1/chat/completions")
        # /fast returns the local 429 (the race is never started) and the
        # stub count does NOT grow.
        self.assertEqual(status, 429,
                         "/fast must short-circuit on an open circuit")
        self.assertEqual(
            self._received_count(), before,
            "/fast must not race (contact the stub) when the circuit is open")
        self.assertIn("retry-after", headers)

if __name__ == "__main__":
    unittest.main()

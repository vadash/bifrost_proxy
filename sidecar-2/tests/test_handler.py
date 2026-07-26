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
import tempfile
import threading
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

        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _post(self, path: str):
        """One-shot POST through the sidecar.

        Returns ``(status, headers_dict_lowercased, body_bytes)``. A dropped
        connection (the pre-fix ``resp.getheaders`` bug surface: the handler
        blows up inside the header-filter loop AFTER ``send_response`` already
        set ``_response_line_sent``, so ``except`` skips ``send_error(502)``
        and just closes) is surfaced as ``status is None`` so the caller's
        ``assertEqual(status, 200)`` fails cleanly instead of raising an
        unhandled ``RemoteDisconnected``. This is the regression signature.
        """
        body = json.dumps({
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


if __name__ == "__main__":
    unittest.main()

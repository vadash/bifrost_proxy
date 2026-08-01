"""Smoke test: replay the real 00:29 storm against a stub upstream.

Not a unit test -- a runnable end-to-end demonstration that the pool circuit
breaker actually stops the storm observed in sidecar.log. Run directly:

    python -m sidecar-2.tests.smoke_storm

It stands up a stub "Bifrost" that answers 429 to every pooled request (the
account-wide rate limit NVIDIA applied), points a real ``Sidecar`` at it on an
ephemeral port, and fires 40 sequential pooled requests from one session --
the same shape as session ``h:16a90643e2``, which produced 136 straight 429s
and ~400 refused upstream calls over ten minutes.

The number that matters is UPSTREAM CALLS. Pre-fix, every client request cost
3 upstream calls (primary + 2 fallbacks) forever. Post-fix, the pool trips
once all 15 providers are hot and the sidecar answers 429 locally, so upstream
call growth stops dead while clients still get a well-formed 429 + Retry-After.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import threading
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))

_pkg = __import__("sidecar-2.config", fromlist=["*"])
SidecarConfig = _pkg.SidecarConfig
_proxy = __import__("sidecar-2.proxy", fromlist=["*"])
_state = __import__("sidecar-2.state", fromlist=["*"])
_io = __import__("sidecar-2.io_jsonl", fromlist=["*"])

POOL = "z-ai/glm-5.2"
PROVIDERS = [f"nvidia-{i}" for i in range(1, 16)]


class RateLimited(BaseHTTPRequestHandler):
    """Stub Bifrost: every pooled call is refused, exactly like the account
    limit. Counts calls so we can measure amplification."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.server.recovered:
            # Account limit lifted: answer 200 with the routing_info the
            # sidecar reads the serving provider out of.
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            self.server.calls += 1
            self.server.provider_calls += 1
            try:
                primary = json.loads(raw)["model"].split("/")[0]
            except Exception:
                primary = PROVIDERS[0]
            ok = json.dumps({
                "id": "smoke",
                "choices": [],
                "extra_fields": {"routing_info": {"provider": primary}},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(ok)))
            self.end_headers()
            self.wfile.write(ok)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        self.server.calls += 1
        # Bifrost expands the body's `fallbacks` array into one upstream
        # provider call per entry, so the primary + len(fallbacks) is what
        # NVIDIA's account-wide limiter actually counts.
        try:
            self.server.provider_calls += 1 + len(
                json.loads(raw).get("fallbacks") or []
            )
        except Exception:
            self.server.provider_calls += 1
        body = b'{"error":{"message":"rate limit","type":"rate_limit_error"}}'
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Stub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr):
        super().__init__(addr, RateLimited)
        self.calls = 0
        self.provider_calls = 0
        self.recovered = False


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sidecar-smoke-")
    stub = Stub(("127.0.0.1", 0))
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    cfg = SidecarConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=stub.server_address[1],
        pools={POOL: list(PROVIDERS)},
        log_path=os.path.join(tmp, "smoke.log"),
        capture_path=os.path.join(tmp, "smoke.jsonl"),
        # Short window so the half-open probe is reachable in a smoke run;
        # production defaults are 20s/120s.
        circuit_base=2.0,
        circuit_max=4.0,
    )
    sc = _proxy.Sidecar(("127.0.0.1", 0), _proxy.Handler)
    sc.cfg = cfg
    sc.state = _state.RoutingState(cfg, shuffle_pools=False)
    sc.capture_writer = _io.JsonlWriter(cfg.capture_path)
    sc.log_writer = _io.JsonlWriter(cfg.log_path)
    threading.Thread(target=sc.serve_forever, daemon=True).start()
    port = sc.server_address[1]

    local_429 = 0
    upstream_at_trip = None
    retry_after_seen = None
    body_seen = None

    print(f"  {'req':>4} {'upstream calls':>15} {'status':>7}  note")
    for i in range(1, 41):
        payload = json.dumps({
            "model": POOL,
            "prompt_cache_key": "storm-session",
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request("POST", "/v1/chat/completions", body=payload,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read()
        ra = r.getheader("Retry-After")
        c.close()

        note = ""
        if ra is not None:
            # Answered locally by the breaker: upstream was never touched.
            local_429 += 1
            if upstream_at_trip is None:
                upstream_at_trip = stub.calls
                retry_after_seen = ra
                body_seen = raw
                note = "<-- CIRCUIT OPEN, no upstream call"
        if i <= 12 or note or i == 40:
            print(f"  {i:>4} {stub.calls:>15} {r.status:>7}  {note}")

    total = stub.calls
    print()
    print(f"  client requests           : 40")
    print(f"  upstream calls actually made: {total}")
    print(f"  answered locally by breaker : {local_429}")
    print(f"  upstream calls when tripped : {upstream_at_trip}")
    print(f"  Retry-After header          : {retry_after_seen}")
    print(f"  local body                  : {(body_seen or b'').decode()}")
    print()
    print("  Provider-level calls (what NVIDIA's rate limiter actually counts):")
    print(f"    pre-fix  : {40 * 3:>4}   (40 requests x primary + 2 fallbacks, forever)")
    print(f"    post-fix : {stub.provider_calls:>4}   (fallbacks dropped when desperate, then no calls at all)")

    # Phase 2: the account limit lifts. Nothing tells the sidecar directly --
    # it must discover recovery on its own via the half-open probe.
    stub.recovered = True
    before = stub.calls
    time.sleep(2.5)  # let the circuit window lapse

    recovered_at = None
    for i in range(1, 6):
        payload = json.dumps({
            "model": POOL,
            "prompt_cache_key": "storm-session",
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request("POST", "/v1/chat/completions", body=payload,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        c.close()
        if r.status == 200 and recovered_at is None:
            recovered_at = i
        print(f"  recovery req {i}: status {r.status}")

    after_calls = stub.calls - before
    print()
    print(f"  first 200 after recovery    : request {recovered_at}")
    print(f"  upstream calls for 5 requests: {after_calls} (service fully restored)")

    sc.shutdown()
    stub.shutdown()

    ok = (
        local_429 > 0
        and total < 120
        and retry_after_seen is not None
        and json.loads(body_seen)["error"]["type"] == "rate_limit_error"
        # Recovery is self-discovered: the first request after the window
        # lapses is the probe, it succeeds, and normal service resumes -- so
        # all 5 requests reach upstream instead of being short-circuited.
        and recovered_at == 1
        and after_calls == 5
    )
    print()
    print("  RESULT:", "PASS - storm stopped" if ok else "FAIL - still storming")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

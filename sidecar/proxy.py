"""Sidecar v1: passthrough capture proxy for Bifrost (Bifrost-kh0).

Listens on 127.0.0.1:8088, forwards every request VERBATIM to Bifrost on
127.0.0.1:8080 (streaming and non-streaming), and appends one JSON line per
request to sidecar/capture.jsonl (redacted headers, request body, response
status, routing_info). NO routing/rewriting -- observe only.

stdlib only: ThreadingHTTPServer + http.client.
"""

import http.client
import http.server
import json
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LISTEN_HOST, LISTEN_PORT = "127.0.0.1", 8088
UPSTREAM_HOST, UPSTREAM_PORT = "127.0.0.1", 8080

CAPTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture.jsonl")

REDACT_HEADERS = {"authorization", "x-api-key", "apikey", "api-key"}
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

CAPTURE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Server class
# ---------------------------------------------------------------------------


class Sidecar(http.server.ThreadingHTTPServer):
    """One thread per connection so concurrent subagents don't block each other."""

    daemon_threads = True


# ---------------------------------------------------------------------------
# Helpers (module-level)
# ---------------------------------------------------------------------------


def redact_headers(items):
    """Return a dict of headers with sensitive values replaced by 'REDACTED'.

    Comparison is case-insensitive on the header name.
    """
    out = {}
    for name, value in items:
        if name.lower() in REDACT_HEADERS:
            out[name] = "REDACTED"
        else:
            out[name] = value
    return out


def parse_request_body(raw: bytes):
    """Parse a request body to JSON if possible, else a truncated raw string.

    Parsed JSON is kept whole (so model/messages/instructions/previous_response_id
    are all visible); non-JSON bodies are truncated to 4KB.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw.decode("utf-8", "replace")[:4096]


def parse_routing_nonstream(resp_body: bytes):
    """Extract routing_info from a non-streaming Bifrost response.

    Non-stream -> body.extra_fields.routing_info.
    Returns None on any exception or missing field.
    """
    try:
        data = json.loads(resp_body)
        return data.get("extra_fields", {}).get("routing_info")
    except Exception:
        return None


def parse_routing_stream(sse_buf: bytes):
    """Extract routing_info from a streaming SSE Bifrost response.

    The terminal `response.completed`/`response.incomplete` event's JSON
    `data:` payload carries `extra_fields.routing_info`. Walk events in
    REVERSE so the terminal event is found first.
    Returns None on any exception or missing field.
    """
    try:
        text = sse_buf.decode("utf-8", "replace")
        # Split into SSE events (blank-line delimited).
        events = text.split("\n\n")
        for event in reversed(events):
            for line in event.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload:
                    continue
                try:
                    parsed = json.loads(payload)
                except Exception:
                    continue
                ri = parsed.get("extra_fields", {}).get("routing_info")
                if ri is not None:
                    return ri
        return None
    except Exception:
        return None


def write_capture(record: dict):
    """Append one JSON line per request to CAPTURE_PATH under CAPTURE_LOCK."""
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with CAPTURE_LOCK:
        with open(CAPTURE_PATH, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """Forward every request VERBATIM to Bifrost and capture one line per request."""

    protocol_version = "HTTP/1.1"

    # --- HTTP verb dispatch ---
    def do_GET(self):
        self.proxy()

    def do_POST(self):
        self.proxy()

    def do_PUT(self):
        self.proxy()

    def do_DELETE(self):
        self.proxy()

    def do_PATCH(self):
        self.proxy()

    def do_OPTIONS(self):
        self.proxy()

    def do_HEAD(self):
        self.proxy()

    # Silence default stderr logging (we do our own capture).
    def log_message(self, *a):
        pass

    # --- Core forwarding logic ---
    def proxy(self):
        conn = None
        response_line_sent = False
        response_status = None
        is_stream = None
        routing_info = None
        request_body_parsed = None
        has_previous_response_id = False
        error_str = None

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

            # 2. Build forward headers: copy verbatim except hop-by-hop / host / content-length.
            fwd_headers = {}
            for name, value in self.headers.items():
                ln = name.lower()
                if ln in HOP_BY_HOP:
                    continue
                if ln == "host":
                    continue
                if ln == "content-length":
                    continue
                fwd_headers[name] = value  # preserve Authorization verbatim upstream

            # 3. Forward to upstream.
            conn = http.client.HTTPConnection(
                UPSTREAM_HOST, UPSTREAM_PORT, timeout=600
            )
            conn.request(self.command, self.path, body=body, headers=fwd_headers)
            resp = conn.getresponse()

            # 4. Streaming detection.
            ctype = resp.getheader("Content-Type", "")
            is_stream = "text/event-stream" in ctype.lower()

            # 5. Status line.
            self.send_response(resp.status)
            response_line_sent = True
            response_status = resp.status

            # 6. Relay response headers except hop-by-hop and content-length.
            for name, value in resp.getheaders():
                ln = name.lower()
                if ln in HOP_BY_HOP:
                    continue
                if ln == "content-length":
                    continue
                self.send_header(name, value)

            sse_buf = bytearray()
            if is_stream:
                # Stream: Connection: close, relay chunks as they arrive.
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = resp.read(8192)
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
                routing_info = parse_routing_nonstream(resp_body)

            if is_stream:
                routing_info = parse_routing_stream(bytes(sse_buf))

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

            # 8/9. Always attempt capture, even on error.
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
            try:
                write_capture(record)
            except Exception:
                pass

            # Force connection close; never let an exception escape.
            self.close_connection = True


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(
        f"[sidecar] listening on http://{LISTEN_HOST}:{LISTEN_PORT} "
        f"-> http://{UPSTREAM_HOST}:{UPSTREAM_PORT}  capture={CAPTURE_PATH}"
    )
    Sidecar((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()

"""Sidecar v2: session-pinned routing proxy for Bifrost (Bifrost-tfz).

Listens on 127.0.0.1:8088, forwards to Bifrost on 127.0.0.1:8080.

For models declared in sidecar/pools.json (the *pooled* models), the sidecar
performs real routing: it pins each session to a deterministic provider for
prompt-cache locality, owns the fallback order, and cools down failing providers
globally. Pooled requests have their rewritten ``model`` + ``fallbacks`` body
forwarded upstream, get ``x-sidecar-session`` / ``x-sidecar-pin`` response
headers, and are logged to sidecar/sidecar.log (one JSON line per request) and
sidecar/capture.jsonl (raw redacted capture).

For models NOT in pools.json, the sidecar is a transparent passthrough --
indistinguishable from the client hitting Bifrost directly: it forwards the
original bytes VERBATIM, adds NO sidecar headers, touches NO state, and writes
NO log file of any kind (neither sidecar.log nor capture.jsonl).

stdlib only: ThreadingHTTPServer + http.client.
"""

import hashlib
import http.client
import http.server
import json
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DIRNAME = os.path.dirname(os.path.abspath(__file__))

LISTEN_HOST, LISTEN_PORT = "127.0.0.1", 8088
UPSTREAM_HOST, UPSTREAM_PORT = "127.0.0.1", 8080

CAPTURE_PATH = os.path.join(_DIRNAME, "capture.jsonl")
SIDECAR_LOG_PATH = os.path.join(_DIRNAME, "sidecar.log")
POOLS_PATH = os.path.join(_DIRNAME, "pools.json")

# Session pins / response-id map expire after this many seconds of inactivity.
SESSION_TTL = 3600
# Global per-provider cooldown duration in seconds.
DEFAULT_COOLDOWN = 600

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

CAPTURE_LOCK = threading.Lock()  # guards both capture.jsonl and sidecar.log writes
STATE_LOCK = threading.Lock()    # guards all routing state (PINS/RESP_MAP/COOLDOWNS)


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


def write_jsonl(path: str, record: dict):
    """Append one JSON line to `path` under CAPTURE_LOCK.

    Generic form used for both capture.jsonl and sidecar.log.
    """
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with CAPTURE_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()


def write_capture(record: dict):
    """Append one JSON line per request to CAPTURE_PATH (raw redacted capture)."""
    write_jsonl(CAPTURE_PATH, record)


# --- Config loading ---------------------------------------------------------


def load_pools() -> dict:
    """Read+parse pools.json. On missing file or parse error, print a
    `[sidecar] WARNING: pools.json ...` line to stdout and return `{}` (pure
    passthrough, no pooled models).
    """
    try:
        with open(POOLS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("pools.json top-level must be a JSON object")
        return data
    except FileNotFoundError:
        print(f"[sidecar] WARNING: pools.json not found at {POOLS_PATH} -> passthrough only")
        return {}
    except Exception as e:
        print(f"[sidecar] WARNING: pools.json parse error ({e!r}) -> passthrough only")
        return {}


# Singleton pool config. A model is "pooled" iff body["model"] is exactly a key
# here. Loaded once at startup (startup-only; editing pools.json needs restart).
POOLS: dict = {}


# --- In-memory routing state (guarded by STATE_LOCK) -----------------------

# session_key -> {"pin": int, "seen": float}  (pin = index into the pooled
# model's provider list; seen = last activity epoch, refreshed every request)
PINS: dict = {}

# response_id -> {"session": str, "seen": float}  (enables cascade step 2)
RESP_MAP: dict = {}

# provider_name -> expiry_epoch  (hot iff expiry > now)
COOLDOWNS: dict = {}


def purge_expired(now: float):
    """Purge expired PINS/RESP_MAP (inactivity > SESSION_TTL) and expired
    COOLDOWNS (now >= expiry). Must be called under STATE_LOCK.
    """
    expired_sessions = [k for k, v in PINS.items() if now - v["seen"] > SESSION_TTL]
    for k in expired_sessions:
        del PINS[k]
    expired_resps = [k for k, v in RESP_MAP.items() if now - v["seen"] > SESSION_TTL]
    for k in expired_resps:
        del RESP_MAP[k]
    expired_cd = [p for p, exp in COOLDOWNS.items() if now >= exp]
    for p in expired_cd:
        del COOLDOWNS[p]


def cooldown_is_hot(provider: str, now: float) -> bool:
    """True iff provider is currently in cooldown. Call under STATE_LOCK."""
    return COOLDOWNS.get(provider, 0) > now


def cooldown_trigger(provider: str, now: float, secs: float = DEFAULT_COOLDOWN):
    """Put a provider into cooldown. Re-trigger before expiry extends to the
    later of current/new expiry. Call under STATE_LOCK.
    """
    COOLDOWNS[provider] = max(COOLDOWNS.get(provider, 0), now + secs)


# --- Session-identity cascade ----------------------------------------------


def first_user_text(body: dict) -> str:
    """Extract the first user-turn text for the hash fallback.

    Handles both /v1/responses (`input`) and /v1/chat/completions (`messages`).
    Joins `text` fields across content parts when content is a list.
    """
    # /v1/responses: body["input"]
    inp = body.get("input")
    if isinstance(inp, str):
        return inp
    if isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            content = item.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict) and "text" in part:
                        parts.append(str(part["text"]))
                if parts:
                    return "".join(parts)
    # /v1/chat/completions: body["messages"]
    for msg in body.get("messages", []) or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    parts.append(str(part["text"]))
            if parts:
                return "".join(parts)
    return ""


def derive_session_key(body: dict) -> tuple:
    """Return (session_key, source) via the cascade (first match wins):

    1. prompt_cache_key truthy      -> (str(that), "cache_key")
    2. previous_response_id in RESP_MAP -> (stored session, "prev_resp")
    3. hash fallback -> ("h:" + sha256(instructions + "\n" + first_user_text)[:32], "hash")
    """
    cache_key = body.get("prompt_cache_key")
    if cache_key:
        return (str(cache_key), "cache_key")
    prev_id = body.get("previous_response_id")
    if prev_id is not None and prev_id in RESP_MAP:
        return (RESP_MAP[prev_id]["session"], "prev_resp")
    instructions = body.get("instructions") or ""
    text = first_user_text(body)
    digest = hashlib.sha256((instructions + "\n" + text).encode("utf-8")).hexdigest()[:32]
    return ("h:" + digest, "hash")


# --- Pin assignment ---------------------------------------------------------


def assign_pin(session_key: str, providers: list, now: float) -> int:
    """Return the pinned provider index for `session_key`. If known, refresh
    `seen` and return the stored pin; else compute least-loaded start (fewest
    live pinned sessions, tie -> lowest index, skipping hot providers;
    if all hot, fall back to all indices). Store + return the pin. STATE_LOCK
    must be held by the caller.
    """
    if session_key in PINS:
        PINS[session_key]["seen"] = now
        return PINS[session_key]["pin"]
    load = [0] * len(providers)
    for v in PINS.values():
        idx = v["pin"]
        if 0 <= idx < len(providers) and now - v["seen"] <= SESSION_TTL:
            load[idx] += 1
    candidates = [i for i in range(len(providers)) if not cooldown_is_hot(providers[i], now)]
    if not candidates:
        candidates = list(range(len(providers)))  # desperate: all hot, still land somewhere
    pin = min(candidates, key=lambda i: (load[i], i))
    PINS[session_key] = {"pin": pin, "seen": now}
    return pin


# --- Response metadata extraction ------------------------------------------


def parse_response_meta_nonstream(resp_body: bytes):
    """Extract (routing_info, response_id) from a non-streaming Bifrost
    response. routing_info from data.extra_fields.routing_info; response_id from
    data.id. Returns (None, None) on any exception or missing field.
    """
    try:
        data = json.loads(resp_body)
        routing_info = data.get("extra_fields", {}).get("routing_info")
        response_id = data.get("id")
        return (routing_info, response_id)
    except Exception:
        return (None, None)


def parse_response_meta_stream(sse_buf: bytes):
    """Extract (routing_info, response_id) from a streaming SSE Bifrost
    response. Walk events in REVERSE; the terminal `response.completed`/
    `response.incomplete` event's JSON `data:` payload carries
    `extra_fields.routing_info` and the id (`payload.id` or
    `payload.response.id`). Return the first event that yields a non-None
    routing_info, paired with whatever id that same event carried.
    Returns (None, None) on any exception or missing field.
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
                    rid = parsed.get("id")
                    if rid is None:
                        rid = parsed.get("response", {}).get("id")
                    return (ri, rid)
        return (None, None)
    except Exception:
        return (None, None)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """Forward to Bifrost. Pooled models get session-pinned routing; everything
    else is a transparent passthrough (verbatim bytes, no state, no logs)."""

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

            # --- 2b. Optional pooled routing: rewrite model+fallbacks, pick pin, set state. ---
            forward_body = body  # default: verbatim passthrough
            if (
                isinstance(request_body_parsed, dict)
                and request_body_parsed.get("model") in POOLS
            ):
                pooled_model = request_body_parsed["model"]
                providers = POOLS[pooled_model]
                now = time.time()
                with STATE_LOCK:
                    purge_expired(now)
                    session_key, session_source = derive_session_key(request_body_parsed)
                    pin = assign_pin(session_key, providers, now)

                    # Ring: rotate starting at the pinned provider.
                    ring_names = list(providers[pin:]) + list(providers[:pin])
                    kept = [p for p in ring_names if not cooldown_is_hot(p, now)]
                    if not kept:
                        desperate = True
                        kept = list(ring_names)  # full ring, request still goes out
                    keep_list = list(kept)

                    # Rewrite request body dict: model -> "primary/pooled", fallbacks -> rest.
                    request_body_parsed["model"] = f"{kept[0]}/{pooled_model}"
                    request_body_parsed["fallbacks"] = [
                        f"{p}/{pooled_model}" for p in kept[1:]
                    ]
                    new_body = json.dumps(request_body_parsed).encode("utf-8")
                    forward_body = new_body

            # 3. Forward to upstream.
            conn = http.client.HTTPConnection(
                UPSTREAM_HOST, UPSTREAM_PORT, timeout=600
            )
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
            for name, value in resp.getheaders():
                ln = name.lower()
                if ln in HOP_BY_HOP:
                    continue
                if ln == "content-length":
                    continue
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
                routing_info, response_id = parse_response_meta_nonstream(resp_body)

            if is_stream:
                routing_info, response_id = parse_response_meta_stream(bytes(sse_buf))

            # --- 6b. Post-response feedback (pooled requests only). ---
            if pooled_model is not None:
                now_fb = time.time()
                served = None
                primary = None
                is_fallback = None
                if isinstance(routing_info, dict) and routing_info:
                    served = routing_info.get("provider")
                    primary = routing_info.get("primary_provider")
                    is_fallback = routing_info.get("is_fallback")

                with STATE_LOCK:
                    purge_expired(now_fb)
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
                        cooldown_trigger(primary, now_fb)
                        # Re-pin: follow the provider that actually served.
                        if served in providers:
                            PINS[session_key] = {
                                "pin": providers.index(served),
                                "seen": now_fb,
                            }
                    elif err_path:
                        # Cool the primary we forced; advance pin one step.
                        cooldown_trigger(keep_list[0], now_fb)
                        if len(keep_list) > 1 and keep_list[1] in providers:
                            PINS[session_key] = {
                                "pin": providers.index(keep_list[1]),
                                "seen": now_fb,
                            }

                    # Map response id -> session for future prev_response_id lookups.
                    if response_id is not None:
                        RESP_MAP[response_id] = {"session": session_key, "seen": now_fb}

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

            # Logging: pooled only. Non-pooled requests are transparent -- no
            # capture.jsonl, no sidecar.log, no state, no headers. (User override.)
            if pooled_model is not None:
                # 8/9. Raw capture record (pooled requests only).
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

                # Decision log: one sidecar.log line per pooled request.
                try:
                    served = None
                    is_fallback_val = None
                    repin = None
                    if isinstance(routing_info, dict) and routing_info:
                        served = routing_info.get("provider")
                        is_fallback_val = routing_info.get("is_fallback")
                    # Reconstruct repin from pin/served for observability.
                    now_log = time.time()
                    with STATE_LOCK:
                        entry = PINS.get(session_key)
                        if entry is not None:
                            repin_idx = entry["pin"]
                            repin = (
                                providers[repin_idx]
                                if providers is not None and 0 <= repin_idx < len(providers)
                                else None
                            )
                        hot = [
                            p for p in keep_list
                            if cooldown_is_hot(p, now_log)
                        ] if keep_list is not None else []
                    write_jsonl(SIDECAR_LOG_PATH, {
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
                except Exception:
                    pass

            # Force connection close; never let an exception escape.
            self.close_connection = True


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    POOLS = load_pools()
    print(
        f"[sidecar] listening on http://{LISTEN_HOST}:{LISTEN_PORT} "
        f"-> http://{UPSTREAM_HOST}:{UPSTREAM_PORT}  "
        f"pooled_models={len(POOLS)} capture={CAPTURE_PATH} log={SIDECAR_LOG_PATH}"
    )
    if POOLS:
        for model, provs in POOLS.items():
            print(f"[sidecar]   {model}: {len(provs)} providers")
    Sidecar((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()

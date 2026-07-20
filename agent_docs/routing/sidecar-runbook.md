# Sidecar Runbook

How to run, verify, and use the Bifrost capture sidecar (v1, Bifrost-kh0).

## What it is

A Python stdlib (`ThreadingHTTPServer` + `http.client`) passthrough proxy at
`sidecar/proxy.py`. It listens on `127.0.0.1:8088`, forwards every request
verbatim to Bifrost on `127.0.0.1:8080` (streaming and non-streaming), and
appends one JSON line per request to `sidecar/capture.jsonl` with redacted
headers, parsed request body, response status, and `routing_info`. No routing
or rewriting — observe only.

## Prerequisites

- Python 3.14 at `C:\Users\vadash\AppData\Local\Python\pythoncore-3.14-64\python.exe`
  (stdlib only; no pip packages needed).
- Bifrost running on `127.0.0.1:8080`.
- `curl.exe` at `C:\Windows\System32\curl.exe` (for verification).

## Start the sidecar

### From a terminal

```cmd
C:\projects\_llm\_llm\Bifrost\start_sidecar.cmd
```

Or directly:
```cmd
python C:\projects\_llm\_llm\Bifrost\sidecar\proxy.py
```

Expected banner:
```
[sidecar] listening on http://127.0.0.1:8088 -> http://127.0.0.1:8080  capture=...\sidecar\capture.jsonl
```

### Via the agent hub (long-running background process)

```json
{
  "op": "start",
  "name": "sidecar",
  "application": "C:\\Users\\vadash\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe",
  "args": ["C:/projects/_llm/Bifrost/sidecar/proxy.py"],
  "cwd": "C:/projects/_llm/Bifrost",
  "ready": {"log": "listening on", "timeout": 15}
}
```

## Repoint the client

Change the client's base URL from `http://127.0.0.1:8080/v1` to
`http://127.0.0.1:8088/v1`. Bifrost stays on :8080 unchanged.

## Verify (5 checks)

### 1. Sidecar starts clean
Hub `logs name="sidecar"` shows the `listening on http://127.0.0.1:8088`
banner with no traceback.

### 2. Non-stream passthrough + capture
```cmd
curl.exe -s -X POST http://127.0.0.1:8088/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Bearer test-key-should-not-appear" ^
  -d "{\"model\":\"z-ai/glm-5.2\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```
Expect: curl returns a valid completion. Last line of `capture.jsonl`:
`request_headers.Authorization == "REDACTED"`,
`request_body.model == "z-ai/glm-5.2"`,
`streaming == false`, `routing_info` populated.

### 3. Streaming passthrough
Same curl with `"stream":true` added. Expect: curl emits incremental `data:`
SSE events (not one buffered blob); capture line has `streaming == true` and
non-null `routing_info` from the terminal event.

### 4. Redaction never leaks upstream
All test requests returned 200 (not 401) — the real `Authorization` reached
Bifrost. Only the **capture** is redacted.

### 5. Concurrency
Two overlapping `curl.exe` calls both complete and both produce well-formed
(non-interleaved) capture lines. `ThreadingHTTPServer` + `CAPTURE_LOCK`
handle parallel subagents.

## Analyze the capture log

```python
import json
lines = [l for l in open("sidecar/capture.jsonl", encoding="utf-8").read().splitlines() if l]
records = [json.loads(l) for l in lines]
# count, group by method/path, tabulate routing_info.provider, check has_previous_response_id
```

See `session-identity.md` for the analysis that revealed `prompt_cache_key` is
the real session identifier.

## Capture record shape

```json
{
  "ts": "2026-07-20T19:36:28.953730",
  "method": "POST",
  "path": "/v1/responses",
  "request_headers": {"Authorization": "REDACTED", "...": "..."},
  "request_body": {"model": "...", "input": [...], "instructions": "...", "...": "..."},
  "has_previous_response_id": false,
  "response_status": 200,
  "streaming": true,
  "routing_info": {"provider": "nvidia-3", "key": "NVIDIA_API_KEY_13", "is_fallback": true}
}
```

- `request_headers` — redacted (`authorization`, `x-api-key`, `apikey`,
  `api-key` → `"REDACTED"`).
- `request_body` — parsed JSON kept whole (model/messages/instructions visible);
  non-JSON bodies truncated to 4 KB.
- `routing_info` — extracted from response `extra_fields.routing_info`
  (non-stream body / streaming terminal SSE event). `null` on error.

## Files

| File | Purpose |
|---|---|
| `sidecar/proxy.py` | The proxy (single file, stdlib only) |
| `start_sidecar.cmd` | Repo-root launcher (`%~dp0` style) |
| `sidecar/capture.jsonl` | Capture output (gitignored, runtime data) |

## Commit

`704c1d5` — "Add Bifrost passthrough capture sidecar (Bifrost-kh0)" — added
`sidecar/proxy.py`, `start_sidecar.cmd`, appended `sidecar/capture.jsonl` to
`.gitignore`. File-scoped `git add` (no `git add .`).

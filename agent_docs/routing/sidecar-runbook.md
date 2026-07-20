# Sidecar Runbook

Run + verify Bifrost routing sidecar (v2.1, Bifrost-tfz).

## What it is

Stdlib (`ThreadingHTTPServer` + `http.client`) proxy package at `sidecar/`
(run with `python -m sidecar`). Listens `127.0.0.1:8088` → Bifrost `127.0.0.1:8080`.

Pooled models (`sidecar/pools.json` keys): rewrite `model` → `provider/model`,
own `fallbacks` array, pin session to least-loaded provider, cool down failures
globally, log to `sidecar/sidecar.log`. `sidecar/capture.jsonl` is recorded
only when `--capture` is passed (off by default).

Non-pooled models: verbatim passthrough, no state, no headers, no logs —
indistinguishable from hitting Bifrost directly.

## Prerequisites

- Python 3.14 at `C:\Users\vadash\AppData\Local\Python\pythoncore-3.14-64\python.exe`
  (stdlib only).
- Bifrost on `127.0.0.1:8080`.
- `curl.exe` for verify.

## Start

```cmd
python -m sidecar
```

With raw capture (records `sidecar/capture.jsonl`):
```cmd
python -m sidecar --capture
```

Or hub:
```json
{"op":"start","name":"sidecar",
 "application":"C:\\Users\\vadash\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe",
 "args":["-m","sidecar"],"cwd":"C:/projects/_llm/Bifrost",
 "ready":{"log":"listening on","timeout":30}}
```

Banner: `[sidecar] listening on http://127.0.0.1:8088 -> http://127.0.0.1:8080
pooled_models=N ...`. Edit `pools.json` requires restart (startup-only load).

## Repoint client

Base URL `http://127.0.0.1:8080/v1` → `http://127.0.0.1:8088/v1`. Bifrost
unchanged.

## Verify

### Pooled: least-loaded + distinct
```cmd
curl.exe -s -D - -X POST http://127.0.0.1:8088/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"z-ai/glm-5.2\",\"prompt_cache_key\":\"sess-A\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
curl.exe -s -D - -X POST http://127.0.0.1:8088/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"z-ai/glm-5.2\",\"prompt_cache_key\":\"sess-B\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```
Expect 200, different `x-sidecar-pin` values, `x-sidecar-session` = first 12
chars of key. `sidecar.log` two lines, distinct `primary`.

### Stickiness
Repeat `sess-A` request → same `x-sidecar-pin`.

### Non-pooled passthrough
```cmd
curl.exe -s -D - -X POST http://127.0.0.1:8088/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"poolside/laguna-xs-2.1\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```
Expect 200, **no** `x-sidecar-*` headers, **no** new `sidecar.log` line
(transparent passthrough). `capture.jsonl` is never written unless
`--capture` was passed, and even then only for pooled requests.

### Streaming
Add `"stream":true` to pooled request → incremental `data:` SSE events,
`sidecar.log` still records `served`/`fell_back`.

### Unit tests (no live Bifrost needed)
```cmd
python -m unittest sidecar.tests.test_routing -v
```
Stdlib `unittest` only. Covers `build_send_order` (send-order + desperate),
`fallback_feedback` (re-pin + first-skipped cooldown on 2xx fallback), cooldown
regression, and cold-start pin spread. Run before committing routing changes.

## sidecar.log record shape

```json
{"ts":"...","session":"<key[:12]>","source":"cache_key|prev_resp|hash",
 "pin":<idx>,"primary":"nvidia-7","ring":["nvidia-7","nvidia-8",...],
 "cooldowns":["nvidia-1"],"served":"nvidia-8","is_fallback":true,
 "fell_back":true,"repin":"nvidia-8|null","status":200,"desperate":false}
```

## Files

| File | Purpose |
|---|---|
| `sidecar/` | Proxy package (stdlib; `__main__.py` entrypoint) |
| `sidecar/pools.json` | Pooled models → ordered provider list |
| `start_sidecar.cmd` | Repo-root launcher (`python -m sidecar`) |
| `sidecar/sidecar.log` | Decision log (pooled only, gitignored) |
| `sidecar/capture.jsonl` | Raw capture (pooled only, gitignored; **off by default — add `--capture`**) |
| `sidecar/tests/test_routing.py` | Stdlib `unittest` for `build_send_order`/`fallback_feedback`/cooldowns/cold-start (see Verify) |

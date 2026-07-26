# Sidecar Runbook

Run + verify Bifrost routing sidecar (v2.2, Bifrost-tfz).

## What it is

Stdlib (`ThreadingHTTPServer` + `http.client`) proxy package at `sidecar-2/`
(run with `python -m sidecar-2`). Listens `127.0.0.1:8088` → Bifrost `127.0.0.1:8080`.

Pooled models (`sidecar-2/pools.json` keys): rewrite `model` → `provider/model`,
own `fallbacks` array, pin session to least-loaded provider, cool down failures
globally, log to `sidecar-2/sidecar.log`. `sidecar-2/capture.jsonl` is recorded
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
python -m sidecar-2
```

Reserve the first 3 alpha-sorted nvidia providers for the Bifrost auto route
(sidecar pools the remaining 12):
```cmd
python -m sidecar-2 --reserve-bifrost 3
```

With raw capture (records `sidecar-2/capture.jsonl`):
```cmd
python -m sidecar-2 --capture
```

Gotcha: for pooled requests the captured `request_body` is the **rewritten**
body (model → `{primary}/{pooled_model}` + 2 fallbacks), not the original —
`state.plan_pooled_request` mutates `parsed` in place and `proxy.py` passes
that same dict to `pooled.write_capture`. Non-pooled passthrough records the
verbatim body. Deliberate carry-over from the proxy/pooled refactor; changing
it is a behaviour change, not a bug.

`start_sidecar.cmd` (repo-root launcher) deletes `sidecar-2/sidecar.log` before
launch (rotation guard) and runs `python -m sidecar-2 --reserve-bifrost 3`
(reserves first 3 alpha-sorted nvidia providers for the Bifrost auto route;
sidecar pools the remaining 12).

Hub start: name `sidecar`,
application `C:\Users\vadash\AppData\Local\Python\pythoncore-3.14-64\python.exe`,
args `["-m","sidecar-2","--reserve-bifrost","3"]`, ready on log `listening on`.
Banner: `[sidecar] listening on http://127.0.0.1:8088 -> http://127.0.0.1:8080
pooled_models=N ...`. `pools.json` is startup-only — edits require restart.

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
python -m unittest discover -s sidecar-2.tests -v
```
Stdlib `unittest` only. Covers `build_send_order` (send-order + desperate),
`fallback_feedback` (re-pin + first-skipped cooldown on 2xx fallback),
`plan_pooled_request` (pooled decision + model/fallbacks rewrite),
`sanitize_request_body` (orchestrates the three Claude rewriters),
cooldown regression, cold-start pin spread, `shuffle_pools`,
`load_pools(reserve_bifrost=N)`, sanitize rewrites, and `extract_provider`
against recorded SSE fixtures.

### Relay invariants (`proxy.py`)

`Handler._open_upstream_meta(path, command, body, headers) -> (conn, resp,
status, resp_headers, is_stream)` is the shared open+request+getresponse+
Content-Type/SSE-detection surface, called by both the single path
(``_forward_and_relay``) and each fast lane (``_proxy_fast.run_lane``).
The caller owns the response read loop and the two are **deliberately not
unified**:
- **Single path** relays incrementally (writes chunks to the client as they
  arrive) and does NOT salvage ``IncompleteRead`` — a transport error there
  must surface to the client, not a partial relay. Do not add salvage here.
- **Fast path** buffers the full body into a local ``buf`` and salvages
  ``IncompleteRead.partial`` so a truncated lane can still win the race
  (biggest-partial rule). Do not unify the salvage into the single path.

``RoutingState.set_lane_pins`` is the only writer of a fast session's
``{"pin","pin2"?,"seen"}`` record after the lane split; ``assign_pin_pair``'s
least-loaded ``pin_b`` can be swapped by ``build_fast_lanes``, so the record
is re-pinned to the ACTUAL lane primaries. Do not write the record shape
inline in ``fast.py`` — go through ``set_lane_pins``.

## sidecar.log record shape

Emitted by `pooled.write_decision_log` in `sidecar-2/pooled.py` (Step 3 of
the proxy.py refactor split the former `write_logs` into `write_capture` +
`write_decision_log`): ts,
session, source, pin, primary, ring, cooldowns, served, fell_back, repin,
status, desperate.
`session` is the key truncated to 12 chars; `ring` is the kept send-order list
for this request; `fell_back` is the derived fallback indicator (`is_fallback`
is never emitted by this Bifrost build and is not logged). `repin` is the
provider this request actually re-pinned the session to (returned by
`apply_feedback`), else `null` when no re-pin happened — it does NOT echo the
session's standing pin. A steady session (served by its primary, no fallback)
logs `repin: null` even though it stays pinned.

## Files

| File | Purpose |
|---|---|
| `sidecar-2/` | Proxy package (stdlib; `__main__.py` entrypoint) |
| `sidecar-2/pools.json` | Pooled models → ordered provider list |
| `start_sidecar.cmd` | Repo-root launcher (`python -m sidecar-2`) |
| `sidecar-2/sidecar.log` | Decision log (pooled only, gitignored) |
| `sidecar-2/capture.jsonl` | Raw capture (pooled only, gitignored; **off by default — add `--capture`**) |
| `sidecar-2/tests/test_routing.py` | Stdlib `unittest` for `build_send_order`/`fallback_feedback`/cooldowns/cold-start (see Verify) |

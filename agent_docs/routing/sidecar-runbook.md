# Sidecar Runbook

Run + verify Bifrost routing sidecar (v2.2 lineage; package `__version__=2.3.0`).

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

## Graceful shutdown (Ctrl-C / SIGTERM)

`__main__.py:main()` installs SIGINT + SIGTERM handlers that call
`server.shutdown()` from a side thread (calling it from the `serve_forever`
thread deadlocks), then a `finally` runs `server.server_close()` and closes
both `JsonlWriter`s. So Ctrl-C and `kill -INT <pid>` both drain cleanly:
print `[sidecar] shutting down ...` then `[sidecar] stopped`, exit in <1s,
free the listening socket, flush `sidecar.log`. A second signal is a no-op
(Event-guarded, one-shot shutdown thread).

**Windows quirk.** `os.kill(pid, signal.SIGTERM)` calls `TerminateProcess`
directly and bypasses Python signal handlers — the process dies with NO
`shutting down` banner and the writers' `finally` does NOT run. To exercise
the graceful path on Windows, send a console Ctrl-C event (Ctrl-C in the
foreground terminal, or hub `send` with `keys:["CTRL_C"]`); SIGINT raises
`KeyboardInterrupt` on the main thread and the `except KeyboardInterrupt`
arm drives the same drain. `stop_sidecar.cmd` keeps `taskkill /F` as the
last-resort hammer for a wedged process that ignores SIGTERM/SIGINT.

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

`tests/test_handler.py` adds end-to-end transport coverage: an in-process
`StubBifrost(ThreadingHTTPServer)` + the real `Sidecar` threaded server,
asserting the single-path and `/fast` relays both work over real sockets
(`tests/test_handler.py::TestHandlerRelay`). The single-path test is the
regression test for the `resp.getheaders` bound-method-passed-uncalled bug
in `_filter_response_headers`. Do not add a third test there lightly — start
a fresh `ThreadingHTTPServer` per test, `tearDown` `shutdown()` from the
main thread (never from the `serve_forever` daemon) then `server_close()`.

Invocation gotcha: `sidecar-2` has a hyphen, so it is not a valid Python
identifier. `discover -s sidecar-2.tests` works from the repo root (parent
of `sidecar-2/`). A single-module run `python -m unittest tests.test_handler`
from inside `sidecar-2/` fails with `ModuleNotFoundError: No module named
'sidecar-2'`; run `python -m unittest sidecar-2.tests.test_handler.<Case>`
from the repo root instead.

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

Gotcha: an exception raised in the single-path relay AFTER `send_response`
set `_response_line_sent=True` (e.g. inside `_filter_response_headers`
mid-relay) does NOT surface as a 502. The `except` at the top of `proxy()`
guards `send_error(502)` with `if not self._response_line_sent`, so it is
skipped and `finally` just closes the connection → the client sees
`RemoteDisconnected` / `BadStatusLine`, never a 502. The bug this prevents:
`resp.getheaders` (bound method, un-called) was once passed where a list of
pairs was expected; iterating it raised `TypeError` just after the status
line was sent, dropping the connection mid-response. If a 502 is genuinely
desired for mid-relay failures, set `self._response_line_sent = False`
before the failing step — but prefer fixing the relay so it doesn't raise.

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

## JsonlWriter: persistent handle + fsync daemon (`io_jsonl.py`)

`JsonlWriter` keeps a single append handle open for its lifetime (lazily
opened on first `write`) — the old open()+flush()+close() per record was
pure syscall overhead on the hot pooled path. `write()` still flushes after
every record, so a process kill leaves a complete line on disk; **only
`fsync` is deferred to a timer.**

Module-level `_FsyncScheduler` is a single daemon thread (`name=
"sidecar-fsync"`) started lazily on the first `JsonlWriter` registration.
Every `config.FSYNC_INTERVAL_SECS` (5.0s) it `os.fsync`s each live writer's
handle under that writer's lock, swallowing `OSError` (handle may be closed
or a redirected pipe). Set `FSYNC_INTERVAL_SECS=0` to disable (skips thread
spawn entirely).

Lifecycle contracts worth knowing before touching `io_jsonl.py`:
- `close()` is idempotent and unregisters from the scheduler. `__del__`
  backstops callers that forget to close — it swallows all errors and never
  replaces an explicit `close()`.
- **Windows + tests.** An open file handle blocks `shutil.rmtree` with
  `PermissionError`. `test_handler.py::tearDown` calls `capture_writer.close()`
  + `log_writer.close()` AFTER `server_close()` — keep that ordering when
  you add new live-server tests, or the tempdir teardown wedges on Windows.

## Files

| File | Purpose |
|---|---|
| `sidecar-2/` | Proxy package (stdlib; `__main__.py` entrypoint) |
| `sidecar-2/pools.json` | Pooled models → ordered provider list |
| `start_sidecar.cmd` | Repo-root launcher (`python -m sidecar-2`) |
| `sidecar-2/sidecar.log` | Decision log (pooled only, gitignored) |
| `sidecar-2/capture.jsonl` | Raw capture (pooled only, gitignored; **off by default — add `--capture`**) |
| `sidecar-2/tests/test_routing.py` | Stdlib `unittest` for `build_send_order`/`fallback_feedback`/cooldowns/cold-start (see Verify) |
| `sidecar-2/tests/test_handler.py` | Stdlib `unittest` end-to-end transport test: real `Sidecar` server vs in-process stub upstream (single-path + `/fast` relays); regression test for the `resp.getheaders` bug |

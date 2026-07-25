# Fast race endpoint (`/fast/v1`)

Second baseUrl on the sidecar: `http://127.0.0.1:8088/fast/v1/...`. For
pooled models it fires **two simultaneous upstream requests** over disjoint
lanes built by splitting the session's send-order ring odd/even (ring
`1-2-3-4-5-6-7` → lane A `[1,3,5]`, lane B `[2,4,6]`), buffers both, and
returns one full buffered response. Non-pooled via `/fast` = plain
passthrough (prefix stripped, verbatim forward, no state/logs/headers —
same transparency rules as the non-fast path).

## Mechanics

- **Lanes** (`state.py::build_fast_lanes`): ring from `build_send_order`
  (pin A, cold-first, hot last); pin B swapped into ring position 1 when
  cold; lane A = `ring[0::2][:3]`, lane B = `ring[1::2][:3]`. The `[:3]`
  keeps the upstream fallback cap (primary + 2 — see
  [sidecar-routing-policy.md](sidecar-routing-policy.md)); do not widen.
  Lane B is `[]` only for a 1-provider pool.
- **Two pins** (`assign_pin_pair`): pin records carry `{"pin", "pin2",
  "seen"}`; single-path records simply have no `pin2` key (always read with
  `.get("pin2")`). `_least_loaded` counts BOTH pins as load. After planning,
  the bookkeeping record is rewritten to the ACTUAL lane primaries.
- **Response timing** (`proxy.py::_proxy_fast`): respond as soon as a lane
  finishes *complete* (2xx + terminal SSE marker for streams; see
  `fast.py::is_complete`). The loser keeps running as a daemon thread and
  still applies feedback + logging when done — never cancelled, never
  joined. Only when the first finisher is premature-truncated does the
  handler wait for lane B (`fast.py::pick_winner`: complete-first, then
  biggest 2xx partial, then any HTTP response over a transport exception,
  else fabricated 502). TTFT is sacrificed by design on `/fast` — buffered
  SSE keeps `text/event-stream` but arrives all at once.
- **Feedback** (`fast.py::fast_lane_feedback` + `pooled.py::
  apply_fast_feedback`): deliberately different from the single path — when
  a fallback serves, the lane primary IS cooled (plus every skipped
  intermediate before the server; the server is never cooled) and the lane
  slot re-pins to the server (`re_pin_lane` writes only that lane's slot).
  Error path (transport error, 5xx, 429) cools the lane primary and
  advances to `lane_keep[1]`. Other 4xx: no feedback.
- **Observability**: two `sidecar.log` records per fast request (one per
  lane; path keeps the `/fast` marker). Response headers:
  `x-sidecar-pin: <laneA_primary>,<laneB_primary>`,
  `x-sidecar-fast-winner: a|b`.

## Concurrency

A new same-session request while a previous fast request is still running
leaves the old lanes alone; they finish and apply feedback normally
(matches the single path under concurrent requests). Lane threads are
daemon, one `queue.Queue` record each, reads bounded by
`cfg.upstream_timeout`, so the handler's `q.get()` can never hang.

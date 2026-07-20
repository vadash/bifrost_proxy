# Session Identity Derivation

How sidecar derives which "session" a request belongs to. Ground truth from v1
passthrough capture (`sidecar/capture.jsonl`, 2026-07-20).

## What capture showed

Two real subagent sessions (Bun `User-Agent`, `POST /v1/responses`):

- **All entries `has_previous_response_id == false`.** `previous_response_id`
  never set in any subagent request — not first turn, not continuation.
- Continuation re-sends prior turns inline in `input` array (sliding window).
  Array grows `len 1` → `len 3` / `len 4` (`[user, assistant, user, user]`).
- **Session identity carried by `prompt_cache_key`** — stable UUID per
  subagent session, present every request body. Two distinct UUIDs observed
  (one per subagent), constant across each session's turns.

## v2 cascade (shipped)

`derive_session_key(body) -> (session_key, source)`, first match wins:

1. `body.prompt_cache_key` truthy → `(str(that), "cache_key")`.
2. `body.previous_response_id` present AND found in `RESP_MAP` →
   `(RESP_MAP[id].session, "prev_resp")`. Kept for completeness, not observed
   in practice.
3. Hash fallback → `("h:" + sha256(instructions + "\n" + first_user_text)[:32],
   "hash")`. For clients sending neither.

## Pin assignment (NOT hash)

Pin is **least-loaded start**, not `sha256 % N` (that formula was an earlier
design, superseded). First time session seen → pin to non-cooled provider with
fewest live pinned sessions; tie → uniform-random choice (not lowest index) so
a fresh pool doesn't stampede every session onto `nvidia-1` on cold start. See
`state.py` `RoutingState.assign_pin` (random tie-break via `self._rng`).

## Why this matters

Per-session provider pinning = prompt-cache locality. Wrong session identity →
two sessions same provider (cache thrash) or one session scattered (no benefit).
`prompt_cache_key` is reliable signal client already provides.

# Session Identity Derivation

How the sidecar identifies which "session" a request belongs to, and the
ground-truth discovery (from the v1 passthrough capture) that corrected the
original design assumption.

## The original assumption (from the Bifrost-0xc epic)

The cascade was planned as:

1. `previous_response_id` present → look up sidecar's `resp_id → session` map;
   reuse that session (handles multi-turn chains that omit earlier turns).
2. Else → hash(instructions + first user-turn text) → session_key. Sticky
   thereafter.
3. Map the returned response id → session_key for future
   `previous_response_id` lookups.

## What the capture actually showed (v1 passthrough, 2026-07-20)

Two real subagent sessions (Bun `User-Agent`, `POST /v1/responses`) drove the
sidecar. The capture (`sidecar/capture.jsonl`) showed:

- **All 12 entries had `has_previous_response_id == false`.** The
  `previous_response_id` field was **never** set in any subagent request —
  not on the first turn, not on continuation turns.
- **Continuation is done by re-sending prior turns inline in the `input`
  array.** The `input` array grew from `len 1` (initial `"SESSION NUMBA ONE"`)
  to `len 3` / `len 4` on follow-up turns: `[user, assistant, user, user]`.
  The client is a "sliding window" — it re-sends the conversation history each
  turn rather than chaining via `previous_response_id`.
- **Session identity is carried by `prompt_cache_key`**, a stable UUID per
  subagent session present in every request body. Two distinct UUIDs were
  observed (one per subagent), constant across all of each session's turns.

### Evidence

Session A — `019f805d-7c0e-7000-815a-7a17eb819c97`:
| ts | input len | roles | previous_response_id |
|---|---|---|---|
| 19:36:28 | 1 | `[user]` | None |
| 19:42:40 | 4 | `[user, assistant, user, user]` | None |
| 19:43:53 | 3 | `[user, assistant, user]` | None |

Session B — `019f805d-8698-7000-9725-e41965101481`:
| ts | input len | roles | previous_response_id |
|---|---|---|---|
| 19:36:32 | 1 | `[user]` | None |
| 19:42:38 | 4 | `[user, assistant, user, user]` | None |
| 19:43:59 | 3 | `[user, assistant, user]` | None |

## Corrected derivation (for v2 sidecar, Bifrost-tfz)

Session identity should be derived from `prompt_cache_key` first — it is the
field the client actually uses to mark a session. The cascade becomes:

1. `prompt_cache_key` present in request body → use it directly as the
   session_key. (Verified stable across continuation turns.)
2. Fallback: `previous_response_id` present → sidecar's `resp_id → session` map.
   (Kept for completeness; not observed in practice but cheap to support.)
3. Fallback: hash(instructions + first user-turn text) → session_key.
   (For clients that send neither cache key nor previous_response_id.)

Pin = `sha256(session_key)[:8]` interpreted little-endian `% N` (N = provider
count). Same key → same pin across restarts.

## Why this matters for routing

The whole point of the sidecar is per-session provider pinning for
prompt-cache locality. If session identity is wrong, two different sessions
get pinned to the same provider (cache thrash) or one session's pins scatter
across providers (no cache benefit). `prompt_cache_key` is the reliable signal
the client already provides.

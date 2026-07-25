# Session Identity Derivation

How the sidecar derives which "session" a request belongs to. Ground truth
from passthrough capture (2026-07-20): subagents carry a stable per-session
`prompt_cache_key` UUID on every request; continuations re-send prior turns
inline in the `input` array.

## Cascade (sidecar-2, shipped)

`derive_session_key(body) -> (session_key, source)`, first match wins:

1. `body.prompt_cache_key` truthy → `(str(that), "cache_key")`.
2. Hash fallback → `("h:" + sha256(instructions + "\n" + first_user_text)[:32],
   "hash")`. For clients sending no cache key. Handles both `/v1/responses`
   (`input`) and `/v1/chat/completions` (`messages`).

`prompt_cache_key` survives to the provider wire on the nvidia and
Anthropic/Bedrock paths, so it is safe as the session signal.

## Pin assignment (NOT hash)

Pin is **least-loaded start**, not `sha256 % N`. First time a session is seen
→ pin to the non-cooled provider with fewest live pinned sessions; tie →
uniform-random choice so a fresh pool doesn't stampede `nvidia-1` on cold
start. See `state.py` `RoutingState.assign_pin`.

## Why this matters

Per-session provider pinning = prompt-cache locality. Wrong session identity →
two sessions same provider (cache thrash) or one session scattered (no
benefit).

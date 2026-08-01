# Sidecar Routing Policy

The sidecar's own send-order + cooldown decisions for pooled requests (what we
send to Bifrost and how we react to its response). Pin *assignment* (which
provider a new session starts on) is covered in
[session-identity.md](session-identity.md); Bifrost's *mechanics* (prefix,
`fallbacks`, `routing_info`) are in [bifrost-routing-facts.md](bifrost-routing-facts.md).

Two pure helpers split the logic out of `proxy.py` so it's testable without a
live Bifrost. Tests: `sidecar-2/tests/test_routing.py` (run:
`python -m unittest discover -s sidecar-2.tests -v`).

## Send order: full ring, hot appended last

`RoutingState.build_send_order(providers, pin, now) -> (list[str], desperate)`
(`state.py`). Rotate the ring to start at the pinned provider; keep the WHOLE
ring — never drop hot/in-cooldown providers — but move them to the END so
Bifrost only reaches them as a last resort. `desperate` is True iff no cold
provider exists (the request still goes out with the full ring).

Because Bifrost walks the body `fallbacks`
verbatim (see routing-facts), **send-order == try-order up to the cap below**,
so `keep_list[1]` is always the first provider tried after the primary.

## Upstream fallback cap: primary + 2 (or primary only when desperate / probing)

`plan_pooled_request` (*state.py*) rewrites the body via `state.rewrite_body`,
which splits primary + fallbacks into `order[0]` and `order[1:1+max_fallbacks]`
(keyword-only, default `max_fallbacks=2`). The normal pooled path forwards
only **`keep_list[1:3]`** — the forced primary plus two fallbacks, never the
whole ring — even though `keep_list` retains all providers. Why: during NVIDIA
upstream congestion the relay returns 504 after its first-byte timeout, and
forwarding all 11 fallbacks of a 12-provider pool made one congested request
burn ~31s x 12 before failing. The cap bounds that to at most 3 attempts
(~93s). Edge cases are pure slicing: a 1-provider pool forwards `[]`, a
2-provider pool forwards 1 fallback, slicing past the end is safe.

The cap is **not** a flat hard-coded slice. On two paths the planner forces
`max_fallbacks=0` so one client request makes exactly ONE upstream call
(`"fallbacks": []`), not three:

- **Desperate** (all providers hot): see the send-order section — `desperate`
  is True, so the body forwards the primary only. This kills the 3x
  amplification that held the account rate-limit window open during the
  original 429 storm.
- **Half-open circuit probe**: the single request the breaker lets through to
  test a lapsed limit is likewise primary-only — a probe must not fan out 3
  lanes into the limit it is testing.

Both `/fast` lanes get the same `max_fallbacks=0` on their desperate/probe path
(`plan_fast_request` mirrors this). The full ring stays in memory so pins,
cooldowns, and `fallback_feedback` (which reads `keep_list` directly, not the
forwarded `fallbacks`) are unaffected: `keep_list[1]` is still the
first-skipped stampede target, the whole-chain-failure path still cools
`keep_list[0]`. The breaker itself — when to drop to primary-only by opening
the whole pool — is a separate concern; see
[pool-circuit-breaker.md](pool-circuit-breaker.md).

## Post-response feedback: two paths

`fallback_feedback(keep_list, served, status) -> (repin_to, cooldown_provider)`
(`state.py`, pure — no `self`, no lock) decides the 2xx-fallback path.
`pooled.apply_feedback` (`sidecar-2/pooled.py`) runs it under the state lock
and picks one of two mutually-exclusive paths:

**2xx served by a fallback** (`repin_to is not None`): re-pin the session to
the server that answered (`served`); cool the **first-skipped provider**
(`keep_list[1]`) — *only if it was actually skipped* (`served` is neither
`keep_list[0]` nor `keep_list[1]`). The primary the sidecar forced is **NOT**
cooled on this path: the session leaves it anyway via the re-pin, and the
first-skipped is the stampede/overload target.

**Whole-chain failure** (`elif err_path`): 5xx/429/exception from the whole
chain → cool the forced primary (`keep_list[0]`) and advance the pin one step.
A 429 is also an ACCOUNT-WIDE signal: when cooling the primary leaves EVERY
provider hot (`state.all_hot`), the pool-level circuit TRIPS (stops forwarding
the whole pool) instead of repinning into a ring that will just 429 again;
generally see [pool-circuit-breaker.md](pool-circuit-breaker.md). A transport
error or a 5xx is a per-provider fault only and never trips the circuit.
Any genuine recovery — a 2xx (fallback or primary) on a pool whose circuit was
actually open — RESETS the circuit and clears the collateral cooldowns it left
behind; an ordinary 2xx on a pool that never tripped leaves legitimate
per-provider cooldowns alone.

`apply_feedback` / `apply_fast_feedback` return a **`(repin, circuit_note)`**
pair, not a bare provider. `repin` is the provider this request re-pinned to
(`repin_to`, or `keep_list[1]` on the failure path), else `None` — threaded
straight into the `repin` log field so logging never re-derives it from
`state.pins` under a second lock. `circuit_note` is `"tripped"` (this 429
opened the circuit), `"reset"` (this 2xx recovered a tripped circuit), or
`None` (the common case: no circuit event) — threaded into the decision log's
`circuit` field (see runbook "sidecar.log record shape"). The breaker's
state machine, escalating window, and reset-narrowing internals are in
[pool-circuit-breaker.md](pool-circuit-breaker.md).

## `is_fallback` is NOT the signal; `fell_back` is

`fallback_feedback` deliberately does **not** consult Bifrost's
`routing_info.is_fallback`. The real signal is `served != keep_list[0]` — the
provider that answered differs from the primary the sidecar forced.
On the deployed Bifrost build, `is_fallback`/`primary_provider` are never
emitted at all (verified during the sidecar-2 rebuild, even under a forced
fallback), so `sidecar.log` no longer records `is_fallback`; the derived
`fell_back` (`served is not None and keep_list and served != keep_list[0]`)
is the only fallback indicator.

## Why these decisions

When Bifrost falls back off the forced primary and a later provider serves,
every session pinned to that primary would otherwise stampede onto the same
next-in-ring provider and overload it. Cooling the first-skipped provider
(stampede target) and re-pinning to the server spreads load back out.

## Cold-start randomization: shuffled ring, reserved prefix

Two startup-time decisions shape the ring before any request is routed:

**Per-pool shuffle (``RoutingState.__init__(shuffle_pools=True)``).** Each
pool's providers are shuffled once at sidecar startup
(``self._rng.sample`` over ``cfg.pools``), so the cold-start *fallback order*
(the body ``fallbacks`` array) is randomized across runs, not merely
alpha-sorted. ``build_send_order`` still rotates the ring to start at the
session pin, so the primary stays the pinned provider and the *sequence after
it* is the shuffled ring. ``shuffle_pools=False`` keeps the declared order
(used by deterministic tests). This is orthogonal to the least-loaded pin
assignment's random tie-break: that decides *which* pin a cold session lands
on; the shuffle decides the order of the *rest* of the ring.

**Reserve prefix for Bifrost auto (``--reserve-bifrost N``).** When N > 0,
``load_pools`` reserves at most **1** alpha-sorted provider from the
**first** pool only (``pools.json`` first key), capping at 1 regardless of N
so a pool is never drained below its declared size minus 1. Every other pool
keeps its full provider list — this matters when a config adds a smaller
second tier (e.g. a 2-provider ``kilo-auto/free`` pool alongside the 15-pool
``z-ai``); reserving from every pool used to drain the small pool to 0 and
502 with ``IndexError``/``TypeError`` on the fast path. The reservation
matches Bifrost's lexicographic auto-sort (``nvidia-1, nvidia-10, nvidia-2,
…``) so the sidecar never reaches the reserved providers. Pools with <= 1
providers are skipped entirely. ``start_sidecar.cmd`` passes
``--reserve-bifrost 3`` (reserves ``nvidia-1`` from the first pool, leaving
14 for sidecar pooling; all other pools keep their full list). CLI flag:
integer count, default 0 (no reservation).

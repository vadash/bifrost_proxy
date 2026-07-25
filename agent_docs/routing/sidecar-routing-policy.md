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

## Upstream fallback cap: primary + 2

`plan_pooled_request` (*state.py*) writes only **`keep_list[1:3]`** into the
forwarded body's `fallbacks` — the forced primary plus two fallbacks, never
the whole ring — even though `keep_list` retains all providers. Why: during
NVIDIA upstream congestion the relay returns 504 after its first-byte
timeout, and forwarding all 11 fallbacks of a 12-provider pool made one
congested request burn ~31s x 12 before failing. The cap bounds that to at
most 3 attempts (~93s). The full ring stays in memory so pins, cooldowns,
and `fallback_feedback` (which reads `keep_list` directly, not the forwarded
`fallbacks`) are unaffected: `keep_list[1]` is still the first-skipped
stampede target, the whole-chain-failure path still cools `keep_list[0]`.
Edge cases are pure slicing: a 1-provider pool forwards `[]`, a 2-provider
pool forwards 1 fallback, slicing past the end is safe. The cap is a
hard-coded slice, not a config knob.

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
``load_pools`` drops the first N alpha-sorted providers of each pool
(matching Bifrost's own lexicographic auto-sort: `nvidia-1, nvidia-10,
nvidia-2, …`), reserving them for the Bifrost auto route so the sidecar never
routes to them. `start_sidecar.cmd` passes `--reserve-bifrost 3` (first 3 of
the 15 nvidia providers), leaving 12 for sidecar pooling. CLI flag: integer
count, default 0 (no reservation).

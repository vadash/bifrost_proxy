# Sidecar Routing Policy

The sidecar's own send-order + cooldown decisions for pooled requests (what we
send to Bifrost and how we react to its response). Pin *assignment* (which
provider a new session starts on) is covered in
[session-identity.md](session-identity.md); Bifrost's *mechanics* (prefix,
`fallbacks`, `routing_info`) are in [bifrost-routing-facts.md](bifrost-routing-facts.md).

Two pure helpers split the logic out of `proxy.py` so it's testable without a
live Bifrost. Tests: `sidecar/tests/test_routing.py` (run:
`python -m unittest sidecar.tests.test_routing -v`).

## Send order: full ring, hot appended last

`RoutingState.build_send_order(providers, pin, now) -> (list[str], desperate)`
(`state.py`). Rotate the ring to start at the pinned provider; keep the WHOLE
ring — never drop hot/in-cooldown providers — but move them to the END so
Bifrost only reaches them as a last resort. `desperate` is True iff no cold
provider exists (the request still goes out with the full ring).

`proxy.py` writes `model = "{keep_list[0]}/{pooled}"` and `fallbacks = ["{p}/{pooled}" for p in keep_list[1:]]`. Because Bifrost walks the body `fallbacks`
verbatim (see routing-facts), **send-order == try-order**, so `keep_list[1]` is
always the first provider tried after the primary.

This reversed the earlier behavior that *dropped* hot providers from the chain.
Keeping them last (not dropping) means every request still has the full safety
net, but a hot provider can't be the stampede target.

## Post-response feedback: two paths

`fallback_feedback(keep_list, served, status) -> (repin_to, cooldown_provider)`
(`state.py`, pure — no `self`, no lock) decides the 2xx-fallback path.
`_apply_feedback` (`proxy.py`) runs it under the state lock and picks one of two
mutually-exclusive paths:

**2xx served by a fallback** (`repin_to is not None`): re-pin the session to
the server that answered (`served`); cool the **first-skipped provider**
(`keep_list[1]`) — *only if it was actually skipped* (`served` is neither
`keep_list[0]` nor `keep_list[1]`). The primary the sidecar forced is **NOT**
cooled on this path: the session leaves it anyway via the re-pin, and the
first-skipped is the stampede/overload target. This reverses the old
`cooldown_trigger(primary, ...)`.

**Whole-chain failure** (`elif err_path`): 5xx/429/exception from the whole
chain → cool the forced primary (`keep_list[0]`) and advance the pin one step.
Unchanged from v2.

## `is_fallback` is NOT the signal; `fell_back` is

`fallback_feedback` deliberately does **not** consult Bifrost's
`routing_info.is_fallback`. The real signal is `served != keep_list[0]` — the
provider that answered differs from the primary the sidecar forced.
`sidecar.log` keeps the raw `is_fallback` field for reference and adds the
derived `fell_back` (`served is not None and keep_list and served != keep_list[0]`),
which is the authoritative fallback indicator.

## Why these decisions

When Bifrost falls back off the forced primary and a later provider serves,
every session pinned to that primary would otherwise stampede onto the same
next-in-ring provider and overload it. Cooling the first-skipped provider
(stampede target) and re-pinning to the server spreads load back out.

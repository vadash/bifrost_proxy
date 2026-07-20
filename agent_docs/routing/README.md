# Routing

The Bifrost sidecar project: pin each agent/session to a deterministic provider
to fix prompt-cache locality, instead of Bifrost's default alpha-sort which
makes every request start at `nvidia-1`.

## Read these first

1. **[bifrost-routing-facts.md](bifrost-routing-facts.md)** — verified
   mechanics of Bifrost's provider selection, the `provider/model` prefix, the
   body `fallbacks` array, and `routing_info` extraction. Every claim backed by
   curl proofs or source anchors. **Start here before touching routing logic.**
2. **[session-identity.md](session-identity.md)** — how the sidecar identifies
   a session. Ground truth from the v1 capture: `prompt_cache_key` is the real
   signal, NOT `previous_response_id`.
3. **[sidecar-runbook.md](sidecar-runbook.md)** — how to run, verify, and
   analyze the sidecar (v1 passthrough, Bifrost-kh0).

## Phase status

- **Phase 1 (Bifrost-kh0): passthrough capture.** DONE. `sidecar/proxy.py`
  observes traffic without rewriting. Built, committed, verified.
- **Phase 2 (Bifrost-tfz): full pinning + ring + cooldown.** NOT STARTED.
  Replaces passthrough with session-pinned provider selection: ring start,
  rotated `fallbacks` array, global cooldown, "no spontaneous return".

## The bug this fixes

Bifrost alpha-sorts provider candidates, so every request for `z-ai/glm-5.2`
starts at `nvidia-1` and walks `nvidia-1, nvidia-10, nvidia-2, ...`
(lexicographic, not numeric). Different agents hammer the same provider,
killing prompt-cache locality. The sidecar's job is to pin each session to a
deterministic ring start and own the fallback order.

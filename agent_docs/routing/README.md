# Routing

Bifrost sidecar: pin each session to deterministic provider for prompt-cache
locality. Fixes Bifrost alpha-sort (every request starts `nvidia-1`, walks
`nvidia-1, nvidia-10, nvidia-2, ...` lexicographic not numeric).

Status: **sidecar-2** (rebuild of v2.2). Session-pinned routing with global
cooldown. Pure decision helpers live in `state.py`
(`build_send_order`, `fallback_feedback`, `plan_pooled_request`) and
`predicates.py` (`is_2xx`, `is_sse_content_type`); pooled
post-response concerns (feedback application + decision-log write) live in
`pooled.py` (`apply_feedback`, plus `write_capture`/`write_decision_log`
split out of the former `write_logs`); all are wired from the thin
`proxy.py` HTTP-transport layer. Serving provider extracted from response
bodies by `routing_info.py::extract_provider`. Pooled models declared in
`sidecar-2/pools.json`. Non-pooled = verbatim passthrough, no logs (one
exception: Claude empty-thinking sanitize — see #5 below).

Second baseUrl `/fast/v1/...` races pooled models over two disjoint lanes
(odd/even ring split, two session pins, per-lane feedback, biggest-partial
fallback) — see **[fast-race-endpoint.md](fast-race-endpoint.md)**.

## Read these first

1. **[bifrost-routing-facts.md](bifrost-routing-facts.md)** — Bifrost routing
   mechanics: `provider/model` prefix, body `fallbacks` array, `routing_info`
   fields, what does NOT work. **Start here before touching routing logic.**
2. **[sidecar-routing-policy.md](sidecar-routing-policy.md)** — the sidecar's
   own send-order + cooldown policy: full ring (hot appended last), the two
   feedback paths (first-skipped cooled, not primary), why `fell_back` not
   `is_fallback`. **Read after the facts, before changing routing.**
3. **[session-identity.md](session-identity.md)** — how sidecar derives session:
   `prompt_cache_key` real signal; least-loaded
   pin assignment with random tie-break on cold start.
4. **[sidecar-runbook.md](sidecar-runbook.md)** — run + verify sidecar
   (incl. `python -m unittest discover -s sidecar-2.tests -v`).
5. **[request-sanitization.md](request-sanitization.md)** — why passthrough is
   no longer 100% verbatim: Bedrock 400s on empty `thinking` blocks and on
   OpenAI `reasoning_effort`, and `sidecar-2/sanitize.py` rewrites both for
   claude/sonnet/opus models.
6. **[fast-race-endpoint.md](fast-race-endpoint.md)** — `/fast/v1` two-lane
   race: lane construction, dual pins, winner selection, per-lane feedback.
7. CORS: when started with `--cors` (Tailscale/tailnet bind),
   `proxy.py::_send_cors_headers` emits permissive
   `Access-Control-Allow-Origin: *` and answers OPTIONS preflight directly.
   The sidecar is the authoritative CORS source on the tailnet, so
   `_filter_response_headers` strips any upstream `Access-Control-*` headers
   Bifrost relays — otherwise the browser sees a duplicate allow-origin
   (`*, https://vadash.github.io`) and blocks the call.

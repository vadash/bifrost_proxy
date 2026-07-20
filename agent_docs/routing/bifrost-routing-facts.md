# Bifrost Routing Facts

Verified mechanics of the Bifrost AI Gateway's provider selection and
fallback behavior. Every fact here was confirmed by curl against a running
Bifrost v1.6.4 on `127.0.0.1:8080` (2026-07-20) and/or by reading Bifrost
source at `C:/temp/bifrost-source-read-only`.

This is the ground truth the sidecar routing logic (Bifrost-tfz) builds on.
The `provider/model` prefix + body `fallbacks` array mechanism below is
actively used by sidecar v2 (`sidecar/proxy.py`) to pin primaries and own the
fallback order.

## Setup

- 10 nvidia providers registered in the Bifrost web UI: `nvidia-1` ... `nvidia-10`.
- Each backs the same model string `z-ai/glm-5.2` (the pooled model).
- Non-pooled models (e.g. `poolside/laguna-xs-2.1`) pass through untouched.

## How Bifrost picks a provider (the auto path)

When a request arrives with a bare model string (`"model": "z-ai/glm-5.2"`,
no provider prefix):

1. The **model-catalog-resolver plugin** finds every provider that supports
   that model.
2. It **alpha-sorts** the candidate list (lexicographic, NOT numeric):
   `nvidia-1, nvidia-10, nvidia-2, nvidia-3, ..., nvidia-9`.
   (`nvidia-10` sorts second because "1" < "2" lexicographically. This is the bug
   the sidecar exists to fix.)
3. It selects the first (`nvidia-1`) as the primary.
4. It adds the remaining 9 as **catalog fallbacks** in that same alpha order.
5. On primary failure (429, 504, timeout, auth, etc.) the core walks the
   fallback slice **verbatim** — no reorder.

Source anchor: `plugins/modelcatalogresolver/main.go:140`
`ResolveProviderFromCatalog` (PreRequestHook, lines 64-118).

Internal log confirming the path:
```
[model-catalog] - No provider specified for model z-ai/glm-5.2, found 9 options...
[model-catalog] - selected: nvidia-1
[model-catalog] - Added 8 catalog fallback provider(s)...
[core] - Primary nvidia-1/z-ai/glm-5.2 failed (request_limited HTTP 429); evaluating 8 configured fallback(s)
[core] - Trying fallback 1/8: nvidia-10/z-ai/glm-5.2
```

## Forcing the initial provider: `provider/model` prefix

Sending `"model": "nvidia-4/z-ai/glm-5.2"` **forces** the initial provider to
`nvidia-4`. Confirmed for `nvidia-1`, `nvidia-4`, `nvidia-7` — the response's
`extra_fields.routing_info.provider` matched the prefix every time.

Bifrost's `ParseModelString` (`core/schemas/utils.go:96`) splits on the first
`/` only when the prefix is a **registered provider name**, so
`nvidia-4/z-ai/glm-5.2` yields `provider=nvidia-4, model=z-ai/glm-5.2` and the
upstream sees the clean model string `z-ai/glm-5.2`.

### Critical: prefix SILENCES catalog fallback

The `provider/model` prefix pins Bifrost to that single provider and **disables
the auto catalog fallback**. Verified two ways:

- **Disabled keys:** `nvidia-2/z-ai/glm-5.2` with nvidia-2 keys disabled →
  `400 "no keys found that support model: z-ai/glm-5.2"`. No fallback attempted.
- **Rate-limited:** `nvidia-1/z-ai/glm-5.2` with nvidia-1 at 1 req / 30 min →
  `429` returned to the client immediately, `routing_info: {}` empty.
  No fallback attempted. Contrast: the bare `z-ai/glm-5.2` request under the
  same conditions → 200 via `nvidia-10` (`is_fallback: true`).

There is no "prefer this provider, fall back to catalog if it fails" mode
achievable via the prefix alone.

## Restoring the safety net: body `fallbacks` array

The way to pin a primary AND keep fallback behavior is to combine the
`provider/model` prefix in `model` with an explicit `fallbacks` array of
`provider/model` **strings** in the request body:

```json
{
  "model": "nvidia-1/z-ai/glm-5.2",
  "fallbacks": ["nvidia-3/z-ai/glm-5.2", "nvidia-5/z-ai/glm-5.2"],
  "messages": [{"role": "user", "content": "..."}]
}
```

- The `fallbacks` value is an array of **strings** (`["p/model", ...]`).
  Array of `[{provider: ...}]` objects → `400 "Invalid request payload"`.
  Bifrost does OpenAI-schema validation before routing.
- Bifrost honors "Respects existing fallbacks: If you manually specify
  fallbacks, they are preserved" (docs: getbifrost.ai/features/governance/routing).
  Manual fallbacks **replace** the auto catalog list — the sidecar owns the order.
- The core's fallback loop (`core/bifrost.go` ~5020/5145) walks the caller slice
  verbatim; `parseFallbacks` (`transports/bifrost-http/handlers/inference.go:617`)
  builds `schemas.Fallback{Provider, Model}` from the body array.

### Verified result (this session)

Forced `nvidia-1/...` (rate-limited, 429) + `fallbacks: ["nvidia-3/...", "nvidia-5/..."]`
→ **200**, served by `nvidia-3` (`routing_info.provider: "nvidia-3"`,
`is_fallback: true`, `primary_provider: "nvidia-1"`). The manual fallback chain
saved the request that pinning alone would have failed.

## Getting the serving provider back: `routing_info`

Every response (non-stream and streaming) carries which provider actually served
it in `extra_fields.routing_info`:

```json
"routing_info": {
  "provider": "nvidia-3",
  "model": "z-ai/glm-5.2",
  "key": "NVIDIA_API_KEY_13",
  "is_fallback": true,
  "primary_provider": "nvidia-1",
  "primary_model": "z-ai/glm-5.2"
}
```

- `provider` / `key` — who actually served.
- `is_fallback` — true if a fallback was used (primary failed).
- `primary_provider` / `primary_model` — the originally-selected primary.
- Present on `/v1/chat/completions` and `/v1/responses`, non-stream body and
  streaming SSE (terminal `response.completed` / `response.incomplete` event).
- On a hard Bifrost error (pin fails with no fallback) `routing_info` is `{}`.

The sidecar reads this to update session pin + global cooldown state.

## What does NOT work

- **`x-bf-fallbacks` header** — no observed effect. Prefix `nvidia-1/...` with
  `x-bf-fallbacks: ["nvidia-3/...", ...]` still returned the 429 with no
  fallback. Either not honored in v1.6.4 or the wrong mechanism. Use the body
  `fallbacks` array instead.
- **Comma-separated model chain** (`"nvidia-2/...,nvidia-7/...,nvidia-3/..."`)
  — Bifrost splits on the first comma and picks nvidia-2 as primary, but
  forwards the **entire comma string** upstream as the model name → upstream 404.
  Not a valid fallback syntax.
- **Body `provider` + body `fallbacks` objects** — `400 Invalid request payload`
  (not OpenAI schema; rejected before routing).

## Bifrost source anchors (read-only copy at C:/temp/bifrost-source-read-only)

| File | Line | What |
|---|---|---|
| `plugins/modelcatalogresolver/main.go` | 140 | `ResolveProviderFromCatalog` — alpha sort, pick [0], add rest (the bug) |
| `core/schemas/utils.go` | 96 | `ParseModelString` — split on `/` if prefix is a known provider |
| `core/schemas/bifrost.go` | — | `Fallback{Provider, Model}`; `SetFallbacks`; `BifrostContextKeyFallbackIndex` |
| `transports/bifrost-http/handlers/inference.go` | 617 | `parseFallbacks` — reads `fallbacks` body array |
| `transports/bifrost-http/handlers/inference.go` | 84 | `prepareRequest` — reads `fallbacks` from body |
| `core/bifrost.go` | ~5020/5145 | for-range fallback loop walks caller slice verbatim |
| `core/bifrost.go` | ~8352 | `x-bf-session-id` stickiness (key-within-provider only; does NOT reorder providers) |

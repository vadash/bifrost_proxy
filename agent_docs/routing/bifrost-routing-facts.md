# Bifrost Routing Facts

Verified mechanics of Bifrost v1.6.4 provider selection on `127.0.0.1:8080`.
Ground truth the sidecar-2 routing logic builds on.

## Setup

- 15 nvidia providers registered: `nvidia-1` ... `nvidia-15`, all backing
  `z-ai/glm-5.2` (the pooled model).

## Auto path (bare model string)

1. The model-catalog-resolver plugin finds every provider supporting the model.
2. It **alpha-sorts lexicographically** (NOT numeric): `nvidia-1, nvidia-10,
   nvidia-2, ...`. This is the bug the sidecar exists to fix.
3. First entry becomes primary; the rest become catalog fallbacks in that
   order, walked verbatim on failure.

## `provider/model` prefix: forces provider, silences catalog fallback

`"model": "nvidia-4/z-ai/glm-5.2"` forces the primary to `nvidia-4` — but
**disables the auto catalog fallback** (a failure returns the error to the
client with `routing_info: {}`, no fallback attempted). Prefix alone gives no
"prefer X, else catalog" mode.

## Body `fallbacks` array: pin a primary AND keep fallback

```json
{
  "model": "nvidia-1/z-ai/glm-5.2",
  "fallbacks": ["nvidia-3/z-ai/glm-5.2", "nvidia-5/z-ai/glm-5.2"]
}
```

- Value MUST be an array of `"p/model"` **strings**; array of objects →
  `400 Invalid request payload`.
- Manual fallbacks **replace** the auto catalog list and are walked verbatim —
  the sidecar owns the order.

## Getting the serving provider back: `routing_info`

The serving provider arrives **in the body only**, at top-level
`extra_fields.routing_info.provider`:

- Non-stream: body top level.
- `/v1/responses` stream: terminal `response.completed` event's top-level
  `extra_fields` (NOT nested under `response.extra_fields`, which stays `{}`).
- `/v1/chat/completions` stream: every chunk carries it (chunk 0 onward).
- Hard Bifrost error, or a stream cut before its terminal event (client
  disconnect): no provider — a normal outcome, not an error.

On this build:

- **No `x-bifrost-routing-info-*` headers exist** (only `X-Bifrost-Trace-Id`);
  source HEAD has them in `transports/bifrost-http/lib/responseheaders.go`,
  but the running binary predates that.
- **`is_fallback` / `primary_provider` are never emitted**, even during a real
  fallback (verified: forced failing primary + `fallbacks` → 200 served by the
  fallback, no such fields anywhere). `served != forced primary` is the only
  fallback signal.

The sidecar reads this (`sidecar-2/routing_info.py::extract_provider`) to
update session pin + cooldown state.

## What does NOT work

- **`x-bf-fallbacks` header** — no effect; use the body `fallbacks` array.
- **Comma-separated model chain** — primary picked from first entry, but the
  entire comma string is forwarded upstream as the model name → upstream 404.
- **Body `provider` + `fallbacks` objects** — `400 Invalid request payload`.

## Source anchors (read-only copy at C:/tmp/bifrost)

| File | Line | What |
|---|---|---|
| `plugins/modelcatalogresolver/main.go` | 140 | `ResolveProviderFromCatalog` — alpha sort, pick [0] (the bug) |
| `core/schemas/utils.go` | 96 | `ParseModelString` — split on `/` if prefix is a known provider |
| `transports/bifrost-http/handlers/inference.go` | 617 | `parseFallbacks` — reads `fallbacks` body array |
| `core/bifrost.go` | ~5020/5145 | fallback loop walks caller slice verbatim |

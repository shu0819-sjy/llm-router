# Architecture — llm-router 0.3.x

Short module-boundary map for the single-node OpenAI-compatible gateway.
Operational deploy notes live in [`OPERATIONS.md`](./OPERATIONS.md).

## Request path (chat)

```text
Client
  │  Authorization: Bearer <gateway key>
  ▼
api/errors  RequestBoundaryMiddleware (body / message / tool / concurrency limits)
  ▼
auth        gateway API-key check (env-managed + panel-managed keys)
  ▼
routing     KeyRouter.resolve(model, require_capabilities=…)
  ▼
failover    Orchestrator (timeout budget, prefix+capability filter, circuit breaker)
  ▼
providers   DeepSeek / OpenAI / Anthropic / Qwen adapters
  ▼
streaming   SSE remap + disconnect → aclose nested generators → usage finalize
  ▼
db/ledger   SQLite usage / cost rows (WAL)
```

`GET /v1/models` shares the same auth surface and reads price rows from storage.

## Module boundaries

| Area | Package | Responsibility | Non-goals |
|------|---------|----------------|-----------|
| **Providers** | `app/providers/` | Upstream HTTP adapters (`chat` / `chat_stream`), model-prefix + capability declarations, registry wiring from settings | Multi-cloud SDKs; mapping Anthropic tools (intentionally unsupported — failover instead) |
| **Routing** | `app/routing/` | Map gateway API keys → optional forced provider; resolve candidates by model prefix and required capabilities (`tools` / `tool_choice` / `response_format`) | Weighted load balancing (`providers.weight` reserved/unused) |
| **Failover** | `app/failover/` | Ordered attempts within a timeout budget; classify retryable errors; per-provider circuit breaker | Cross-process shared breaker state |
| **Storage** | `app/db/` | `StorageBundle` (SQLite default + in-memory test double); schema/migrations; price history; usage ledger with busy retry | Redis/PostgreSQL production backends; multi-writer replicas on one file |
| **Streaming** | `app/streaming/` + chat handler | OpenAI-shaped SSE; mid-stream error sanitization; client disconnect accounting via nested-generator `aclose` | Full OpenAI API surface beyond Chat Completions |
| **Limits / panel** | `app/ratelimit/`, `app/panel/`, `app/api/errors.py` | Per-key token bucket; admin panel CRUD; SSRF policy on provider base URLs; diagnostics auth for `/metrics` | Distributed rate limits across replicas |

## Capability-aware tool routing

Chat requests that carry `tools` / `tool_choice` / `response_format` pass
`require_capabilities` into `KeyRouter.resolve`. Only providers that declare the
matching capabilities remain candidates; if none remain, the API returns a
structured `400 unsupported_parameter` naming the fields. Anthropic-bound
requests with tool fields fail over to a tool-capable provider rather than
hitting the Anthropic adapter.

## Process model

- One uvicorn process ↔ one SQLite file (WAL + `busy_timeout`).
- Rate limiter and circuit breakers are **in-memory** (process-local).
- Upstream secrets come from environment variables; panel keys are hashed in SQLite.

## Related docs

- Compatibility scope and public claims: root [`README.md`](../README.md)
- Single-node operations checklist: [`OPERATIONS.md`](./OPERATIONS.md)
- Contributing / vulnerability reporting: [`../CONTRIBUTING.md`](../CONTRIBUTING.md), [`../SECURITY.md`](../SECURITY.md)

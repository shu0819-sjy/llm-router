# llm-router v0.1 — Requirements & Acceptance

**Workspace root (mandatory):** `D:\deepseek workspace\llm-router`  
**Never** place artifacts under `C:\` or any path outside this workspace.  
**Target GitHub:** `shu0819-sjy/llm-router` (push after auth works; no secrets in git).  
**Style:** OpenAI-compatible asyncio FastAPI gateway (one-api / new-api class).

---

## 1. Goals (v0.1)

Build a production-shaped **OpenAI-compatible LLM gateway** that:

1. Accepts OpenAI-style `POST /v1/chat/completions` (sync + SSE stream).
2. Routes by **API key** and/or **model prefix** to multiple upstream providers.
3. Fails over in **< 500 ms** decision budget (timeout → next healthy upstream).
4. Enforces **token-bucket** rate limits per API key.
5. Persists **token/cost usage** in SQLite.
6. Exposes `/health`, optional Prometheus metrics, and a **minimal Web panel**.
7. Ships with **Docker one-command**, **pytest ≥ 80%**, and a polished README.

---

## 2. Explicit Non-Goals (v0.1)

| Non-goal | Rationale |
|----------|-----------|
| Billing / invoice UI | Cost ledger is internal; no Stripe/payment flows |
| Multi-tenant RBAC beyond API keys | Auth = bearer API keys only; no roles/orgs/SSO |
| Any `C:\` path usage | All code, data, Docker volumes under `D:\deepseek workspace\llm-router` |
| Provider SDKs as hard runtime deps for all vendors | Prefer httpx + thin adapters; optional official SDKs only where needed |
| Full admin IAM / audit log product | Minimal key CRUD + usage views only |
| Kubernetes / Helm | Docker Compose only for v0.1 |
| Model fine-tune / embedding / image / audio routes | Chat completions only |
| Persistent circuit-breaker state across restarts | In-memory breaker is acceptable for v0.1 |

---

## 3. Project Layout

```text
D:\deepseek workspace\llm-router\
├── README.md                 # Architecture (Mermaid), features, config, benchmarks, interview points, one-click Docker
├── REQUIREMENTS.md           # This document
├── LICENSE                   # MIT (or Apache-2.0)
├── RELEASE_NOTES_v0.1.md
├── pyproject.toml            # package metadata + pytest/coverage config
├── requirements.txt          # pinned runtime deps
├── requirements-dev.txt      # pytest, coverage, httpx, ruff (optional)
├── .env.example              # documented env knobs (no secrets)
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── app/
│   ├── __init__.py
│   ├── main.py               # FastAPI app factory + lifespan
│   ├── config.py             # pydantic-settings from env
│   ├── models.py             # Pydantic request/response + DB ORM models
│   ├── auth.py               # API-key extraction & validation
│   ├── rate_limit.py         # token-bucket limiter
│   ├── router.py             # key/model routing + failover orchestration
│   ├── circuit_breaker.py    # closed/open/half-open state machine
│   ├── sse.py                # SSE passthrough + backpressure helpers
│   ├── metrics.py            # /health + optional Prometheus
│   ├── db.py                 # SQLite engine, sessions, migrations-lite
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py           # Provider Protocol / ABC
│   │   ├── openai_compat.py  # GPT / DeepSeek / Qwen OpenAI-compatible
│   │   ├── anthropic.py      # Claude Messages → OpenAI shape adapter
│   │   └── registry.py       # provider id → instance
│   ├── api/
│   │   ├── __init__.py
│   │   ├── chat.py           # /v1/chat/completions
│   │   ├── health.py         # /health, /metrics
│   │   └── panel.py          # Web panel JSON + static HTML
│   └── web/                  # static panel assets (HTML/JS/CSS)
│       ├── index.html
│       ├── app.js
│       └── styles.css
├── tests/
│   ├── conftest.py
│   ├── test_chat.py
│   ├── test_routing.py
│   ├── test_failover.py
│   ├── test_rate_limit.py
│   ├── test_sse.py
│   ├── test_db_usage.py
│   ├── test_health.py
│   └── test_panel.py
├── scripts/
│   ├── bench_failover.py     # measure failover decision latency
│   └── seed_demo.py          # demo keys/providers for local panel
└── data/                     # runtime SQLite (gitignored); mounted in Docker
    └── .gitkeep
```

---

## 4. Configuration (Environment)

All runtime config via env / `.env` (see `.env.example`). No hardcoded secrets.

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_ROUTER_HOST` | `0.0.0.0` | Bind host |
| `LLM_ROUTER_PORT` | `8000` | Bind port |
| `LLM_ROUTER_DB_PATH` | `./data/llm_router.db` | SQLite path (relative to workspace) |
| `LLM_ROUTER_ADMIN_TOKEN` | *(required in prod)* | Bearer token for panel mutating APIs |
| `LLM_ROUTER_DEFAULT_TIMEOUT_MS` | `500` | Per-attempt upstream timeout (failover budget) |
| `LLM_ROUTER_FAILOVER_BUDGET_MS` | `500` | Max wall time before returning 504 / last error |
| `LLM_ROUTER_CB_FAILURE_THRESHOLD` | `3` | Failures to open circuit |
| `LLM_ROUTER_CB_RECOVERY_TIMEOUT_S` | `30` | Open → half-open wait |
| `LLM_ROUTER_CB_HALF_OPEN_MAX` | `1` | Trial requests in half-open |
| `LLM_ROUTER_RATE_CAPACITY` | `60` | Token-bucket capacity (requests) |
| `LLM_ROUTER_RATE_REFILL_PER_S` | `1.0` | Tokens refilled per second |
| `LLM_ROUTER_RATE_COST_PER_REQ` | `1` | Tokens consumed per request |
| `LLM_ROUTER_ENABLE_PROMETHEUS` | `false` | Expose `/metrics` |
| `LLM_ROUTER_LOG_LEVEL` | `INFO` | Logging level |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | — | Upstream OpenAI-compatible (GPT) |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | — | Claude |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek |
| `QWEN_API_KEY` / `QWEN_BASE_URL` | — | Qwen (DashScope OpenAI-compat) |
| `LLM_ROUTER_PROVIDER_ORDER` | `deepseek,openai,anthropic,qwen` | Default failover order |

Provider credentials may also be stored via panel (SQLite) and override env for that provider id.

---

## 5. Provider Interface

```python
from typing import Protocol, AsyncIterator, Any
from pydantic import BaseModel

class ChatMessage(BaseModel):
    role: str
    content: str | list[Any]

class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    # passthrough extras allowed
    class Config:
        extra = "allow"

class Provider(Protocol):
    id: str                          # e..g. "openai", "anthropic", "deepseek", "qwen"
    supported_prefixes: list[str]    # e.g. ["gpt-", "o1-"], ["claude-"], ["deepseek-"], ["qwen"]

    async def health(self) -> bool: ...
    async def chat(self, req: ChatRequest, *, timeout_ms: int) -> dict: ...
    async def chat_stream(
        self, req: ChatRequest, *, timeout_ms: int
    ) -> AsyncIterator[bytes]: ...   # raw SSE chunk bytes (OpenAI shape)
```

**Minimum providers (≥ 3 required; v0.1 ships 4):**

| Provider id | Upstream | Model prefixes (examples) | Adapter notes |
|-------------|----------|---------------------------|---------------|
| `openai` | OpenAI Chat Completions | `gpt-`, `o1-`, `o3-` | Native OpenAI JSON/SSE |
| `anthropic` | Anthropic Messages API | `claude-` | Map Messages ↔ OpenAI chat; stream → OpenAI SSE |
| `deepseek` | DeepSeek OpenAI-compat | `deepseek-` | Native OpenAI JSON/SSE |
| `qwen` | DashScope OpenAI-compat | `qwen`, `qwen-` | Native OpenAI JSON/SSE |

---

## 6. Routing

### 6.1 Key-based routing

1. Client sends `Authorization: Bearer <api_key>`.
2. Gateway looks up key in SQLite `api_keys`.
3. Key may bind:
   - `provider_id` (force single provider), and/or
   - `model_allowlist` (optional CSV/glob), and/or
   - `rate_capacity` / `rate_refill_per_s` overrides.
4. Missing/invalid key → `401 Unauthorized`.

### 6.2 Model-prefix routing

If key does not force a provider:

1. Match `request.model` against provider `supported_prefixes` (longest prefix wins).
2. Else use `LLM_ROUTER_PROVIDER_ORDER` and try providers that claim the model or accept `*` passthrough.
3. Build **candidate list** = primary + failover chain (excluding open circuits).

### 6.3 Failover design (SLO: decision + first retry < 500 ms)

**Budget:** `LLM_ROUTER_FAILOVER_BUDGET_MS` (default **500**).  
**Per-attempt timeout:** `LLM_ROUTER_DEFAULT_TIMEOUT_MS` (default **500**, may be smaller when multiple candidates remain).

**Algorithm:**

```text
remaining = failover_budget_ms
for candidate in candidates:
  if circuit[candidate].state == OPEN and not ready_for_half_open:
    skip
  attempt_timeout = min(per_attempt_timeout, remaining)
  try:
    result = await provider.chat(..., timeout_ms=attempt_timeout)
    circuit.record_success(candidate)
    return result
  except (TimeoutError, Upstream5xx, ConnectionError) as e:
    circuit.record_failure(candidate)
    remaining -= elapsed
    if remaining <= 0: break
    continue
return 504 Gateway Timeout (or last upstream error body if budget exhausted after ≥1 try)
```

**Circuit breaker (per provider):**

| State | Behavior |
|-------|----------|
| **Closed** | Normal; count consecutive failures |
| **Open** | Skip provider until `recovery_timeout_s` elapsed |
| **Half-open** | Allow up to `half_open_max` trial requests; success → Closed; failure → Open |

Defaults: `failure_threshold=3`, `recovery_timeout_s=30`, `half_open_max=1`.

**Streaming failover:** Only switch candidates **before** the first SSE byte is flushed to the client. After first byte, errors are propagated as SSE `error` / connection close (no mid-stream provider hop in v0.1).

---

## 7. Token-Bucket Rate Limit

**Algorithm:** classic token bucket per API key (in-memory; process-local for v0.1).

| Parameter | Env / key override | Meaning |
|-----------|--------------------|---------|
| `capacity` | `LLM_ROUTER_RATE_CAPACITY` (60) | Max burst tokens |
| `refill_per_s` | `LLM_ROUTER_RATE_REFILL_PER_S` (1.0) | Continuous refill rate |
| `cost_per_req` | `LLM_ROUTER_RATE_COST_PER_REQ` (1) | Tokens deducted per accepted request |

**Behavior:**

1. On each request after auth: `tokens = min(capacity, tokens + refill * Δt)`.
2. If `tokens >= cost_per_req`: deduct and proceed.
3. Else: `429 Too Many Requests` with `Retry-After` (seconds until one token available).
4. Headers (optional but preferred): `X-RateLimit-Limit`, `X-RateLimit-Remaining`.

---

## 8. SQLite Token / Cost Ledger

**Engine:** SQLAlchemy 2.x async (or aiosqlite) → file at `LLM_ROUTER_DB_PATH`.

### 8.1 Tables

#### `api_keys`

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | |
| `key_hash` | TEXT UNIQUE | SHA-256 of raw key (store hash only) |
| `name` | TEXT | Human label |
| `provider_id` | TEXT NULL | Optional forced provider |
| `model_allowlist` | TEXT NULL | CSV / JSON list |
| `rate_capacity` | REAL NULL | Override |
| `rate_refill_per_s` | REAL NULL | Override |
| `is_active` | INTEGER | 0/1 |
| `created_at` | TEXT | ISO-8601 UTC |

#### `providers`

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | `openai`, `anthropic`, … |
| `base_url` | TEXT | |
| `api_key_enc` | TEXT NULL | Optional stored secret (env preferred); never log |
| `enabled` | INTEGER | 0/1 |
| `weight` | INTEGER | Optional routing weight (default 100) |
| `updated_at` | TEXT | |

#### `usage_events`

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | |
| `api_key_id` | INTEGER FK | |
| `provider_id` | TEXT | Actual upstream used |
| `model` | TEXT | |
| `prompt_tokens` | INTEGER | |
| `completion_tokens` | INTEGER | |
| `total_tokens` | INTEGER | |
| `cost_usd` | REAL | Estimated from price table |
| `latency_ms` | INTEGER | |
| `status` | TEXT | `ok` / `error` / `failover` |
| `request_id` | TEXT | |
| `created_at` | TEXT | ISO-8601 UTC |

#### `model_prices` (seed data)

| Column | Type | Notes |
|--------|------|-------|
| `model` | TEXT PK | |
| `input_per_1m_usd` | REAL | |
| `output_per_1m_usd` | REAL | |

**Cost formula:**  
`cost_usd = prompt_tokens/1e6 * input_per_1m_usd + completion_tokens/1e6 * output_per_1m_usd`  
Unknown model → `cost_usd = 0` (still record tokens).

---

## 9. API Contracts (OpenAPI-like)

Base URL: `http://<host>:<port>`  
Content-Type: `application/json` unless SSE.

### 9.1 `POST /v1/chat/completions`

**Auth:** `Authorization: Bearer <api_key>`

**Request body (OpenAI-compatible):**

```json
{
  "model": "deepseek-chat",
  "messages": [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hello"}
  ],
  "stream": false,
  "temperature": 0.7,
  "max_tokens": 1024
}
```

**Success 200 (non-stream):**

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1710000000,
  "model": "deepseek-chat",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "..."},
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 20,
    "total_tokens": 32
  }
}
```

**Success 200 (stream):** `Content-Type: text/event-stream`

OpenAI-compatible SSE events:

```text
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1710000000,"model":"deepseek-chat","choices":[{"index":0,"delta":{"role":"assistant","content":"He"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1710000000,"model":"deepseek-chat","choices":[{"index":0,"delta":{"content":"llo"},"finish_reason":null}]}

data: [DONE]
```

**SSE semantics & backpressure:**

- Passthrough upstream chunks when already OpenAI-shaped; Anthropic adapter remaps to this shape.
- Gateway must **not** unbounded-buffer: use async iteration; apply backpressure by awaiting client send (`await response.write` / Starlette StreamingResponse).
- If client disconnects: cancel upstream task.
- Error before first byte: JSON error response (4xx/5xx). After first byte: emit optional `data: {"error":{...}}` then close; or hard-close.

**Error responses:**

| Status | When |
|--------|------|
| 401 | Missing/invalid API key |
| 403 | Model not in key allowlist |
| 429 | Rate limit exceeded |
| 502 | All upstreams failed with bad gateway |
| 504 | Failover budget exhausted / upstream timeouts |
| 422 | Validation error (FastAPI) |

### 9.2 `GET /health`

**Auth:** none

**200:**

```json
{
  "status": "ok",
  "version": "0.1.0",
  "uptime_s": 123.4,
  "providers": {
    "openai": {"enabled": true, "circuit": "closed", "healthy": true},
    "anthropic": {"enabled": true, "circuit": "open", "healthy": false},
    "deepseek": {"enabled": true, "circuit": "closed", "healthy": true},
    "qwen": {"enabled": false, "circuit": "closed", "healthy": false}
  },
  "db": "ok"
}
```

Degraded: still `200` with `"status": "degraded"` if any enabled provider is open/unhealthy; process down → connection failure (compose healthcheck fails).

### 9.3 `GET /metrics` (optional)

When `LLM_ROUTER_ENABLE_PROMETHEUS=true`: Prometheus text exposition.

Suggested metrics:

- `llm_router_requests_total{provider,status}`
- `llm_router_failover_total{from_provider,to_provider}`
- `llm_router_upstream_latency_ms_bucket{provider}`
- `llm_router_rate_limited_total`
- `llm_router_tokens_total{direction="prompt|completion"}`
- `llm_router_circuit_state{provider}` (0 closed / 1 half-open / 2 open)

### 9.4 Web Panel Routes

Static UI: `GET /panel/` → `app/web/index.html`  
JSON APIs (mutate require `Authorization: Bearer <LLM_ROUTER_ADMIN_TOKEN>`):

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/panel/api/keys` | admin | List keys (hash prefix + name; never raw key after create) |
| `POST` | `/panel/api/keys` | admin | Create key; response returns raw key **once** |
| `DELETE` | `/panel/api/keys/{id}` | admin | Deactivate/delete |
| `GET` | `/panel/api/providers` | admin | List providers + circuit + enabled |
| `PATCH` | `/panel/api/providers/{id}` | admin | Enable/disable, update base_url |
| `GET` | `/panel/api/usage?from=&to=&api_key_id=` | admin | Aggregated tokens + cost |
| `GET` | `/panel/api/usage/recent?limit=50` | admin | Recent `usage_events` |

**Panel MVP UI sections:** Keys · Providers · Usage (simple tables + totals). No charts library required (vanilla JS OK).

---

## 10. Docker Deliverables

**One-command (from workspace):**

```bash
docker compose -f "D:\deepseek workspace\llm-router\docker-compose.yml" up --build -d
```

**Artifacts:**

- `Dockerfile` — Python 3.11-slim, uvicorn, non-root user preferred
- `docker-compose.yml` — service `llm-router`, port `8000:8000`, volume `./data:/app/data`, `env_file: .env`
- Healthcheck: `curl -f http://localhost:8000/health || exit 1`
- `.env.example` copied to `.env` by operator (documented in README)

**Constraint:** image build context = `D:\deepseek workspace\llm-router` only.

---

## 11. Test Strategy

| Area | Tests | Notes |
|------|-------|-------|
| Chat happy path | Mock provider → 200 shape | |
| Key auth | 401/403 | |
| Model-prefix routing | Prefix → correct provider | |
| Key-forced provider | Override prefix | |
| Failover | Primary timeout → secondary success; assert wall < 500 ms in unit with fake clock / mocked sleep | |
| Circuit breaker | Open after N failures; half-open recovery | |
| Rate limit | Burst then 429 + Retry-After | |
| SSE | Stream chunks + `[DONE]`; client disconnect cancel | |
| SQLite usage | Tokens + cost row written | |
| Health | JSON schema + provider map | |
| Panel APIs | CRUD keys (admin token) | |

**Coverage bar:** `pytest --cov=app --cov-report=term-missing` → **line coverage ≥ 80%**.  
**Benchmark script:** `scripts/bench_failover.py` records p50/p95 failover decision latency; README cites results (target: failover decision **< 500 ms**).

---

## 12. README Completion Bar

README **must** include:

1. One-liner + badges (optional).
2. **Architecture** Mermaid diagram (client → gateway → providers; rate limit + CB + SQLite).
3. Feature list aligned with this REQUIREMENTS.md.
4. Config table (env).
5. **Benchmark** subsection (failover latency numbers from script).
6. **One-click Docker** instructions.
7. Local dev (`uvicorn app.main:app --reload`).
8. **Interview talking points** (see §13).
9. Link to `REQUIREMENTS.md` and `RELEASE_NOTES_v0.1.md`.

---

## 13. Interview Talking Points (Outline)

README should expand these into short bullets:

1. **Why FastAPI + asyncio** — high concurrency for SSE; typed validation.
2. **Failover SLO design** — budgeted timeout, not infinite retries; circuit breaker prevents retry storms.
3. **Token bucket vs fixed window** — smooth burst control; per-key overrides.
4. **Cost ledger** — hash-stored keys; usage_events for chargeback without a billing product.
5. **SSE backpressure** — async generators; cancel on disconnect; no mid-stream failover.
6. **Provider Protocol** — OpenAI-compat passthrough vs Anthropic adapter.
7. **Operational MVP** — `/health` + optional Prometheus + Docker healthcheck.
8. **Trade-offs deferred** — in-memory rate limit/CB (multi-replica needs Redis later); no full RBAC.

---

## 14. GitHub Publish Checklist (v0.1)

- [ ] All artifacts under `D:\deepseek workspace\llm-router`
- [ ] `REQUIREMENTS.md`, `README.md`, `LICENSE`, `RELEASE_NOTES_v0.1.md`
- [ ] `Dockerfile`, `docker-compose.yml`, `.env.example`, `.gitignore`
- [ ] `pyproject.toml` / `requirements.txt` (+ dev)
- [ ] pytest ≥ 80% locally documented
- [ ] No secrets committed (keys only in `.env`, gitignored)
- [ ] Remote: `https://github.com/shu0819-sjy/llm-router.git` (create if missing)
- [ ] Tag `v0.1.0` + release notes after push

---

## 15. Acceptance Criteria (Contract Traceability)

| # | Criterion | Status target |
|---|-----------|---------------|
| A1 | `REQUIREMENTS.md` exists under `llm-router` with OpenAPI-like contracts for `/v1/chat/completions`, `/health`, and panel routes | Required for t1 |
| A2 | Documents ≥3 providers (Claude/GPT/Qwen/DeepSeek), key-based routing, failover <500ms with timeout + circuit-breaker + recovery, token-bucket limiter, SQLite token/cost ledger fields, SSE passthrough + backpressure | Required for t1 |
| A3 | States completion bar: pytest ≥80%, README architecture + benchmark + one-click Docker, Docker artifacts, push to GitHub `shu0819-sjy` | Required for t1 |
| A4 | Explicit non-goals: no billing UI, no multi-tenant RBAC beyond API keys, no `C:` path usage | Required for t1 |

**Downstream implementation (t4–t9) is complete when** code + tests + Docker + README satisfy §§3–14 and A1–A4 behaviors are demonstrable.

---

## 16. Version

| Field | Value |
|-------|-------|
| Product | llm-router |
| Version | 0.1.0 |
| Requirements rev | 2026-03-22-r1 |
| Owner role | docs-release (requirements) → backend-engineer → qa-verifier → docs-release (review/release) |

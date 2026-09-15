# llm-router 0.1.0

First public release of a small OpenAI-compatible LLM gateway.

## What's included

- Multi-provider routing: OpenAI, Anthropic (Claude), DeepSeek, Qwen
- API-key and model-prefix routing with budgeted failover + circuit breaker
- Token-bucket rate limiting
- SQLite usage / estimated cost ledger
- SSE streaming
- `/health`, optional Prometheus `/metrics`
- Minimal admin panel
- Docker Compose support
- Test suite and failover micro-benchmark (fake upstreams)

## Install

```bash
git clone https://github.com/shu0819-sjy/llm-router.git
cd llm-router
cp .env.example .env
docker compose up --build -d
```

## Known limits

- In-memory rate limiter and circuit breakers
- No billing UI or full RBAC
- Stream token accounting is still a stub

# Changelog

## [0.2.0] — 2026-03-22

### Security

- Reject empty/placeholder admin tokens outside `LLM_ROUTER_ENV=development|test`
- Constant-time admin token comparison
- Rate-limit bucket identity uses digests (optional `LLM_ROUTER_RATE_LIMIT_SECRET`), not raw API keys

### Added

- Health split: `GET /health/live`, `/health/ready`, `/health/providers` (legacy `/health` retained)
- Request ID middleware (`X-Request-Id`) persisted on usage rows
- Admin mutation audit events
- OpenAI-compatible error envelopes and `GET /v1/models`
- Stream usage accounting when upstream SSE emits a usage-bearing chunk (`actual`); otherwise `unavailable`
- Price-version snapshots for reproducible historical cost estimates
- Storage ports with SQLite default + in-memory adapter for tests
- GitHub Actions CI (Python 3.11/3.12, ruff, mypy informational, pytest coverage, Docker build + `/health` smoke)
- `SECURITY.md`, `CONTRIBUTING.md`, issue/PR templates, client examples, public-release audit script
- Opt-in integration tests gated by `LLM_ROUTER_RUN_INTEGRATION=1`

### Changed

- Pinned direct dependencies for reproducible Docker/local installs
- Dockerfile / Compose image tags for 0.2.0

## [0.1.0] — 2026-03-22

### Added

- OpenAI-compatible `POST /v1/chat/completions` (JSON + SSE)
- Providers: OpenAI, Anthropic Claude adapter, DeepSeek, Qwen
- Key + model-prefix routing with budgeted failover and circuit breaker
- Token-bucket rate limiter (`429` / `Retry-After`)
- SQLite schema for API keys, providers, usage events, and model prices
- Admin panel at `/panel/`
- `/health` and optional Prometheus `/metrics`
- Docker image + Compose healthcheck
- Failover micro-benchmark script

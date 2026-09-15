# Changelog

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

# Changelog

All notable changes to **llm-router** are documented here.

## [0.1.0] — 2026-03-22

### Added

- OpenAI-compatible `POST /v1/chat/completions` (JSON + SSE)
- Providers: OpenAI (GPT), Anthropic Claude adapter, DeepSeek, Qwen
- Key + model-prefix routing with budgeted failover and circuit breaker
- Token-bucket rate limiter with `429` / `Retry-After`
- SQLite schema: `api_keys`, `providers`, `usage_events`, `model_prices`
- Panel key auth via SHA-256 → `api_keys.key_hash` (survives restart)
- SSE pre-first-byte failover + async backpressure
- `GET /health`, optional `GET /metrics`
- Web panel MVP at `/panel/`
- Docker image + Compose healthcheck
- `scripts/bench_failover.py`
- Docs: `README.md`, `REQUIREMENTS.md`, `RELEASE_NOTES_v0.1.md`, MIT `LICENSE`

### Notes

- Coverage bar ≥80% enforced in `pyproject.toml` (`fail_under = 80`)

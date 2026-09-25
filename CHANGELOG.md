# Changelog

## [0.3.3] — 2026-09-25

### Fixed

- Client disconnect mid-SSE now `aclose`s nested async generators so finalize / usage accounting always runs and the process cannot hang on Python 3.12
- CI: add `ruff format --check` hard gate; reformat `app` / `tests` / `scripts` to match

### Added

- Tag-driven GitHub Release workflow (`.github/workflows/release.yml`)

## [0.3.2] — 2026-09-24

Tool-call fallback: capability-aware routing for requests carrying `tools` / `tool_choice` / `response_format`.

### Added

- Tool-call fallback: chat requests carrying `tools` / `tool_choice` / `response_format` resolve only providers that declare the matching capabilities; failover continues between tool-capable candidates within the same timeout budget
- Structured `400` (`unsupported_parameter`) that names the unsupported fields when no tool-capable provider serves the model (auto-routed or key-forced) — replaces the Anthropic-only rejection path

### Changed

- `KeyRouter.resolve` is now called from `/v1/chat/completions` with `require_capabilities` derived from the request body (the routing-level capability filter existed since 0.3.0 but was not wired for tool fields)
- Anthropic-routed requests carrying tool fields now fail over to a tool-capable provider instead of reaching Anthropic (the adapter still does not map these fields)

### Fixed

- Routing candidates for tool requests can no longer include providers that cannot serve them (previously guarded by a provider-id allowlist in the chat handler)

## [0.3.1] — 2026-09-16

Industrial single-node follow-up on the 0.3.x line. First-/second-generation release artifacts are retired from the public product surface.

### Fixed

- `/v1/models` authorization-aware listing aligned with migration smoke (seed model must be provider-prefix compatible)
- CI job/step timeouts so hung unit suites cannot run for hours

### Changed

- Default upstream timeout / failover budget: `30000` / `60000` ms (was `500` / `500`)
- Panel UI version badge → v0.3.1; SECURITY supported line → 0.3.x only
- Docs: single-node operations guide; gen1/gen2 release notes and v0.2 publish checklist removed from the active tree
- `providers.weight` / `api_key_enc` documented as reserved/unused this release

### Added

- Queryable model price history (from tip): `model_price_history` + point-in-time `get_price(at=...)`
- httpx connect/read/write/pool limits on Claude and OpenAI-compatible providers

## [0.3.0] — 2026-09-16

Hardening release on top of v0.2.0. Public API surface unchanged: OpenAI compatibility remains Chat Completions (JSON + SSE) and the Models list only.

### Security

- Production admin-token quality policy (minimum length / reject weak placeholders outside development)
- Request body, message/tool-count, and concurrent chat limits with OpenAI-shaped `413` / `400` / `429` envelopes
- Provider base-URL SSRF controls (scheme/host validation; private targets blocked outside explicit development override)
- Diagnostics/metrics access policy (provider inventory and `/metrics` gated per configuration)
- Client error sanitization: SSE mid-stream errors emit a fixed public message; `failover_exhausted` responses omit raw attempt payloads; messages are bounded/redacted before leaving the process

### Fixed

- Failover candidates restricted to providers whose model prefixes (and capabilities) match the request — unrelated providers are no longer appended
- Forced-provider keys requesting an unsupported model return router-level `400` (`forced_provider_model_mismatch`); unknown models return `400` (`model_not_supported`)
- Environment-managed API keys (`LLM_ROUTER_API_KEYS`) carry `source=env` and are deactivated when removed; panel-managed keys are preserved across restarts
- Streaming usage rows distinguish `ok` (after `[DONE]`), `partial`, `upstream_error`, and `client_disconnected`, with measured TTFB (`ttfb_ms`) and total latency (no hardcoded `latency_ms=0`)
- SQLite uses WAL + `busy_timeout` + `synchronous=NORMAL`; usage ledger writes retry on lock/busy
- Mid-stream disconnect usage accounting survives response cancellation via background persistence

### Added

- `StorageBundle` dependency injection (SQLite default; in-memory test double; no Redis/PostgreSQL production backend claimed)
- Additive `usage_events.ttfb_ms` column (schema + migration)
- Opt-in live provider integration matrix (`tests/integration/`): provider × {sync, SSE} × {plain, tools}, double-gated off by default
- `scripts/restart_migration_smoke.py` Docker restart/migration smoke on a clean temporary volume (packaged-defaults smoke, not production validation)
- CI: mypy as a hard gate; integration-lane skip assertion; Docker restart/migration smoke job

### Changed

- CI type check is a **hard gate** (`mypy app`); residual `disable_error_code` list is documented and itemized
- Docker base image digest-pinned; direct dependency pins documented in `requirements.txt`
- Public compatibility claims narrowed in README / release notes (explicit unsupported OpenAI endpoints and provider capability caveats)

### Compatibility (docs)

- Authoritative scope: `POST /v1/chat/completions` (JSON + SSE) and `GET /v1/models` only
- Explicitly unsupported: legacy completions, embeddings, images, audio, files, batches, fine-tuning
- Tools / structured-output forwarded to OpenAI-compatible upstreams only; Anthropic-routed tool fields are rejected with `400`

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

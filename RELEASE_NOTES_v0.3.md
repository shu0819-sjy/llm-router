# llm-router v0.3.0

Hardening release on top of v0.2.0. Focus: routing correctness, key lifecycle, stream accounting, SQLite reliability, request/SSRF boundaries, client-error hygiene, CI reproducibility, and narrowed public compatibility claims.

**No production-scale validation claim** — Docker/CI smokes exercise packaged defaults on clean temporary volumes and fake upstreams.

## Compatibility

- **Python:** 3.11+ (CI covers 3.11 and 3.12)
- **OpenAI-compatible surface (authoritative):**
  - `POST /v1/chat/completions` — JSON and SSE (`stream=true`)
  - `GET /v1/models`
- **Not supported:** `/v1/completions`, embeddings, images, audio, files, batches, fine-tuning; OpenAI org/project header semantics
- **Tools / structured output:** forwarded as-is to OpenAI-compatible upstreams (OpenAI, DeepSeek, Qwen). Anthropic-routed requests carrying `tools` / `tool_choice` / `response_format` are rejected with `400`
- **Auth / headers:** `Authorization: Bearer <api_key>` unchanged; additive `X-Request-Id`
- **Health:** legacy `GET /health` retained; `/health/live`, `/health/ready`, `/health/providers` unchanged in shape

## Migrations

Additive SQLite migrations applied on connect (safe for v0.2.0 databases):

- `api_keys.source` — distinguishes environment-managed vs panel-managed keys
- `usage_events.ttfb_ms` — stream time-to-first-byte (INTEGER, default 0)

Existing rows are preserved. WAL / `busy_timeout` / `synchronous=NORMAL` are applied at connect.

Upgrade steps:

1. Set a strong unique `LLM_ROUTER_ADMIN_TOKEN` (required outside `development` / `test`).
2. Review new optional limits / diagnostics / SSRF knobs in `.env.example`.
3. Deploy; first boot migrates the database additively.
4. Optionally run `python scripts/restart_migration_smoke.py --build` against a disposable volume.

## Security

- Stronger production admin-token policy
- Request body / message / tool / concurrency boundaries (`413` / `400` / `429` OpenAI-shaped envelopes)
- Provider URL SSRF validation (panel PATCH and settings)
- Diagnostics/metrics access policy
- Client-facing errors: SSE mid-stream failures use a fixed public message; failover exhaustion omits raw attempt lists; messages are sanitized/bounded before return

See [SECURITY.md](./SECURITY.md) for vulnerability reporting.

## Testing

- Default: `pytest --cov=app` (unit only; coverage gate ≥80%; integration marker deselected)
- Lint (hard gate): `ruff check app tests scripts`
- Types (hard gate): `mypy app`
- Public hygiene: `python scripts/audit_public_release.py`
- Docker restart/migration smoke: `python scripts/restart_migration_smoke.py --build`
- Opt-in live providers: `LLM_ROUTER_RUN_INTEGRATION=1` plus credentials, then `pytest -m integration`

## Limitations

- Rate limiter and circuit breakers remain in-memory (not shared across replicas)
- SQLite is single-node (WAL enabled; no multi-replica coordination)
- No Redis/PostgreSQL production backend (storage ports are seams; in-memory is a test double)
- No billing / multi-tenant RBAC beyond API keys
- No Anthropic tools / structured-output mapping in this release
- Streaming usage: `accounting_status=actual` when upstream SSE emits usage; otherwise `unavailable` (cost is not invented)
- Packaged-defaults Docker/CI smokes are not a production validation claim

## Highlights vs v0.2.0

- Capability-filtered failover candidates and clear `400` mismatch / unsupported-model errors
- Environment key source/revocation semantics that survive restart
- Truthful stream status + TTFB/total latency accounting
- SQLite WAL/busy-timeout + ledger write retry; `StorageBundle` DI
- Request/SSRF/diagnostics boundaries and sanitized client errors
- Hard mypy gate, digest-pinned base image, restart/migration smoke, opt-in provider matrix

See [CHANGELOG.md](./CHANGELOG.md) for the full list. The active product line is **0.3.x only**; earlier 0.1/0.2 GitHub releases and tags have been retired.

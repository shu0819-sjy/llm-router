# llm-router v0.2.0

Hardening release focused on safer defaults, OpenAI-compatible protocol polish, stream usage accounting, observability, CI, and public OSS docs.

## Compatibility

- **Python:** 3.11+ (CI covers 3.11 and 3.12)
- **API:** `POST /v1/chat/completions` remains OpenAI-compatible for JSON and SSE (`stream=true`)
- **Auth:** `Authorization: Bearer <api_key>` unchanged; additive `X-Request-Id` response header
- **Health:** legacy `GET /health` retained; additive `/health/live`, `/health/ready`, `/health/providers`
- **Config:** new optional `LLM_ROUTER_ENV` (default `production`) and `LLM_ROUTER_RATE_LIMIT_SECRET`
- **Breaking for insecure deploys:** outside `development`/`test`, empty or placeholder `LLM_ROUTER_ADMIN_TOKEN` values are rejected at startup

## Security

- Production/default mode rejects empty/well-known admin tokens
- Constant-time admin token comparison
- Rate-limit bucket identity uses digests (not raw API keys); set `LLM_ROUTER_RATE_LIMIT_SECRET` for multi-instance deployments
- Admin mutation audit events omit secrets
- Vulnerability reporting via [SECURITY.md](./SECURITY.md)

## Testing

- Default: `pytest --cov=app` (unit only; coverage gate ≥80%)
- Lint: `ruff check app tests scripts`
- Types: `mypy app` (informational in CI for v0.2.0)
- Docker: `docker build -t llm-router:ci .` plus CI `/health` smoke
- Opt-in live lane: `LLM_ROUTER_RUN_INTEGRATION=1 pytest -m integration`
- Public hygiene: `python scripts/audit_public_release.py`

## Known limitations

- Rate limiter and circuit breakers are in-memory (not shared across replicas)
- No billing product / multi-tenant RBAC beyond API keys
- Chat completions only (no embeddings / images / audio routes)
- Streaming usage: recorded as `accounting_status=actual` when upstream SSE emits a usage-bearing chunk; otherwise `unavailable` (cost is not invented)

## Highlights

- Digest-based rate-limit identities
- Health split while keeping legacy `/health`
- Request IDs and admin mutation audit events
- Price-versioned cost snapshots and storage ports
- GitHub Actions CI with coverage gate and Docker `/health` smoke
- `SECURITY.md`, `CONTRIBUTING.md`, issue/PR templates

## Upgrade notes

1. Set a strong unique `LLM_ROUTER_ADMIN_TOKEN` for any non-development deployment.
2. Optionally set `LLM_ROUTER_RATE_LIMIT_SECRET` when running multiple instances.
3. Prefer `/health/ready` for orchestrator readiness probes if desired.

See [CHANGELOG.md](./CHANGELOG.md) for the full list.

## Post-release clarification: compatibility scope

Added after the v0.2.0 release to keep claims precise. v0.2.0's "OpenAI-compatible" API surface is exactly:

- `POST /v1/chat/completions` — JSON and SSE (`stream=true`)
- `GET /v1/models`

No other OpenAI endpoints are implemented (`/v1/completions`, embeddings, images, audio, files, batches, fine-tuning are not supported). `tools` / `tool_choice` / `response_format` are forwarded as-is to OpenAI-compatible upstreams only; Anthropic-routed requests carrying these fields are rejected with `400`. See the README "Compatibility scope" section for the current, authoritative list.

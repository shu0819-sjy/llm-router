# Operations — single-node industrial deploy

llm-router 0.3.x is a **single-node** OpenAI-compatible gateway. This note covers packaged-defaults production hygiene, not a multi-replica claim.

## Checklist

1. **Admin token** — set a strong unique `LLM_ROUTER_ADMIN_TOKEN` (≥24 chars, mixed classes). Leave `LLM_ROUTER_ENV` at `production` (default).
2. **Upstream keys** — set only the providers you use (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `QWEN_API_KEY`). Empty disables that provider.
3. **Gateway keys** — bootstrap with `LLM_ROUTER_API_KEYS` and/or create keys in `/panel/`. Panel keys are hashed in SQLite and survive restart.
4. **SQLite volume** — persist `LLM_ROUTER_DB_PATH` (Compose maps `./data` → `/app/data`). One writer process only.
5. **Timeouts** — defaults are `30000` ms per attempt and `60000` ms failover budget. Raise for slow models; do not use the old 500 ms demo values in production.
6. **Health probes** — liveness `GET /health/live`; readiness `GET /health/ready`; legacy `GET /health` remains.
7. **Diagnostics** — keep `LLM_ROUTER_DIAGNOSTICS_AUTH=auto` (or `admin`) so `/health/providers` and `/metrics` are not public in production.
8. **Prometheus** — optional `LLM_ROUTER_ENABLE_PROMETHEUS=true`; still gated by diagnostics auth.
9. **Restart** — Compose `restart: unless-stopped` (or equivalent). Prefer one container / one uvicorn process per database file.

## Docker Compose

```bash
cp .env.example .env
# edit LLM_ROUTER_ADMIN_TOKEN + upstream keys; set LLM_ROUTER_ENV=production
docker compose up --build -d
curl -fsS http://localhost:8000/health/ready
```

## What not to do

- Do not scale to N replicas sharing one SQLite file and expect consistent rate limits / circuit breakers.
- Do not expose the panel or diagnostics without a strong admin token.
- Do not commit `.env`.
- Do not assume full OpenAI API compatibility beyond Chat Completions + Models.

## Verification

```bash
pytest --cov=app
python scripts/restart_migration_smoke.py --build   # requires Docker
```

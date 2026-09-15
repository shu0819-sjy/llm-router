# llm-router v0.1.0 — Release Notes

**Date:** 2026-03-22  
**Tag:** `v0.1.0`  
**Repo:** `shu0819-sjy/llm-router`

## Highlights

First public cut of an OpenAI-compatible asyncio FastAPI LLM gateway:

- Multi-provider routing: **OpenAI / Anthropic (Claude) / DeepSeek / Qwen**
- Key-based + model-prefix routing with **&lt;500 ms** budgeted failover and circuit breaker
- Token-bucket rate limiting, SQLite token/cost ledger, SSE streaming with backpressure
- `/health`, optional Prometheus `/metrics`, Web panel MVP, Docker Compose one-command
- **48** pytest cases; **~94%** line coverage on `app/` (bar ≥80%)
- Failover bench max **~127 ms** (fake upstreams, budget 500 ms) — PASS

## Included artifacts

- `README.md` — architecture (Mermaid), config, benchmark, interview points, Docker
- `REQUIREMENTS.md` — locked v0.1 contracts
- `LICENSE` — MIT
- `Dockerfile` + `docker-compose.yml` + `.env.example`
- `pyproject.toml` / `requirements.txt` / `requirements-dev.txt`

## Breaking / limits

- In-memory rate limiter & circuit breakers (not shared across replicas)
- Stream token counts recorded as 0 until a future aggregator
- No billing UI, no RBAC beyond API keys
- GitHub push requires valid `gh` auth for `shu0819-sjy`

## Upgrade / install

```powershell
cd "llm-router"
copy .env.example .env
docker compose up --build -d
```

## Auth recovery (if push failed)

```text
gh auth status
# Expected when broken:
#   X Failed to log in to github.com account shu0819-sjy
#   - The token in default is invalid.
gh auth login -h github.com
```

Then create/push and tag as documented in the README “GitHub publish” section.

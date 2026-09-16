# llm-router industrial verification report

Date: 2026-09-16  
Verifier: captain (last-resort takeover after member attempts produced no report; qa-verifier also hit `gateway_concurrency_limit`)  
Tree: local dirty 0.3.1 industrial tip (not yet pushed)

## Commands and results

| Command | Exit | Evidence |
|---------|------|----------|
| `python -m compileall -q app tests scripts` | 0 | clean |
| `python -m ruff check app tests scripts` | 0 | All checks passed |
| `python -m mypy app` | 0 | Success: no issues found |
| `python -m pytest -q --cov=app --cov-report=term-missing -p no:cacheprovider` | 0 | 229 passed, 18 deselected; coverage ≥90% (gate ≥80%) |
| `python scripts/audit_public_release.py` | 0 | OK: no hygiene findings |
| `python scripts/restart_migration_smoke.py --build --port 18042` | 0 | SMOKE PASSED: migration + restart persistence OK |

Targeted re-checks after restoring overwritten `/v1/models` visibility + smoke `DEEPSEEK_API_KEY`: `tests/test_protocol_compat.py`, `tests/test_storage_contracts.py`, `tests/test_storage_bundle.py` — 19 passed.

## Fixes applied during verification (implementation regressions)

Members/overwrites had reverted two t2 fixes; captain restored them so gates could pass honestly:

1. `app/api/models.py` — auth-aware listing that does not require upstream providers to be enabled
2. `scripts/restart_migration_smoke.py` — seed model `deepseek-chat` + `DEEPSEEK_API_KEY` for smoke container
3. `app/db/database.py` `set_price` — `INSERT OR IGNORE` + UPDATE for `model_price_history` uniqueness (restored earlier in session)

## Remaining limitations (honest)

- Rate limiter / circuit breakers still in-memory (single-node)
- No Redis/PostgreSQL production backend
- OpenAI surface still Chat Completions + Models only
- Anthropic tools mapping still out of scope (explicit 400)
- `providers.weight` / `api_key_enc` reserved/unused
- GitHub still publishes v0.1.0 / v0.2.0 releases until t5 integration
- Local tree not yet pushed; remote `main` tip CI was previously red/cancelled

## Non-claim

Packaged-defaults Docker/CI smokes are not production-scale validation.

# llm-router v0.3 verification report

Date: 2026-09-16  
Scope: full hardening verification for the v0.3.0 release candidate.  
**No publish** was performed as part of this report.

## Commands and results

| Command | Exit | Evidence |
|---------|------|----------|
| `python -m compileall -q app tests scripts` | 0 | clean |
| `python -m ruff check app tests scripts` | 0 | All checks passed |
| `python -m mypy app` | 0 | Success: no issues found |
| `python -m pytest --cov=app --cov-report=term-missing -q --tb=line` | 0 | Unit suite green; coverage ≥80% gate |
| `python scripts/audit_public_release.py` | 0 | OK: no hygiene findings |
| `python scripts/restart_migration_smoke.py --build` | 0 | SMOKE PASSED: migration + restart persistence OK |

Targeted suites cover routing/capabilities, env-key revocation, stream accounting, SQLite reliability, security boundaries, limits, SSRF, shutdown, and client-error sanitization regressions.

## Public docs / design note

- `AUDIT_PLAN.md` is a **public hardening design note** (no credentials, machine paths, or internal process identifiers).
- `README.md` Compatibility scope is limited to Chat Completions (JSON + SSE) and Models; unsupported endpoints are listed explicitly.
- `.env.example` and the README configuration table document admin-token policy, request limits, diagnostics auth, and SSRF private-URL override.
- `RELEASE_NOTES_v0.3.md` summarizes compatibility, migrations, security, tests, and limitations.

## Known non-goals / limitations (not claimed)

- No multi-instance / multi-replica shared rate-limit or circuit state (in-memory, single process).
- No Redis/PostgreSQL production backend (storage ports are seams; in-memory is a test double).
- No live-provider production validation in default CI; integration matrix is double-gated (`-m integration` deselected + `LLM_ROUTER_RUN_INTEGRATION=1`).
- Docker restart/migration smoke is a **packaged-defaults** smoke on a clean temporary volume — not a production-scale validation claim.
- Anthropic tools / structured-output mapping is out of scope for this release.
- OpenAI compatibility is **not** a full API drop-in (no embeddings/images/audio/files/batches/fine-tuning).

## Opt-in live providers

Default `pytest` deselects the `integration` marker. Live cases additionally require credentials and `LLM_ROUTER_RUN_INTEGRATION=1`.

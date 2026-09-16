# Integration summary — industrial third-gen only

Date: 2026-09-16  
Team: llm-router-industrial

## Public release surface

| Artifact | Status |
|----------|--------|
| GitHub Release `v0.1.0` | **Deleted** |
| GitHub Release `v0.2.0` | **Deleted** |
| GitHub tags `v0.1.0` / `v0.2.0` | **Absent** (removed with releases) |
| GitHub Release / tag `v0.3.0` | **Kept** (Latest) |

Verified with `gh release list` and `gh api .../tags` — only `v0.3.0` remains.

## In-tree retirement

Removed from the active tree:

- `RELEASE_NOTES_v0.1.md`
- `RELEASE_NOTES_v0.2.md`
- `PUBLISH_CHECKLIST_v0.2.0.md`

`SECURITY.md` supports **0.3.x only**. CHANGELOG historical sections for 0.1/0.2 retained as history (not a supported product line).

## Industrial tip (local)

Working tree advances the tip with **0.3.1** industrial single-node hardening:

- Auth-aware `/v1/models` that does not require upstream keys to be enabled
- Smoke seed `deepseek-chat` + `DEEPSEEK_API_KEY` placeholder
- Default timeouts `30000` / `60000` ms
- CI job/step timeouts
- `docs/OPERATIONS.md` single-node runbook
- Price history + `set_price` idempotency
- Verification: 229 passed, Docker SMOKE PASSED (see `verification-report-industrial.md`)

## Remaining non-goals

- Multi-replica shared rate-limit / circuit state
- Redis/PostgreSQL production backend
- Full OpenAI API surface
- Anthropic tools mapping
- `providers.weight` / `api_key_enc` still reserved/unused
- No production-scale validation claim

## How to run (single-node)

See `docs/OPERATIONS.md`. Strong `LLM_ROUTER_ADMIN_TOKEN`, persist SQLite volume, one process per DB file.

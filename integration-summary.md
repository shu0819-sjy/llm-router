# Integration summary 鈥?industrial third-gen only

Date: 2026-09-16 (updated for privacy rewrite + CI timeout fix)  
Team: llm-router-industrial

## Public release surface

| Artifact | Status |
|----------|--------|
| GitHub Release `v0.1.0` | **Deleted** |
| GitHub Release `v0.2.0` | **Deleted** |
| GitHub tags `v0.1.0` / `v0.2.0` | **Absent** |
| GitHub Release / tag `v0.3.0` | **Kept** (Latest) |

Verified with `gh release list` and `gh api .../tags` 鈥?only `v0.3.0` remains.

## Local tip (ready for force-push)

- Local `main` after author-email rewrite + CI timeout commit (hashes changed).
- Prior published tips (`d7b2c1f` / `f184e00`) are rewritten; force-push required (captain).
- Author/committer emails on `main`: noreply GitHub identities only (no personal QQ or school addresses).

## In-tree retirement

Removed from the active tree:

- `RELEASE_NOTES_v0.1.md`
- `RELEASE_NOTES_v0.2.md`
- `PUBLISH_CHECKLIST_v0.2.0.md`

`SECURITY.md` supports **0.3.x only**. CHANGELOG historical 0.1/0.2 sections retained as history only.

## Industrial tip contents

- Auth-aware `/v1/models` that does not require upstream keys to be enabled
- Smoke seed `deepseek-chat` + `DEEPSEEK_API_KEY` placeholder
- Default timeouts `30000` / `60000` ms
- CI job/step timeouts (test 20m / pytest 12m + `timeout 600`, docker 25m)
- `docs/OPERATIONS.md` single-node runbook
- Price history + `set_price` idempotency
- Verification: 229 passed, Docker SMOKE PASSED (`verification-report-industrial.md`)

## CI cancel root cause

- Not `concurrency.cancel-in-progress` (workflow never had it).
- Pattern across cancelled runs: Docker 鉁? Test 3.11 鉁? Test 3.12 hung ~6h 鈫?GitHub default job timeout 鈫?`cancelled`.
- Fix: explicit `timeout-minutes` + `timeout 600` around pytest so hangs surface as failures, not cancels.

## Remaining non-goals / known gaps

- Multi-replica shared rate-limit / circuit state
- Redis/PostgreSQL production backend
- Full OpenAI API surface
- Anthropic tools mapping
- `providers.weight` / `api_key_enc` reserved/unused
- Underlying Python 3.12 hang root cause not yet isolated (timeouts contain blast radius)
- No production-scale validation claim

## How to run (single-node)

See `docs/OPERATIONS.md`. Strong `LLM_ROUTER_ADMIN_TOKEN`, persist SQLite volume, one process per DB file.

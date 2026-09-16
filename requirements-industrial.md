# llm-router Industrial Requirements — Round 1 (v0.3.x single-node line)

Status: requirements for the "llm-router-industrial" round 1. Authored by requirements-lead.
Date: 2026-09-16. Product stance: **shippable industrial-grade single-node OpenAI-compatible LLM gateway**. We finish and harden the third-generation v0.3.x line; we do not chase a multi-replica platform.

---

## 1. Grounded current state (evidence, as of this round)

### 1.1 Repo / tip sync

| Item | Value | Evidence |
|---|---|---|
| GitHub main tip | `6297e9617302a84329420873002a5b4a6e0b8d2b` — "feat: retain queryable model price history" (2026-09-16T07:22:46Z) | GitHub API `commits/main` |
| Local workspace | `D:\deepseek workspace\llm-router` at `6eead1b` ("release: llm-router v0.3.0 hardening"), working tree clean | local `git status --porcelain -b`, `git log` |
| Commits to bring in (fast-forward, 2) | `0ffb3c2` "fix: align key lifecycle and model visibility"; `6297e961` "feat: retain queryable model price history" | GitHub API commit list |
| Local fetch blocker | `git fetch` fails in the harness shell (`schannel: SEC_E_NO_CREDENTIALS`); tip facts verified via GitHub REST API; use tarball/staging copy if fetch stays blocked | local pwsh output |

Content of the two incoming commits (from API patches):

- `0ffb3c2`: `/v1/models` becomes authorization-aware — filters model ids through `router.resolve(api_key, mid).candidates`; env-key sync moved to `db.sync_environment_keys()`; provider `httpx.AsyncClient` timeouts changed to `Timeout(connect=5, read=60, write=10, pool=5)` + connection limits; adds `test_v1_models_respects_forced_provider`.
- `6297e961`: new `model_price_history` table (append-only price versions, `UNIQUE(model, version, effective_from)`), backfill of current prices into history on init, `get_price(model, at=...)` historical lookup (SQL + in-memory double), `set_price` appends history and only advances the current row when `effective_from` is not older; adds `test_price_history_returns_quote_effective_at_requested_time`.

### 1.2 CI state on main — no green run on the v0.3 line

| Run | Head | Result |
|---|---|---|
| #1 | `5a1fbe2` (v0.2.0 prep) | **success** (last green CI) |
| #2 | `6eead1b` (v0.3.0) | **cancelled** after ~6 h (17:38 → 23:38) — hung, then cancelled |
| #3 | `0ffb3c2` | **cancelled** after ~1 h (id `35067785968`; cancel if still alive) |
| #4 | `6297e961` (tip) | **cancelled** after ~1 h (id `35068227152`, cancelled by captain) |

Conclusion: CI has been red/hung since the v0.3.0 release commit. Two distinct P0s: a deterministic smoke failure and a unit-test hang.

### 1.3 P0 regression: `/v1/models` × restart/migration smoke

- Observed on tip: `[migration] FAIL: seeded model price row missing from /v1/models: []`.
- Cause chain: `0ffb3c2` filters `/v1/models` through `router.resolve(api_key, mid).candidates`; the smoke seeds price row `smoke-legacy-model`, which matches no provider prefix, so the filter drops it and the smoke sees `[]`.
- Design tension to resolve **once and explicitly**: `GET /v1/models` was documented as "OpenAI-shaped model list (from the price catalog)" while `0ffb3c2` makes it an authorization-aware capability view. Both are defensible; the current state is neither (price rows invisible even to callers whose request would be routed by fallback).

### 1.4 Industrial gaps found in the tree

- **Unusable default timeouts**: `LLM_ROUTER_DEFAULT_TIMEOUT_MS=500` / `LLM_ROUTER_FAILOVER_BUDGET_MS=500` (README + settings). A 500 ms total budget fails every real LLM call. Provider clients now use 5/60/10/5 s httpx timeouts (`0ffb3c2`), so the settings defaults contradict the code path.
- **Dead schema surface**: `providers.api_key_enc` and `providers.weight` exist in `app/db/schema.sql` (lines 20/22), are written by `app/panel/routes.py` (~line 248) and mirrored in `scripts/restart_migration_smoke.py` and test fixture schemas, but **no routing code reads them** (`key_router.py` uses the env/registry provider set). Secrets written to `api_key_enc` are also a liability.
- **Gen1/gen2 artifacts still active in docs**: `RELEASE_NOTES_v0.1.md`, `RELEASE_NOTES_v0.2.md`, `PUBLISH_CHECKLIST_v0.2.0.md` in tree; README links both prior release-notes lines; `SECURITY.md` supported-versions table advertises `0.2.x Yes / 0.1.x Best-effort`.
- **GitHub releases/tags to retire**: releases v0.1.0 (id `389018979`) and v0.2.0 (id `389227902`) plus tags `v0.1.0`, `v0.2.0` are published; v0.3.0 stays.
- **Hung-test risk unmanaged**: CI has no per-job `timeout-minutes` and no pytest-level timeout; a single hanging test stalled main for 6 h.
- **Anthropic tools**: still rejected with `400` (documented honestly in v0.3.0). Optional upgrade this round if it lands with tests; otherwise the honest `400` stays.

---

## 2. Scope

**In scope** (all within `llm-router/` unless the step is explicitly a GitHub-side release-ops action):

1. Tip sync of the two remote commits (fast-forward; no rebase, no force-push).
2. Fix the P0 `/v1/models` smoke regression.
3. Fix the CI hang and add CI honesty guards (job timeouts).
4. Industrial defaults: realistic timeout/failover budgets.
5. `weight`/`api_key_enc` dead-field cleanup (remove + document) — implement-not-remove is accepted only if routing actually honors them with tests.
6. Panel/SECURITY/README/CHANGELOG consistency with a single v0.3.x line.
7. Anthropic tools mapping — conditional (see W6).
8. Single-node industrial runbook section in README/docs.
9. Gen1/gen2 retirement: repo doc cleanup (implementation) + GitHub release/tag deletion (release-ops, gated on review pass).

**Out of scope**: any change outside `llm-router/` except the release-ops GitHub actions (releases/tags/CI-run cancellation) and the team-level documents.

---

## 3. Work breakdown (prioritized; owner hints per team roles)

### W1 — P0: tip sync + `/v1/models` regression fix (backend-engineer, blocks everything)

1. Fast-forward local `main` to `6297e961`. If `git fetch` stays blocked in the harness shell, obtain the tip via unauthenticated tarball (`https://api.github.com/repos/shu0819-sjy/llm-router/tarball/6297e9617302a84329420873002a5b4a6e0b8d2b`) and record the applied diff in the implementation output. No force-push, no rebase.
2. Decide ONE authoritative `/v1/models` contract and make code, smoke, and tests agree. Required contract for this round (preferred approach from captain context):
   - Keep authorization-aware listing for chat-servable models, **and** make the smoke seed model prefix-compatible (e.g. `smoke-deepseek-model` with DeepSeek capability) **and** add an explicit unit test that forced-provider keys do not see unsupported models; **or**
   - Split concerns: verify price/catalog survival via DB/admin surface, and have `/v1/models` include priced catalog models even when not currently routable (never `[]` for unrestricted keys when the catalog is non-empty).
   - Either way: unrestricted keys must never get `[]` while the priced catalog is non-empty.
3. `restart_migration_smoke.py` passes end-to-end on tip content, including the new price-history checks: seeded price row visible per the chosen contract, v0.2.0-schema DB migrates additively, panel key + usage survive `docker restart`.

**Acceptance (measurable)**
- [ ] Local `main` contains `6eead1b`, `0ffb3c2`, `6297e961` (or an explicitly recorded tarball equivalent) and a new fix commit on top.
- [ ] `GET /v1/models` with an unrestricted key returns ≥ the priced catalog (never `[]` when the catalog is non-empty).
- [ ] `GET /v1/models` with a forced-provider key returns only models that key can serve, and stays non-empty when ≥1 provider is usable (covered by `test_v1_models_respects_forced_provider`, kept or adapted).
- [ ] `python scripts/restart_migration_smoke.py --build` exits 0 (requires Docker; if Docker is unavailable in this environment, qa-verifier records it as explicitly blocked with the command + error — not silently skipped).

### W2 — P0: CI hang fix + CI honesty (backend-engineer)

1. Reproduce the hang locally: run the default suite with a wall-clock guard and identify the hanging test(s) (candidates: any test doing real network/`httpx` without a fake transport, SSE lifecycle waits, or the Docker smoke inside CI without a Docker daemon).
2. Fix the root cause (fake transport / event-loop shutdown / skipped-with-reason). No "sleep longer" fixes.
3. CI hardening in `.github/workflows/ci.yml`:
   - `timeout-minutes` on every job (unit: 15; docker/smoke: 30; workflow ≤ 45).
   - `concurrency` group so a new push to main cancels the previous run.
   - pytest must complete without network by default (integration lane stays double-gated off).

**Acceptance (measurable)**
- [ ] Full default local suite (`python -m pytest -q --cov=app --cov-report=term-missing`) completes in **< 10 minutes** wall clock with exit 0 and coverage ≥ 80%.
- [ ] Named hang root cause is written into the implementation output (which test / why / fix).
- [ ] CI YAML has explicit `timeout-minutes` on all jobs and a `concurrency` block; verified by a YAML parse check.
- [ ] A CI run on the fixed tip reaches conclusion `success` (or every failing job is attributed to external infra with evidence) — qa-verifier captures the run URL.

### W3 — P1: industrial timeout defaults (backend-engineer)

1. Replace the 500 ms defaults with production-usable ones, consistent with the provider clients (5/60/10/5): e.g. `LLM_ROUTER_DEFAULT_TIMEOUT_MS` default `60000` (per-attempt, aligned with read=60s), `LLM_ROUTER_FAILOVER_BUDGET_MS` default `90000` (floor: ≥30000 / ≥60000 per captain context); keep both overridable.
2. Update `.env.example`, README config table, and any tests asserting the old defaults.
3. Document the semantics precisely (per-attempt vs total budget vs stream read) with worst-case latency guidance: per-attempt × candidate count ≤ budget.

**Acceptance (measurable)**
- [ ] Defaults changed in settings with docs updated; no test asserts 500 ms.
- [ ] README documents the worst-case client-visible latency bound.
- [ ] Failover bench (`python scripts/bench_failover.py`) still runs and prints a result.

### W4 — P1: `weight` / `api_key_enc` dead-field honesty (backend-engineer)

Decision for this round: **remove the dead surfaces** (single-node router doesn't honor weight; storing provider secrets unencrypted in `providers.api_key_enc` is worse than not having the column). Keep env-var credentials as the only provider credential path.

1. Drop `providers.weight` and `providers.api_key_enc` from `app/db/schema.sql`, the panel write path, the smoke script fixture schema, and `test_env_key_revocation.py` fixture schema. Keep additive-migration discipline: fresh schema creates the table without these columns; existing DBs simply keep unused columns (no destructive migration) — document in CHANGELOG.
2. Remove the fields from the panel UI/form too if referenced (panel/docs consistency).
3. Alternative accepted only with proof: implement weighted routing + encrypted-at-rest key storage with tests — explicitly the *bigger* option and not required this round.

**Acceptance (measurable)**
- [ ] `grep -rn "api_key_enc" app/ scripts/ tests/` returns no functional reference (audit redaction list may drop the name too).
- [ ] Fresh-DB boot and v0.2-schema-migrated DB boot both pass smoke; panel provider PATCH flow works without those fields.
- [ ] README/CHANGELOG note the removed fields as a documented behavior change (panel no longer stores provider credentials).

### W5 — P1: docs/version honesty — one active line (backend-engineer + release-ops split)

Repo-side (backend-engineer, in the same tip commit series):
1. Delete from tree: `RELEASE_NOTES_v0.1.md`, `RELEASE_NOTES_v0.2.md`, `PUBLISH_CHECKLIST_v0.2.0.md`.
2. README: release-notes section points to v0.3.x only; remove "Prior line" pointers.
3. `SECURITY.md` supported-versions: `0.3.x Yes`, everything older `No`.
4. `CHANGELOG.md` keeps 0.1.0/0.2.0 entries (history is append-only; changelog is not an "active product line" advertisement). The v0.3.0 release body on GitHub links to CHANGELOG instead of the deleted `RELEASE_NOTES_v0.2.md` — release-ops edits the release body text (allowed; not history rewrite).
5. Panel version display and `pyproject.toml` stay `0.3.x` (bump to `0.3.1` only if the round ships a release; otherwise leave version and note it).

GitHub-side (release-ops, **only after t4 review pass**):
6. Cancel hung/obsolete CI runs (`35067785968` if still alive; others already cancelled).
7. Delete release v0.1.0 (id `389018979`) + tag `v0.1.0`; release v0.2.0 (id `389227902`) + tag `v0.2.0`. Deleting a tag is not history rewriting; commit history is untouched.
8. Update the v0.3.0 release body to drop the `RELEASE_NOTES_v0.2.md` link.

**Acceptance (measurable)**
- [ ] Tree contains no gen1/gen2 release notes/checklists; grep over tracked files for `RELEASE_NOTES_v0.1|RELEASE_NOTES_v0.2|PUBLISH_CHECKLIST_v0.2` returns nothing (CHANGELOG excluded).
- [ ] SECURITY.md table lists exactly `0.3.x Yes`.
- [ ] GitHub repo shows releases: v0.3.0 only; remote tags no longer include `v0.1.0`/`v0.2.0`.
- [ ] No force-push occurred: `6eead1b`, `0ffb3c2`, `6297e961` remain reachable ancestors of main.

### W6 — P2 (conditional): Anthropic tools mapping

In scope **only if** it can land with unit tests and no change to the OpenAI-compat surface: map `tools`/`tool_choice`/`response_format` to Anthropic Messages equivalents and remove the 400 rejection for Anthropic-routed tool requests. If not landed, keep the honest documented `400` and record an issue-ready note (remaining non-goal).

**Acceptance (measurable, if attempted)**
- [ ] Unit tests: Anthropic adapter maps tools/tool_choice correctly (snapshot assertions); malformed tool payloads → 400.
- [ ] README capability caveats match the implementation exactly.
- [ ] If not attempted: README unchanged and the implementation output records "W6 skipped: reason".

### W7 — P2: single-node industrial runbook

README (or `docs/OPERATIONS.md` linked from README) section covering: recommended `.env` for production single node, strong admin token, SQLite volume + backup/restore (WAL caveat: checkpoint before copy), restart policy, health probes (`/health/ready`) and optional Prometheus `/metrics`, graceful shutdown (tests exist: `test_shutdown.py`), upgrade steps from any prior version, and an explicit "single node only — not multi-replica" statement.

**Acceptance (measurable)**
- [ ] Runbook exists, links from README, and every command in it is executable as written (spot-checked by qa-verifier where environment allows).

---

## 4. Explicit non-goals (this round)

- No multi-replica productization: no Redis/PostgreSQL storage backend, no shared rate-limit/breaker state across replicas. (Storage ports remain seams; in-memory stays a test double.)
- No full OpenAI API clone: still exactly `POST /v1/chat/completions` (JSON + SSE) + `GET /v1/models`; no embeddings/images/audio/files/batches/fine-tuning, no org/project header semantics.
- No multi-tenant billing / RBAC product beyond API keys.
- No git history rewrite / force-push of `main`; gen1/gen2 retirement is release/tag/doc level only.
- No production-scale/performance validation claims — Docker/CI smokes cover packaged defaults only; docs must keep saying so.
- No new provider adapters beyond mapping fixes to the existing four.

---

## 5. Verification matrix (qa-verifier, t3)

All commands run from `llm-router/` on the implemented tip:

| Gate | Command | Pass condition |
|---|---|---|
| Syntax | `python -m compileall -q app tests scripts` | exit 0 |
| Lint | `python -m ruff check app tests scripts` | exit 0 |
| Types | `python -m mypy app` | exit 0 (hard gate, no new disables) |
| Unit+coverage | `python -m pytest -q --cov=app --cov-report=term-missing` | exit 0, < 10 min, coverage ≥ 80% |
| Public hygiene | `python scripts/audit_public_release.py` | exit 0 |
| Bench | `python scripts/bench_failover.py` | completes, prints result |
| Docker smoke | `python scripts/restart_migration_smoke.py --build` | exit 0, or explicitly blocked with Docker-unavailability evidence |
| CI | GitHub Actions on fixed tip | conclusion success with run URL |
| Retirement | releases/tags listing | only v0.3.0 remains (post-review step) |

## 6. Definition of done (round 1)

Tip = green CI + all gates above passing on `llm-router/`; `/v1/models` contract implemented per W1; industrial defaults per W3; dead fields gone per W4; docs advertise only the v0.3.x line per W5; gen1/gen2 GitHub artifacts deleted per W5 (release-ops, post-review); every claim in README/SECURITY/CHANGELOG true of the shipped tree.

# Hardening design notes — v0.3.0

This note describes the design intent behind the v0.3.0 hardening release of
llm-router. It is safe for public distribution: it contains no credentials,
machine-specific paths, or internal process details. It records *what* was
hardened and *why*, not release-management mechanics.

## Scope

v0.3.0 is a reliability and safety hardening release on top of v0.2.0. The
public API surface is unchanged: OpenAI compatibility remains limited to
Chat Completions (JSON + SSE) and the Models list (see README
"Compatibility scope"). Work areas:

1. **Routing correctness.** Failover candidates must be providers that
   actually serve the requested model (prefix match), so the failover budget
   is never spent on providers that would reject the model. Keys pinned to a
   single provider get router-level errors when that provider does not
   support the model, instead of an upstream error round-trip.
2. **Key lifecycle.** API keys can come from the environment
   (`LLM_ROUTER_API_KEYS`) or the admin panel. The database is authoritative:
   revoking a key in the panel stays revoked across restarts even when the
   raw key is still listed in the environment, and removing a key from the
   environment deactivates its database row.
3. **Runtime boundaries.** Production startup rejects weak admin tokens;
   request bodies, message/tool payload sizes, and in-flight concurrency are
   bounded; provider base URLs are validated (https and non-private targets
   outside development) to prevent the gateway being retargeted at internal
   networks (SSRF).
4. **Streaming accounting.** Streamed requests record real time-to-first-byte
   and total latency and distinguish clean completion from client disconnect
   and mid-stream errors, in both the usage ledger and metrics.
5. **Storage reliability.** SQLite runs with WAL, a busy timeout, and write
   resilience for concurrent usage writes; storage access goes through small
   protocol ports (storage bundle) so the SQLite adapter is a swappable
   implementation and tests can run the whole app on an in-memory store.
6. **Diagnostics.** Provider inventory and metrics exposition follow a single
   documented access policy; label cardinality in the Prometheus exposition is
   bounded by code constants.
7. **Quality gates and reproducibility.** CI treats lint and type checks as
   hard gates with coverage ≥80%; the Docker base image is digest-pinned and
   direct dependencies exact-pinned; an opt-in live-provider matrix
   (double-gated off by default) and a Docker restart/migration smoke cover
   release-critical behaviors on clean temporary volumes.

## Verification approach

- Unit suite (fake upstreams, no live network) with coverage gate — default
  `pytest` run.
- Opt-in integration matrix: provider × sync/SSE × plain/tools, enabled only
  with `LLM_ROUTER_RUN_INTEGRATION=1` and per-provider credentials; never in
  default CI.
- `scripts/restart_migration_smoke.py`: boots the image on a clean temporary
  volume with a v0.2.0-schema database and verifies additive migration, row
  survival, and restart persistence of panel keys and usage history.
- Docker CI job: build, `/health` smoke, restart/migration smoke.

These are packaged-defaults smokes and unit/integration tests, not a
production-scale validation claim.

## Non-goals

- No new OpenAI endpoint families (embeddings, images, audio, files, batches,
  fine-tuning); compatibility claims stay narrowed to Chat Completions +
  Models.
- No Redis/PostgreSQL production backend (storage ports exist as seams; the
  in-memory store is a test double).
- No multi-replica shared state (rate limiter and circuit breakers stay
  in-memory), no billing/RBAC product.
- No Anthropic tools/structured-output mapping in this release.

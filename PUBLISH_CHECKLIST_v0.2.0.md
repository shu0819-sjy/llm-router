# Publish checklist — v0.2.0

Local release preparation only. **Do not push tags or create a GitHub Release until the maintainer explicitly confirms.**

## Prepared locally

- [x] Version `0.2.0` in `pyproject.toml` and `app/__init__.py`
- [x] `CHANGELOG.md` `[0.2.0]` section
- [x] `RELEASE_NOTES_v0.2.md` (compatibility, security, testing, limitations)
- [x] `SECURITY.md` supports `0.2.x`
- [x] Compose image tag `llm-router:0.2.0`
- [x] Internal artifacts removed (`HARDENING_PLAN.md`, `verification-report.md`)
- [x] `.dockerignore` present
- [x] Public audit clean (`python scripts/audit_public_release.py`)

## After explicit user confirmation

```bash
# from repo root, on a clean publish-ready commit
git push origin main
git tag -a v0.2.0 -m "llm-router v0.2.0 hardening release"
git push origin v0.2.0
# optional: gh release create v0.2.0 -F RELEASE_NOTES_v0.2.md
```

## Smoke after publish

```bash
docker compose up --build -d
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/health/ready
```

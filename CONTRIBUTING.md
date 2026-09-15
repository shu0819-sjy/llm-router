# Contributing to llm-router

Thanks for helping improve this project. This guide covers local development, tests, and dependency hygiene for a public open-source workflow.

## Development setup

```bash
git clone https://github.com/shu0819-sjy/llm-router.git
cd llm-router
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
# .env.example defaults to LLM_ROUTER_ENV=development so the placeholder admin
# token works locally. For production-like runs, set LLM_ROUTER_ENV=production
# and a strong unique LLM_ROUTER_ADMIN_TOKEN.
```

Run the API locally:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Tests

Default CI and local runs execute **unit tests only** (no live upstream network):

```bash
python -m pytest -q --cov=app --cov-report=term-missing
ruff check app tests scripts
mypy app
```

### Opt-in live provider integration tests

Integration tests under `tests/integration/` are **never** selected by default (`-m "not integration"` in `pyproject.toml`). They also require an explicit environment flag:

```bash
export LLM_ROUTER_RUN_INTEGRATION=1   # Windows PowerShell: $env:LLM_ROUTER_RUN_INTEGRATION=1
# Provide real upstream credentials in the environment if the test needs them
python -m pytest -m integration -q
```

Do not enable `LLM_ROUTER_RUN_INTEGRATION` in default CI.

## Lint and types

- Lint (hard gate in CI): `ruff check app tests scripts`
- Types (hard gate in CI as of v0.3): `mypy app`

Both ruff and mypy must pass before merge. The residual mypy `disable_error_code` list in `pyproject.toml` is a documented, itemized set (assignment / arg-type / return-value) with a known path to removal — do not widen it. Newly introduced type errors in touched code should be fixed in the same PR.

*(Supersedes the v0.2.0 note that mypy was `continue-on-error` / informational only.)*

## Docker

```bash
docker build -t llm-router:ci .
docker compose up --build -d   # requires a local .env
curl -fsS http://127.0.0.1:8000/health
```

A committed [`.dockerignore`](./.dockerignore) excludes `.env` / `.env.*`, virtualenvs, `__pycache__`, `.git`, `data/*.db`, pytest/mypy/ruff caches, and coverage artefacts from the build context. The Dockerfile installs only from the pinned `requirements.txt` and copies `app/` + `pyproject.toml`.

## Reproducible dependencies

Runtime pins live in `requirements.txt` (used by the Dockerfile). Dev/CI tools are pinned in `requirements-dev.txt` and mirrored under `[project.optional-dependencies].dev` in `pyproject.toml`.

To refresh pins:

1. Review upstream changelogs for breaking changes.
2. Update the version pins in `requirements.txt` / `requirements-dev.txt` / `pyproject.toml` together.
3. Recreate the venv (or `pip install -r requirements-dev.txt`).
4. Re-run unit tests, `ruff`, `mypy`, and `docker build -t llm-router:ci .`.

We intentionally keep a simple pinned requirements file rather than a heavyweight lockfile so Docker and local installs stay identical without extra tooling.

## Pull requests

- Open PRs against `main` (or `master` if that is the default branch).
- Keep diffs free of secrets, real API keys, admin tokens, and machine-absolute paths.
- Prefer small, focused changes with tests for behavior changes.
- Use the PR template checklist.

## Security

See [SECURITY.md](./SECURITY.md) for private vulnerability reporting.

## License

By contributing, you agree that your contributions are licensed under the MIT License in [LICENSE](./LICENSE).

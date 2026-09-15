# llm-router — build context must be this repo root only
# Base image is pinned by digest for reproducible builds. To refresh: pull the
# new tag, replace the digest below (docker image inspect python:3.11-slim
# --format '{{index .RepoDigests 0}}'), and re-run CI plus the restart/migration
# smoke (scripts/restart_migration_smoke.py).
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LLM_ROUTER_HOST=0.0.0.0 \
    LLM_ROUTER_PORT=8000 \
    LLM_ROUTER_DB_PATH=/app/data/llm_router.db

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

# Install from the pinned lock-style requirements for reproducible images.
# All direct runtime dependencies are exact-pinned in requirements.txt, so the
# image contents vary only with the base digest above and these pins.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY pyproject.toml .

RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# Legacy /health remains the default HEALTHCHECK target (compat policy)
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD curl -f http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

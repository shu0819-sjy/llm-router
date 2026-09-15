"""GET /health (+ live/ready/providers) and optional GET /metrics.

Semantics (HARDENING_PLAN F06):
- GET /health         — legacy aggregate (status, version, uptime_s, providers, db)
- GET /health/live    — process liveness only (always 200 if the app can answer)
- GET /health/ready   — readiness: DB must ping; 503 when not ready to serve
- GET /health/providers — per-provider enabled/circuit/healthy detail

Auth / network policy (v0.3 hardening):
- /health, /health/live, /health/ready are public and dependency-free so load
  balancers and container probes can poll them. The legacy /health body only
  carries coarse per-provider status (enabled/circuit/healthy).
- /health/providers (detailed diagnostics) and /metrics (Prometheus
  exposition) are gated by LLM_ROUTER_DIAGNOSTICS_AUTH:
    * "auto" (default): admin bearer token required outside development
      mode; open in development/test for local debugging.
    * "admin": always require the admin bearer token.
    * "public": always open (operator opt-in).
  /metrics additionally returns 404 when Prometheus exposition is disabled
  (LLM_ROUTER_ENABLE_PROMETHEUS=false), independent of auth.
- A production instance whose admin token is missing/weak refuses diagnostics
  with 503 instead of exposing provider details.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app import __version__
from app.auth import (
    DEFAULT_ADMIN_TOKEN_MIN_LENGTH,
    admin_token_quality_issues,
    constant_time_token_equals,
    extract_bearer,
    is_development_mode,
)
from app.metrics.prometheus import render_prometheus

router = APIRouter(tags=["health"])


def _diagnostics_requires_admin(settings: Any) -> bool:
    mode = str(getattr(settings, "diagnostics_auth", "auto") or "auto").strip().lower()
    if mode == "public":
        return False
    if mode == "admin":
        return True
    # auto: locked down outside development mode.
    return not is_development_mode(settings)


async def require_diagnostics_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """Route dependency enforcing the diagnostics/metrics auth policy above."""
    settings = request.app.state.settings
    if not _diagnostics_requires_admin(settings):
        return
    if not is_development_mode(settings):
        min_length = int(
            getattr(settings, "admin_token_min_length", DEFAULT_ADMIN_TOKEN_MIN_LENGTH)
            or DEFAULT_ADMIN_TOKEN_MIN_LENGTH
        )
        if admin_token_quality_issues(settings.admin_token, min_length=min_length):
            raise _diagnostics_error(503, "admin_insecure_config")
    expected = (settings.admin_token or "").strip()
    if not expected:
        raise _diagnostics_error(503, "admin_not_configured")
    token = extract_bearer(authorization) or ""
    if not constant_time_token_equals(token, expected):
        raise _diagnostics_error(401, "invalid_admin_token")


def _diagnostics_error(status_code: int, code: str) -> Exception:
    messages = {
        "admin_insecure_config": "Admin token missing or insecure; diagnostics are locked down.",
        "admin_not_configured": "Admin token not configured; diagnostics are locked down.",
        "invalid_admin_token": "Invalid admin token",
    }
    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "message": messages.get(code, "Diagnostics unavailable"),
                "type": "api_error" if status_code >= 500 else "invalid_request_error",
                "code": code,
            }
        },
    )


def _provider_snapshot(request: Request) -> tuple[dict[str, Any], bool]:
    registry = request.app.state.registry
    breakers = request.app.state.breakers
    providers: dict[str, Any] = {}
    degraded = False
    for p in registry.all():
        circuit = breakers.get(p.id).state.value
        healthy = p.enabled and circuit != "open"
        if p.enabled and not healthy:
            degraded = True
        providers[p.id] = {
            "enabled": p.enabled,
            "circuit": circuit,
            "healthy": healthy,
        }
        metrics = getattr(request.app.state, "metrics", None)
        if metrics is not None:
            metrics.set_circuit(p.id, circuit)
    return providers, degraded


async def _db_status(request: Request) -> str:
    db = getattr(request.app.state, "db", None)
    if db is None:
        return "n/a"
    return "ok" if await db.ping() else "error"


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Legacy aggregate health — shape preserved for v0.2 compatibility."""
    started_at: float = getattr(request.app.state, "started_at", time.time())
    providers, degraded = _provider_snapshot(request)
    db_status = await _db_status(request)
    if db_status != "ok" and db_status != "n/a":
        degraded = True
    return {
        "status": "degraded" if degraded else "ok",
        "version": __version__,
        "uptime_s": round(time.time() - started_at, 3),
        "providers": providers,
        "db": db_status,
    }


@router.get("/health/live")
async def health_live(request: Request) -> dict[str, Any]:
    """Liveness: process is up. No dependency checks."""
    started_at: float = getattr(request.app.state, "started_at", time.time())
    return {
        "status": "ok",
        "version": __version__,
        "uptime_s": round(time.time() - started_at, 3),
    }


@router.get("/health/ready")
async def health_ready(request: Request) -> Any:
    """Readiness: local DB must be reachable. 503 when not ready."""
    db_status = await _db_status(request)
    ready = db_status in ("ok", "n/a")
    body = {
        "status": "ok" if ready else "error",
        "db": db_status,
        "version": __version__,
    }
    if not ready:
        return JSONResponse(status_code=503, content=body)
    return body


@router.get("/health/providers", dependencies=[Depends(require_diagnostics_auth)])
async def health_providers(request: Request) -> dict[str, Any]:
    """Provider/circuit detail (also embedded under legacy /health). Admin-gated."""
    providers, degraded = _provider_snapshot(request)
    return {
        "status": "degraded" if degraded else "ok",
        "providers": providers,
    }


@router.get("/metrics", dependencies=[Depends(require_diagnostics_auth)])
async def metrics_endpoint(request: Request) -> Response:
    settings = request.app.state.settings
    if not getattr(settings, "enable_prometheus", False):
        return Response(
            content="Prometheus metrics disabled. Set LLM_ROUTER_ENABLE_PROMETHEUS=true\n",
            status_code=404,
            media_type="text/plain",
        )
    reg = request.app.state.metrics
    breakers = request.app.state.breakers
    extra = {pid: breakers.get(pid).state.value for pid in breakers.states()}
    for p in request.app.state.registry.all():
        extra.setdefault(p.id, breakers.get(p.id).state.value)
    body = render_prometheus(reg, extra_circuits=extra)
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")

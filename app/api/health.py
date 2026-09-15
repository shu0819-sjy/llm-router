"""GET /health (+ live/ready/providers) and optional GET /metrics.

Semantics (HARDENING_PLAN F06):
- GET /health         — legacy aggregate (status, version, uptime_s, providers, db)
- GET /health/live    — process liveness only (always 200 if the app can answer)
- GET /health/ready   — readiness: DB must ping; 503 when not ready to serve
- GET /health/providers — per-provider enabled/circuit/healthy detail
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app import __version__
from app.metrics.prometheus import render_prometheus

router = APIRouter(tags=["health"])


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


@router.get("/health/providers")
async def health_providers(request: Request) -> dict[str, Any]:
    """Provider/circuit detail (also embedded under legacy /health)."""
    providers, degraded = _provider_snapshot(request)
    return {
        "status": "degraded" if degraded else "ok",
        "providers": providers,
    }


@router.get("/metrics")
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

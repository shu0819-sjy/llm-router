"""GET /health and optional GET /metrics."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request, Response

from app import __version__
from app.metrics.prometheus import render_prometheus

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    started_at: float = getattr(request.app.state, "started_at", time.time())
    registry = request.app.state.registry
    breakers = request.app.state.breakers
    db = getattr(request.app.state, "db", None)

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

    db_status = "n/a"
    if db is not None:
        db_status = "ok" if await db.ping() else "error"
        if db_status != "ok":
            degraded = True

    return {
        "status": "degraded" if degraded else "ok",
        "version": __version__,
        "uptime_s": round(time.time() - started_at, 3),
        "providers": providers,
        "db": db_status,
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
    # Also include known providers even if never tripped
    for p in request.app.state.registry.all():
        extra.setdefault(p.id, breakers.get(p.id).state.value)
    body = render_prometheus(reg, extra_circuits=extra)
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")

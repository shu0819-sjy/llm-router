"""FastAPI application factory + lifespan."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app import __version__
from app.api import chat as chat_api
from app.api import health as health_api
from app.config import Settings, get_settings
from app.panel import router as panel_router
from app.db.database import Database, set_db
from app.db.ledger import UsageLedger
from app.failover.circuit_breaker import CircuitBreakerRegistry
from app.failover.orchestrator import FailoverOrchestrator
from app.metrics.prometheus import MetricsRegistry
from app.providers.registry import ProviderRegistry, build_default_registry
from app.ratelimit.token_bucket import TokenBucketLimiter
from app.routing.key_router import KeyRouter

logger = logging.getLogger("llm_router")


async def _sync_env_keys(db: Database, settings: Settings) -> None:
    for item in settings.parsed_api_keys():
        await db.upsert_api_key(
            raw_key=str(item["key"]),
            name=str(item["name"]),
            provider_id=item.get("provider_id"),  # type: ignore[arg-type]
        )


def create_app(
    settings: Settings | None = None,
    *,
    registry: ProviderRegistry | None = None,
    db: Database | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.started_at = time.time()
        database: Database = app.state.db
        await database.connect()
        set_db(database)
        await _sync_env_keys(database, app.state.settings)
        router: KeyRouter = app.state.key_router
        router.bind_db(database)
        linked = await router.hydrate_from_db()
        logger.info(
            "llm-router %s starting (db=%s, env_keys_linked=%s)",
            __version__,
            database.path,
            linked,
        )
        yield
        reg: ProviderRegistry = app.state.registry
        await reg.aclose()
        router.bind_db(None)
        await database.close()
        set_db(None)
        logger.info("llm-router shutdown complete")

    app = FastAPI(
        title="llm-router",
        version=__version__,
        description="OpenAI-compatible asyncio FastAPI LLM gateway with multi-provider failover",
        lifespan=lifespan,
    )

    reg = registry or build_default_registry(settings)
    breakers = CircuitBreakerRegistry(
        failure_threshold=settings.cb_failure_threshold,
        recovery_timeout_s=settings.cb_recovery_timeout_s,
        half_open_max=settings.cb_half_open_max,
    )
    orchestrator = FailoverOrchestrator(
        breakers=breakers,
        default_timeout_ms=settings.default_timeout_ms,
        failover_budget_ms=settings.failover_budget_ms,
    )
    key_router = KeyRouter(reg, settings=settings)
    database = db or Database(settings.db_path)
    ledger = UsageLedger(database)
    rate_limiter = TokenBucketLimiter(
        capacity=settings.rate_capacity,
        refill_per_s=settings.rate_refill_per_s,
        cost_per_req=settings.rate_cost_per_req,
    )
    metrics = MetricsRegistry()

    app.state.settings = settings
    app.state.registry = reg
    app.state.breakers = breakers
    app.state.orchestrator = orchestrator
    app.state.key_router = key_router
    app.state.db = database
    app.state.ledger = ledger
    app.state.rate_limiter = rate_limiter
    app.state.metrics = metrics

    app.include_router(chat_api.router)
    app.include_router(health_api.router)
    app.include_router(panel_router)

    return app


app = create_app()

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
from app.api.errors import RequestBoundaryMiddleware, install_openai_exception_handlers
from app.auth import assert_secure_admin_token
from app.config import Settings, get_settings
from app.db import StorageBundle, build_sqlite_storage_bundle
from app.db.database import Database, set_db
from app.db.ledger import UsageLedger
from app.failover.circuit_breaker import CircuitBreakerRegistry
from app.failover.orchestrator import FailoverOrchestrator
from app.metrics.audit import AuditLog
from app.metrics.prometheus import MetricsRegistry
from app.metrics.request_id import RequestIdMiddleware
from app.panel import router as panel_router
from app.providers.registry import ProviderRegistry, build_default_registry
from app.ratelimit.token_bucket import TokenBucketLimiter
from app.routing.key_router import KeyRouter

logger = logging.getLogger("llm_router")

# Default SQLite reliability knobs (Database.connect may also apply these).
_SQLITE_BUSY_TIMEOUT_MS = 5000


async def _sync_env_keys(db: Database, settings: Settings) -> None:
    """同步环境密钥，并撤销已从环境变量移除的环境托管密钥。"""
    await db.sync_environment_keys(settings.parsed_api_keys())


async def _ensure_sqlite_reliability(database: Database) -> None:
    """
    Apply WAL / busy_timeout / synchronous after connect.

    Preferred long-term home is Database.connect() (DB owner). Idempotent when
    both sites set the same pragmas. No-op for non-SQLite injected backends.
    """
    if not getattr(database, "connected", False):
        return
    if not hasattr(database, "execute"):
        return
    try:
        await database.execute("PRAGMA journal_mode=WAL")
        await database.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        await database.execute("PRAGMA synchronous=NORMAL")
    except Exception as exc:  # noqa: BLE001 — non-SQLite or closed handle
        logger.debug("sqlite reliability pragmas skipped: %s", exc)


def create_app(
    settings: Settings | None = None,
    *,
    registry: ProviderRegistry | None = None,
    db: Database | None = None,
    storage: StorageBundle | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Reject insecure admin tokens outside development before serving traffic.
        assert_secure_admin_token(app.state.settings)
        app.state.started_at = time.time()
        database: Database | None = getattr(app.state, "db", None)
        if database is not None and hasattr(database, "connect"):
            await database.connect()
            set_db(database)
            await _ensure_sqlite_reliability(database)
            await _sync_env_keys(database, app.state.settings)
            router: KeyRouter = app.state.key_router
            router.bind_db(database)
            linked = await router.hydrate_from_db()
        else:
            linked = 0
        storage_bundle: StorageBundle | None = getattr(app.state, "storage", None)
        backend = getattr(storage_bundle, "backend", "n/a") if storage_bundle else "n/a"
        logger.info(
            "llm-router %s starting (db=%s, storage=%s, env_keys_linked=%s, env=%s)",
            __version__,
            getattr(database, "path", None),
            backend,
            linked,
            getattr(app.state.settings, "env", "production"),
        )
        yield
        reg: ProviderRegistry = app.state.registry
        await reg.aclose()
        router = app.state.key_router
        router.bind_db(None)
        if database is not None and hasattr(database, "close"):
            await database.close()
        set_db(None)
        logger.info("llm-router shutdown complete")

    app = FastAPI(
        title="llm-router",
        version=__version__,
        description="OpenAI-compatible asyncio FastAPI LLM gateway with multi-provider failover",
        lifespan=lifespan,
    )
    install_openai_exception_handlers(app)
    app.add_middleware(RequestIdMiddleware)
    # Body/schema/concurrency limits — explicit wire (same pattern as RequestId).
    # Do not auto-install from exception-handler setup: that cancel-scope path
    # previously aborted stream usage accounting on client disconnect.
    app.add_middleware(RequestBoundaryMiddleware)

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
    database = db
    if storage is not None:
        bundle = storage
        ledger = bundle.usage
        # Keep a Database handle for panel/health SQL when the default SQLite
        # adapter is in use; memory-only bundles leave db unset.
        if database is None and bundle.backend == "sqlite":
            # Prefer concrete Database when api_keys port is one.
            if isinstance(bundle.api_keys, Database):
                database = bundle.api_keys
    else:
        database = database or Database(settings.db_path)
        ledger = UsageLedger(database)
        bundle = build_sqlite_storage_bundle(database, ledger=ledger)

    rate_limiter = TokenBucketLimiter(
        capacity=settings.rate_capacity,
        refill_per_s=settings.rate_refill_per_s,
        cost_per_req=settings.rate_cost_per_req,
    )
    metrics = MetricsRegistry()
    audit = AuditLog()

    app.state.settings = settings
    app.state.registry = reg
    app.state.breakers = breakers
    app.state.orchestrator = orchestrator
    app.state.key_router = key_router
    app.state.db = database
    app.state.ledger = ledger
    app.state.storage = bundle
    app.state.rate_limiter = rate_limiter
    app.state.metrics = metrics
    app.state.audit = audit

    app.include_router(chat_api.router)
    app.include_router(health_api.router)
    app.include_router(panel_router)

    return app


app = create_app()

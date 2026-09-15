"""StorageBundle dependency injection — SQLite default, memory seam, no Redis/PG claims."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import build_sqlite_storage_bundle
from app.db.database import Database
from app.db.ledger import UsageLedger
from app.db.storage import (
    ApiKeyStore,
    InMemoryStorage,
    PriceStore,
    StorageBundle,
    UsageStore,
)
from app.main import create_app
from app.models import ApiKeyRecord
from app.providers.registry import ProviderRegistry
from app.ratelimit.token_bucket import TokenBucketLimiter
from app.routing.key_router import KeyRouter
from tests.conftest import FakeProvider


@pytest.mark.asyncio
async def test_sqlite_bundle_runtime_protocol_checks(tmp_path) -> None:
    db = Database(str(tmp_path / "bundle.db"))
    await db.connect()
    ledger = UsageLedger(db)
    bundle = build_sqlite_storage_bundle(db, ledger=ledger)

    assert bundle.backend == "sqlite"
    assert isinstance(bundle.api_keys, ApiKeyStore)
    assert isinstance(bundle.prices, PriceStore)
    assert isinstance(bundle.usage, UsageStore)
    assert isinstance(db, ApiKeyStore)
    assert isinstance(db, PriceStore)
    assert isinstance(ledger, UsageStore)

    key_id = await bundle.api_keys.upsert_api_key(raw_key="sk-b", name="b")
    await bundle.prices.set_price("deepseek-chat", 1.0, 2.0, version="v1")
    row = await bundle.usage.record(
        api_key_id=key_id,
        provider_id="deepseek",
        model="deepseek-chat",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        latency_ms=12,
        status="ok",
        accounting_status="actual",
        ttfb_ms=3,
    )
    assert row["cost_usd"] == pytest.approx(1.0)
    assert row["ttfb_ms"] == 3
    await db.close()


@pytest.mark.asyncio
async def test_memory_bundle_same_contract() -> None:
    bundle = StorageBundle.from_memory()
    assert bundle.backend == "memory"
    assert isinstance(bundle.api_keys, ApiKeyStore)
    assert isinstance(bundle.prices, PriceStore)
    assert isinstance(bundle.usage, UsageStore)

    key_id = await bundle.api_keys.upsert_api_key(
        raw_key="sk-m", name="m", source="panel", update_active=True
    )
    # update_active=False must not reactivate
    await bundle.api_keys.upsert_api_key(
        raw_key="sk-m", name="m2", is_active=False, update_active=True
    )
    assert await bundle.api_keys.get_api_key_id_by_raw("sk-m") is None
    await bundle.api_keys.upsert_api_key(
        raw_key="sk-m", name="m3", is_active=True, update_active=False
    )
    assert await bundle.api_keys.get_api_key_id_by_raw("sk-m") is None
    await bundle.api_keys.upsert_api_key(
        raw_key="sk-m", name="m4", is_active=True, update_active=True
    )
    assert await bundle.api_keys.get_api_key_id_by_raw("sk-m") == key_id

    await bundle.prices.set_price("demo", 2.0, 4.0, version="v1")
    row = await bundle.usage.record(
        api_key_id=key_id,
        provider_id="deepseek",
        model="demo",
        prompt_tokens=500_000,
        completion_tokens=500_000,
        latency_ms=8,
        ttfb_ms=2,
        status="partial",
        accounting_status="actual",
    )
    assert row["status"] == "partial"
    assert row["ttfb_ms"] == 2
    assert row["cost_usd"] == pytest.approx(3.0)


def test_create_app_injects_sqlite_storage_bundle(test_settings) -> None:
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-", "deepseek"])])
    app = create_app(test_settings, registry=reg)
    assert isinstance(app.state.storage, StorageBundle)
    assert app.state.storage.backend == "sqlite"
    assert app.state.ledger is app.state.storage.usage
    assert app.state.db is not None

    app.state.rate_limiter = TokenBucketLimiter(capacity=100, refill_per_s=10)
    app.state.key_router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo-key")],
    )

    with TestClient(app) as client:
        # health path sees db via app.state.db (SQLite default)
        h = client.get("/health/ready")
        assert h.status_code == 200
        assert h.json()["db"] == "ok"

        # chat → usage via StorageBundle.usage
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert r.status_code == 200

        # usage landed via StorageBundle.usage (SQLite ledger)
        import sqlite3

        conn = sqlite3.connect(test_settings.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT provider_id, status FROM usage_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["provider_id"] == "deepseek"


def test_create_app_accepts_memory_storage_injection(test_settings) -> None:
    mem = InMemoryStorage()
    bundle = StorageBundle.from_memory(mem)
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-", "deepseek"])])
    # Keep a real SQLite db for panel/health/auth hash paths; inject memory usage port
    # by swapping after create — full memory-only skips SQL panel.
    app = create_app(test_settings, registry=reg)
    app.state.storage = bundle
    app.state.ledger = bundle.usage
    app.state.rate_limiter = TokenBucketLimiter(capacity=100, refill_per_s=10)
    app.state.key_router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo-key")],
    )

    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
        assert r.status_code == 200

    assert len(mem._events) >= 1
    assert mem._events[-1]["status"] in {"ok", "failover"}


def test_no_redis_postgres_backend_claimed() -> None:
    import app.db as db_pkg
    import app.db.storage as storage_mod

    assert StorageBundle.from_memory().backend == "memory"
    assert "redis" not in storage_mod.__dict__
    assert "psycopg" not in storage_mod.__dict__
    assert "asyncpg" not in storage_mod.__dict__
    assert not hasattr(db_pkg, "RedisStorage")
    assert not hasattr(db_pkg, "PostgresStorage")

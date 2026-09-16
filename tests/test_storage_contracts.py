"""Storage ports, SQLite default, price versioning, in-memory seam."""

from __future__ import annotations

import pytest

from app.db.database import Database
from app.db.ledger import UsageLedger
from app.db.storage import ApiKeyStore, InMemoryStorage, PriceStore, UsageStore


@pytest.mark.asyncio
async def test_inmemory_satisfies_storage_ports() -> None:
    store = InMemoryStorage()
    assert isinstance(store, ApiKeyStore)
    assert isinstance(store, PriceStore)
    assert isinstance(store, UsageStore)

    key_id = await store.upsert_api_key(raw_key="sk-mem", name="mem")
    assert await store.get_api_key_id_by_raw("sk-mem") == key_id

    quote = await store.set_price("demo-model", 1.0, 2.0, version="v1")
    assert quote.version == "v1"
    got = await store.get_price("demo-model")
    assert got is not None
    assert got.input_per_1m_usd == 1.0

    row = await store.record(
        api_key_id=key_id,
        provider_id="deepseek",
        model="demo-model",
        prompt_tokens=1_000_000,
        completion_tokens=500_000,
        status="ok",
        accounting_status="actual",
    )
    assert row["cost_usd"] == pytest.approx(2.0)  # 1*1 + 0.5*2
    assert row["price_version"] == "v1"
    assert row["input_price_per_1m_usd"] == 1.0
    assert row["output_price_per_1m_usd"] == 2.0
    assert row["accounting_status"] == "actual"


@pytest.mark.asyncio
async def test_sqlite_database_implements_price_store(tmp_path) -> None:
    db = Database(str(tmp_path / "prices.db"))
    await db.connect()
    assert isinstance(db, PriceStore)
    assert isinstance(db, ApiKeyStore)

    ids = await db.list_model_ids()
    assert "deepseek-chat" in ids

    q1 = await db.get_price("deepseek-chat")
    assert q1 is not None
    v1 = q1.version

    q2 = await db.set_price("deepseek-chat", 9.0, 18.0)
    assert q2.version != v1
    assert q2.input_per_1m_usd == 9.0

    await db.close()


@pytest.mark.asyncio
async def test_price_change_does_not_rewrite_prior_costs(tmp_path) -> None:
    db = Database(str(tmp_path / "ledger.db"))
    await db.connect()
    ledger = UsageLedger(db)
    assert isinstance(ledger, UsageStore)

    await db.set_price("deepseek-chat", 1.0, 2.0, version="v1")
    key_id = await db.upsert_api_key(raw_key="sk-p", name="p")

    old = await ledger.record(
        api_key_id=key_id,
        provider_id="deepseek",
        model="deepseek-chat",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        status="ok",
        accounting_status="actual",
    )
    assert old["cost_usd"] == pytest.approx(1.0)
    assert old["price_version"] == "v1"
    assert old["input_price_per_1m_usd"] == 1.0

    await db.set_price("deepseek-chat", 100.0, 200.0)  # bumps version
    recent = await ledger.recent(limit=10)
    # Prior row unchanged
    prior = next(r for r in recent if r["id"] == old["id"])
    assert prior["cost_usd"] == pytest.approx(1.0)
    assert prior["price_version"] == "v1"
    assert prior["input_price_per_1m_usd"] == 1.0

    new = await ledger.record(
        api_key_id=key_id,
        provider_id="deepseek",
        model="deepseek-chat",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        status="ok",
        accounting_status="actual",
    )
    assert new["cost_usd"] == pytest.approx(100.0)
    assert new["price_version"] != "v1"
    assert new["input_price_per_1m_usd"] == 100.0

    await db.close()


@pytest.mark.asyncio
async def test_price_history_returns_quote_effective_at_requested_time(tmp_path) -> None:
    db = Database(str(tmp_path / "price_history.db"))
    await db.connect()

    await db.set_price(
        "history-model",
        1.0,
        2.0,
        version="v1",
        effective_from="2026-01-01T00:00:00+00:00",
    )
    await db.set_price(
        "history-model",
        3.0,
        4.0,
        version="v2",
        effective_from="2026-06-01T00:00:00+00:00",
    )

    earlier = await db.get_price("history-model", at="2026-03-01T00:00:00+00:00")
    later = await db.get_price("history-model", at="2026-09-01T00:00:00+00:00")
    assert earlier is not None and earlier.version == "v1"
    assert earlier.input_per_1m_usd == 1.0
    assert later is not None and later.version == "v2"
    assert later.input_per_1m_usd == 3.0

    await db.close()


@pytest.mark.asyncio
async def test_unavailable_accounting_zero_tokens_and_cost(tmp_path) -> None:
    db = Database(str(tmp_path / "unavail.db"))
    await db.connect()
    ledger = UsageLedger(db)
    row = await ledger.record(
        api_key_id=None,
        provider_id="deepseek",
        model="deepseek-chat",
        prompt_tokens=99,
        completion_tokens=99,
        status="ok",
        accounting_status="unavailable",
    )
    assert row["prompt_tokens"] == 0
    assert row["completion_tokens"] == 0
    assert row["cost_usd"] == 0.0
    assert row["accounting_status"] == "unavailable"
    # Price version still snapshotted for auditability
    assert row["price_version"] is not None
    await db.close()


@pytest.mark.asyncio
async def test_estimated_accounting_persists_tokens(tmp_path) -> None:
    db = Database(str(tmp_path / "est.db"))
    await db.connect()
    ledger = UsageLedger(db)
    await db.set_price("deepseek-chat", 1.0, 1.0, version="v1")
    row = await ledger.record(
        api_key_id=None,
        provider_id="deepseek",
        model="deepseek-chat",
        prompt_tokens=1000,
        completion_tokens=0,
        status="ok",
        accounting_status="estimated",
    )
    assert row["accounting_status"] == "estimated"
    assert row["prompt_tokens"] == 1000
    assert row["cost_usd"] > 0
    await db.close()


def test_no_false_claim_of_redis_postgres() -> None:
    """Extension points exist; production adapters for Redis/PG are not shipped."""
    import app.db.storage as storage_mod

    assert hasattr(storage_mod, "InMemoryStorage")
    assert hasattr(storage_mod, "ApiKeyStore")
    assert hasattr(storage_mod, "PriceStore")
    assert hasattr(storage_mod, "UsageStore")
    # Honest: no Redis/PostgreSQL client modules imported by the seam module.
    assert "redis" not in getattr(storage_mod, "__dict__", {})
    assert "psycopg" not in getattr(storage_mod, "__dict__", {})
    assert "asyncpg" not in getattr(storage_mod, "__dict__", {})

"""SQLite WAL / busy_timeout pragmas + bounded ledger write retry."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from app.db.database import Database
from app.db.ledger import UsageLedger, is_retryable_db_error
from app.main import _SQLITE_BUSY_TIMEOUT_MS, _ensure_sqlite_reliability, create_app


def test_is_retryable_db_error_matrix() -> None:
    assert is_retryable_db_error(sqlite3.OperationalError("database is locked"))
    assert is_retryable_db_error(sqlite3.OperationalError("database is busy"))
    assert not is_retryable_db_error(sqlite3.OperationalError("no such table: x"))
    assert not is_retryable_db_error(ValueError("database is locked"))


@pytest.mark.asyncio
async def test_sqlite_pragmas_wal_busy_timeout_synchronous(tmp_path) -> None:
    db = Database(str(tmp_path / "pragma.db"))
    await db.connect()
    await _ensure_sqlite_reliability(db)

    mode = await db.fetchone("PRAGMA journal_mode")
    assert mode is not None
    # PRAGMA journal_mode returns one column (journal_mode)
    mode_val = str(mode[0] if not hasattr(mode, "keys") else mode[0]).lower()
    assert mode_val == "wal"

    busy = await db.fetchone("PRAGMA busy_timeout")
    assert busy is not None
    busy_val = int(busy[0])
    assert busy_val >= _SQLITE_BUSY_TIMEOUT_MS

    sync = await db.fetchone("PRAGMA synchronous")
    assert sync is not None
    # NORMAL == 1
    assert int(sync[0]) == 1

    await db.close()


@pytest.mark.asyncio
async def test_sqlite_restart_reopen_wal_recovery(tmp_path) -> None:
    path = str(tmp_path / "restart.db")
    db = Database(path)
    await db.connect()
    await _ensure_sqlite_reliability(db)
    ledger = UsageLedger(db)
    row = await ledger.record(
        api_key_id=None,
        provider_id="deepseek",
        model="deepseek-chat",
        prompt_tokens=1,
        completion_tokens=1,
        latency_ms=5,
        status="ok",
        accounting_status="actual",
        ttfb_ms=2,
    )
    assert row["id"] >= 1
    await db.close()

    db2 = Database(path)
    await db2.connect()
    await _ensure_sqlite_reliability(db2)
    mode = await db2.fetchone("PRAGMA journal_mode")
    assert str(mode[0]).lower() == "wal"
    ledger2 = UsageLedger(db2)
    recent = await ledger2.recent(limit=5)
    assert any(r["id"] == row["id"] for r in recent)
    await db2.close()


@pytest.mark.asyncio
async def test_ledger_retries_on_locked_then_succeeds(tmp_path) -> None:
    db = Database(str(tmp_path / "retry.db"))
    await db.connect()
    ledger = UsageLedger(db, write_retries=4, retry_base_delay_s=0.001)

    real_execute = db.execute
    calls = {"n": 0}

    async def flaky_execute(sql: str, params: Any = ()) -> Any:
        calls["n"] += 1
        # Fail first two writes that look like INSERTs into usage_events
        if "INSERT INTO usage_events" in sql and calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return await real_execute(sql, params)

    db.execute = flaky_execute  # type: ignore[method-assign]

    row = await ledger.record(
        api_key_id=None,
        provider_id="deepseek",
        model="deepseek-chat",
        prompt_tokens=3,
        completion_tokens=1,
        latency_ms=10,
        status="ok",
        accounting_status="actual",
        ttfb_ms=4,
    )
    assert row["id"] >= 1
    assert calls["n"] >= 3  # ensure + failed inserts + success (ensure may also call execute)
    await db.close()


@pytest.mark.asyncio
async def test_ledger_retry_exhausted_raises(tmp_path) -> None:
    db = Database(str(tmp_path / "fail.db"))
    await db.connect()
    ledger = UsageLedger(db, write_retries=3, retry_base_delay_s=0.0)
    assert await ledger._has_ttfb_column() is True

    async def always_locked(sql: str, params: Any = ()) -> Any:
        raise sqlite3.OperationalError("database is locked")

    db.execute = always_locked  # type: ignore[method-assign]

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        await ledger.record(
            api_key_id=None,
            provider_id="deepseek",
            model="deepseek-chat",
            status="ok",
        )
    await db.close()


@pytest.mark.asyncio
async def test_concurrent_ledger_writes_no_lock_errors(tmp_path) -> None:
    db = Database(str(tmp_path / "conc.db"))
    await db.connect()
    await _ensure_sqlite_reliability(db)
    ledger = UsageLedger(db, write_retries=8, retry_base_delay_s=0.005)

    async def one(i: int) -> dict[str, Any]:
        return await ledger.record(
            api_key_id=None,
            provider_id="deepseek",
            model="deepseek-chat",
            prompt_tokens=i,
            completion_tokens=1,
            latency_ms=i,
            status="ok",
            accounting_status="actual",
            ttfb_ms=1,
            request_id=f"req-conc-{i}",
        )

    results = await asyncio.gather(*[one(i) for i in range(24)])
    assert len(results) == 24
    ids = {r["id"] for r in results}
    assert len(ids) == 24

    # Interleave a key mutation with more writes
    async def mutate_and_write(i: int) -> None:
        await db.upsert_api_key(raw_key=f"sk-conc-{i}", name=f"n{i}")
        await ledger.record(
            api_key_id=None,
            provider_id="openai",
            model="gpt-4o-mini",
            prompt_tokens=1,
            completion_tokens=1,
            latency_ms=1,
            status="ok",
            request_id=f"req-mut-{i}",
        )

    await asyncio.gather(*[mutate_and_write(i) for i in range(12)])
    recent = await ledger.recent(limit=50)
    assert len(recent) >= 24
    await db.close()


def test_create_app_applies_sqlite_reliability(test_settings) -> None:
    app = create_app(test_settings)
    with __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(app):
        # lifespan ran _ensure_sqlite_reliability
        pass

    conn = sqlite3.connect(test_settings.db_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    assert str(mode).lower() == "wal"
    assert int(busy) >= _SQLITE_BUSY_TIMEOUT_MS

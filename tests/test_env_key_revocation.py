"""Regression: environment key source metadata + revocation vs panel keys."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.db.database import Database, hash_api_key
from app.models import ApiKeyRecord
from app.providers.registry import ProviderRegistry
from app.routing.key_router import KeyRouter
from tests.conftest import FakeProvider


@pytest.mark.asyncio
async def test_sync_environment_keys_sets_source_and_revokes_removed(tmp_path) -> None:
    db = Database(str(tmp_path / "env_keys.db"))
    await db.connect()

    # Panel-managed key must survive env sync / removal.
    panel_id = await db.upsert_api_key(
        raw_key="sk-panel-keep",
        name="panel",
        source="panel",
        is_active=True,
    )

    first = await db.sync_environment_keys(
        [
            {"name": "env-a", "key": "sk-env-a"},
            {"name": "env-b", "key": "sk-env-b", "provider_id": "deepseek"},
        ]
    )
    assert first["upserted"] == 2
    assert first["deactivated"] == 0

    row_a = await db.fetchone(
        "SELECT source, is_active, name FROM api_keys WHERE key_hash = ?",
        (hash_api_key("sk-env-a"),),
    )
    row_b = await db.fetchone(
        "SELECT source, is_active, provider_id FROM api_keys WHERE key_hash = ?",
        (hash_api_key("sk-env-b"),),
    )
    assert row_a is not None and row_a["source"] == "env" and int(row_a["is_active"]) == 1
    assert row_b is not None and row_b["source"] == "env" and row_b["provider_id"] == "deepseek"

    # Remove env-b from the environment set.
    second = await db.sync_environment_keys([{"name": "env-a", "key": "sk-env-a"}])
    assert second["upserted"] == 1
    assert second["deactivated"] == 1

    row_b2 = await db.fetchone(
        "SELECT is_active, source FROM api_keys WHERE key_hash = ?",
        (hash_api_key("sk-env-b"),),
    )
    assert row_b2 is not None
    assert int(row_b2["is_active"]) == 0
    assert row_b2["source"] == "env"

    row_a2 = await db.fetchone(
        "SELECT is_active FROM api_keys WHERE key_hash = ?",
        (hash_api_key("sk-env-a"),),
    )
    assert row_a2 is not None and int(row_a2["is_active"]) == 1

    panel = await db.fetchone("SELECT is_active, source FROM api_keys WHERE id = ?", (panel_id,))
    assert panel is not None
    assert int(panel["is_active"]) == 1
    assert panel["source"] == "panel"
    assert await db.get_api_key_id_by_raw("sk-panel-keep") == panel_id
    assert await db.get_api_key_id_by_raw("sk-env-b") is None

    await db.close()


@pytest.mark.asyncio
async def test_upsert_does_not_reactivate_without_update_active(tmp_path) -> None:
    db = Database(str(tmp_path / "reactivate.db"))
    await db.connect()

    kid = await db.upsert_api_key(
        raw_key="sk-env-revoked",
        name="env",
        source="env",
        is_active=True,
    )
    await db.execute("UPDATE api_keys SET is_active = 0 WHERE id = ?", (kid,))

    # Mimic main._sync_env_keys / env re-assertion without update_active.
    await db.upsert_api_key(
        raw_key="sk-env-revoked",
        name="env",
        source="env",
        is_active=True,
        update_active=False,
    )
    row = await db.fetchone("SELECT is_active FROM api_keys WHERE id = ?", (kid,))
    assert row is not None and int(row["is_active"]) == 0

    # Explicit reactivation still works when requested.
    await db.upsert_api_key(
        raw_key="sk-env-revoked",
        name="env",
        source="env",
        is_active=True,
        update_active=True,
    )
    row2 = await db.fetchone("SELECT is_active FROM api_keys WHERE id = ?", (kid,))
    assert row2 is not None and int(row2["is_active"]) == 1

    await db.close()


@pytest.mark.asyncio
async def test_hydrate_tags_env_source_and_drops_revoked_hot_keys(
    test_settings: Settings, tmp_path
) -> None:
    db = Database(str(tmp_path / "hydrate.db"))
    await db.connect()

    await db.upsert_api_key(raw_key="sk-demo-key", name="demo", source="panel")
    await db.upsert_api_key(
        raw_key="sk-removed",
        name="gone",
        source="env",
        is_active=True,
    )
    # Panel revoke of an env-present key: stays inactive across hydrate.
    revoked_id = await db.upsert_api_key(
        raw_key="sk-forced",
        name="forced",
        source="env",
        provider_id="deepseek",
        is_active=True,
    )
    await db.execute("UPDATE api_keys SET is_active = 0 WHERE id = ?", (revoked_id,))

    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[
            ApiKeyRecord(name="demo", key="sk-demo-key"),
            ApiKeyRecord(name="forced", key="sk-forced", provider_id="deepseek"),
        ],
        db=db,
    )

    linked = await router.hydrate_from_db()
    assert linked == 1  # only active demo key

    demo_row = await db.fetchone(
        "SELECT source, is_active FROM api_keys WHERE key_hash = ?",
        (hash_api_key("sk-demo-key"),),
    )
    assert demo_row is not None
    assert demo_row["source"] == "env"
    assert int(demo_row["is_active"]) == 1

    removed = await db.fetchone(
        "SELECT is_active FROM api_keys WHERE key_hash = ?",
        (hash_api_key("sk-removed"),),
    )
    assert removed is not None and int(removed["is_active"]) == 0

    # Revoked env key must not remain on the hot path.
    assert router.authenticate("sk-forced") is None
    assert await router.authenticate_async("sk-forced") is None
    assert router.authenticate("sk-demo-key") is not None

    await db.close()


@pytest.mark.asyncio
async def test_source_column_migrates_on_legacy_db(tmp_path) -> None:
    """Additive migration: older api_keys tables without source still boot."""
    import aiosqlite

    path = str(tmp_path / "legacy.db")
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            """
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_hash TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                provider_id TEXT,
                model_allowlist TEXT,
                rate_capacity REAL,
                rate_refill_per_s REAL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key_id INTEGER,
                provider_id TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                request_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE model_prices (
                model TEXT PRIMARY KEY,
                input_per_1m_usd REAL NOT NULL DEFAULT 0,
                output_per_1m_usd REAL NOT NULL DEFAULT 0
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE providers (
                id TEXT PRIMARY KEY,
                base_url TEXT NOT NULL,
                api_key_enc TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                weight INTEGER NOT NULL DEFAULT 100,
                updated_at TEXT NOT NULL
            )
            """
        )
        await conn.commit()

    db = Database(path)
    await db.connect()
    cols = await db._table_columns("api_keys")  # noqa: SLF001
    assert "source" in cols

    kid = await db.upsert_api_key(raw_key="sk-legacy", name="legacy", source="env")
    row = await db.fetchone("SELECT source FROM api_keys WHERE id = ?", (kid,))
    assert row is not None and row["source"] == "env"
    await db.close()


@pytest.mark.asyncio
async def test_panel_key_preserved_when_all_env_keys_removed(tmp_path) -> None:
    db = Database(str(tmp_path / "panel_only.db"))
    await db.connect()

    await db.upsert_api_key(raw_key="sk-panel", name="panel", source="panel")
    await db.sync_environment_keys([{"name": "e", "key": "sk-env"}])
    await db.sync_environment_keys([])  # env emptied

    env_row = await db.fetchone(
        "SELECT is_active FROM api_keys WHERE key_hash = ?",
        (hash_api_key("sk-env"),),
    )
    panel_row = await db.fetchone(
        "SELECT is_active, source FROM api_keys WHERE key_hash = ?",
        (hash_api_key("sk-panel"),),
    )
    assert env_row is not None and int(env_row["is_active"]) == 0
    assert panel_row is not None
    assert int(panel_row["is_active"]) == 1
    assert panel_row["source"] == "panel"
    await db.close()

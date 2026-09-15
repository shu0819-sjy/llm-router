"""SQLite usage / cost ledger tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.database import Database
from app.db.ledger import UsageLedger, estimate_cost
from app.main import create_app
from app.models import ApiKeyRecord
from app.providers.registry import ProviderRegistry
from app.routing.key_router import KeyRouter
from tests.conftest import FakeProvider


def test_estimate_cost_formula() -> None:
    cost = estimate_cost(1_000_000, 500_000, 1.0, 2.0)
    assert cost == pytest.approx(1.0 + 1.0)


@pytest.mark.asyncio
async def test_ledger_records_tokens_and_cost(tmp_path) -> None:
    db = Database(str(tmp_path / "usage.db"))
    await db.connect()
    key_id = await db.upsert_api_key(raw_key="sk-u", name="user")
    ledger = UsageLedger(db)
    row = await ledger.record(
        api_key_id=key_id,
        provider_id="deepseek",
        model="deepseek-chat",
        prompt_tokens=1000,
        completion_tokens=500,
        latency_ms=42,
        status="ok",
    )
    assert row["total_tokens"] == 1500
    assert row["cost_usd"] > 0
    assert row["provider_id"] == "deepseek"
    recent = await ledger.recent(limit=10)
    assert recent and recent[0]["id"] == row["id"]
    await db.close()


def test_chat_writes_usage_event(test_settings) -> None:
    reg = ProviderRegistry(
        [
            FakeProvider(
                "deepseek",
                ["deepseek-"],
                response={
                    "id": "chatcmpl-u",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "deepseek-chat",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 8,
                        "total_tokens": 20,
                    },
                },
            )
        ]
    )
    app = create_app(test_settings, registry=reg)
    app.state.key_router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo-key")],
    )
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        # Read via sync sqlite while lifespan holds the connection open
        import sqlite3

        conn = sqlite3.connect(test_settings.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT model, provider_id, prompt_tokens, completion_tokens, cost_usd, api_key_id "
            "FROM usage_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["model"] == "deepseek-chat"
        assert row["provider_id"] == "deepseek"
        assert row["prompt_tokens"] == 12
        assert row["completion_tokens"] == 8
        assert row["cost_usd"] >= 0
        assert row["api_key_id"] is not None

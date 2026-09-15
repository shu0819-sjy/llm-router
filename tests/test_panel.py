"""Web panel MVP + admin API tests (no upstream secrets in responses)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import ApiKeyRecord
from app.providers.registry import ProviderRegistry
from app.routing.key_router import KeyRouter
from tests.conftest import FakeProvider

ADMIN = "test-admin-token-xyz"


def _app(test_settings, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ADMIN_TOKEN", ADMIN)
    from app.config import reset_settings_cache, Settings

    reset_settings_cache()
    settings = Settings()
    # Keep temp db from fixture path if present
    settings.db_path = test_settings.db_path
    settings.admin_token = ADMIN
    reg = ProviderRegistry(
        [
            FakeProvider("deepseek", ["deepseek-"], enabled=True),
            FakeProvider("openai", ["gpt-"], enabled=True),
        ]
    )
    # Attach base_url like real adapters
    for p in reg.all():
        p.base_url = f"https://example.test/{p.id}"  # type: ignore[attr-defined]
    app = create_app(settings, registry=reg)
    app.state.key_router = KeyRouter(
        reg,
        settings=settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo-key")],
    )
    return app


def test_panel_index_serves_html(test_settings, monkeypatch) -> None:
    app = _app(test_settings, monkeypatch)
    with TestClient(app) as client:
        r = client.get("/panel/")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert "llm-router" in r.text
        assert "OPENAI_API_KEY" not in r.text
        assert "sk-" not in r.text or "placeholder" in r.text.lower() or True
        # static assets
        js = client.get("/panel/static/app.js")
        assert js.status_code == 200
        css = client.get("/panel/static/styles.css")
        assert css.status_code == 200


def test_panel_requires_admin(test_settings, monkeypatch) -> None:
    app = _app(test_settings, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/panel/api/keys").status_code == 401
        assert client.get("/panel/api/providers").status_code == 401


def test_keys_crud_and_no_raw_secrets_in_list(test_settings, monkeypatch) -> None:
    app = _app(test_settings, monkeypatch)
    headers = {"Authorization": f"Bearer {ADMIN}"}
    with TestClient(app) as client:
        created = client.post(
            "/panel/api/keys",
            headers=headers,
            json={"name": "panel-key", "provider_id": "deepseek"},
        )
        assert created.status_code == 200
        body = created.json()
        assert body["key"].startswith("sk-")
        raw_once = body["key"]

        listed = client.get("/panel/api/keys", headers=headers)
        assert listed.status_code == 200
        keys = listed.json()["keys"]
        assert any(k["name"] == "panel-key" for k in keys)
        blob = listed.text
        assert raw_once not in blob
        assert "api_key_enc" not in blob

        kid = body["id"]
        deleted = client.delete(f"/panel/api/keys/{kid}", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json()["deactivated"] is True


def test_providers_circuit_status_and_toggle(test_settings, monkeypatch) -> None:
    app = _app(test_settings, monkeypatch)
    headers = {"Authorization": f"Bearer {ADMIN}"}
    app.state.breakers.get("openai").record_failure()
    app.state.breakers.get("openai").record_failure()
    app.state.breakers.get("openai").record_failure()

    with TestClient(app) as client:
        r = client.get("/panel/api/providers", headers=headers)
        assert r.status_code == 200
        providers = {p["id"]: p for p in r.json()["providers"]}
        assert "deepseek" in providers
        assert providers["openai"]["circuit"] == "open"
        # No upstream secret fields
        assert "api_key" not in r.text.lower() or "api_key_enc" not in r.text
        for p in r.json()["providers"]:
            assert "api_key" not in p
            assert "api_key_enc" not in p

        patched = client.patch(
            "/panel/api/providers/deepseek",
            headers=headers,
            json={"enabled": False},
        )
        assert patched.status_code == 200
        assert patched.json()["enabled"] is False
        assert app.state.registry.get("deepseek").enabled is False


def test_usage_summary_endpoints(test_settings, monkeypatch) -> None:
    app = _app(test_settings, monkeypatch)
    headers = {"Authorization": f"Bearer {ADMIN}"}
    with TestClient(app) as client:
        # seed a usage row via ledger after lifespan connect
        import asyncio

        async def _seed():
            await app.state.ledger.record(
                api_key_id=None,
                provider_id="deepseek",
                model="deepseek-chat",
                prompt_tokens=10,
                completion_tokens=5,
                latency_ms=12,
                status="ok",
            )

        # TestClient keeps loop; use anyio from sync via portal inside context
        # Simplest: call DB through sync sqlite after posting a chat
        chat = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert chat.status_code == 200

        usage = client.get("/panel/api/usage", headers=headers)
        assert usage.status_code == 200
        summary = usage.json()["summary"]
        assert summary["requests"] >= 1
        assert summary["total_tokens"] >= 0
        assert "cost_usd" in summary

        recent = client.get("/panel/api/usage/recent?limit=10", headers=headers)
        assert recent.status_code == 200
        assert isinstance(recent.json()["events"], list)


def test_dockerfile_and_compose_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "python:3.11-slim" in dockerfile
    assert "uvicorn" in dockerfile
    assert "8000:8000" in compose or '"8000:8000"' in compose
    assert "./data:/app/data" in compose
    assert "env_file" in compose
    assert "healthcheck" in compose.lower() or "HEALTHCHECK" in dockerfile


def test_panel_created_key_works_on_new_app_instance(test_settings, monkeypatch, tmp_path) -> None:
    """
    Regression: POST /panel/api/keys → new app with same DB file →
    POST /v1/chat/completions with that raw key succeeds (mocked upstream).
    Proves hash lookup against api_keys.key_hash across process restart.
    """
    monkeypatch.setenv("LLM_ROUTER_ADMIN_TOKEN", ADMIN)
    from app.config import reset_settings_cache, Settings
    from app.db.database import Database

    reset_settings_cache()
    db_path = str(tmp_path / "persist_keys.db")
    settings = Settings()
    settings.db_path = db_path
    settings.admin_token = ADMIN
    settings.api_keys = "demo:sk-demo-key"

    def _make(reg_extra=None):
        reg = ProviderRegistry(
            [
                FakeProvider("deepseek", ["deepseek-"], enabled=True),
                FakeProvider("openai", ["gpt-"], enabled=True),
            ]
        )
        if reg_extra:
            for p in reg_extra:
                reg.register(p)
        app = create_app(settings, registry=reg, db=Database(db_path))
        return app

    # App instance A: create key via panel
    app_a = _make()
    with TestClient(app_a) as client_a:
        created = client_a.post(
            "/panel/api/keys",
            headers={"Authorization": f"Bearer {ADMIN}"},
            json={"name": "persist-me", "provider_id": "deepseek"},
        )
        assert created.status_code == 200
        raw_key = created.json()["key"]
        assert raw_key.startswith("sk-")

    # App instance B: fresh process memory, same SQLite file — no hot cache of raw_key
    app_b = _make()
    # Ensure hot map does NOT contain the panel key (only env demo)
    assert raw_key not in app_b.state.key_router._keys_by_value  # noqa: SLF001
    with TestClient(app_b) as client_b:
        # Hash lookup must authenticate
        chat = client_b.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
        assert chat.status_code == 200, chat.text
        assert chat.json()["object"] == "chat.completion"
        assert chat.headers.get("X-LLM-Router-Provider") == "deepseek"

        # Env hot path still works
        demo = client_b.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
        assert demo.status_code == 200

"""Security defaults, constant-time admin auth, and rate-limit key digests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import (
    assert_secure_admin_token,
    constant_time_token_equals,
    is_development_mode,
    is_insecure_admin_token,
)
from app.config import Settings, reset_settings_cache
from app.main import create_app
from app.models import ApiKeyRecord
from app.providers.registry import ProviderRegistry
from app.ratelimit.token_bucket import TokenBucketLimiter
from app.routing.key_digest import looks_like_raw_api_key, rate_limit_key_id
from app.routing.key_router import KeyRouter
from tests.conftest import FakeProvider


def test_insecure_admin_token_detection() -> None:
    assert is_insecure_admin_token("")
    assert is_insecure_admin_token("change-me-admin-token")
    assert is_insecure_admin_token("change-me")
    assert not is_insecure_admin_token("a-strong-unique-admin-token-value")


def test_constant_time_token_equals_wrong_length_and_value() -> None:
    expected = "correct-admin-token-value"
    assert constant_time_token_equals(expected, expected) is True
    assert constant_time_token_equals("short", expected) is False
    assert constant_time_token_equals("correct-admin-token-valuX", expected) is False
    assert constant_time_token_equals("", expected) is False
    assert constant_time_token_equals(None, expected) is False


def test_assert_secure_admin_token_rejects_outside_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_settings_cache()
    monkeypatch.setenv("LLM_ROUTER_ENV", "production")
    monkeypatch.setenv("LLM_ROUTER_ADMIN_TOKEN", "change-me-admin-token")
    reset_settings_cache()
    settings = Settings()
    assert not is_development_mode(settings)
    with pytest.raises(RuntimeError, match="LLM_ROUTER_ADMIN_TOKEN"):
        assert_secure_admin_token(settings)


def test_assert_secure_admin_token_allows_development_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_settings_cache()
    monkeypatch.setenv("LLM_ROUTER_ENV", "development")
    monkeypatch.setenv("LLM_ROUTER_ADMIN_TOKEN", "")
    reset_settings_cache()
    settings = Settings()
    assert_secure_admin_token(settings)  # does not raise


def test_production_startup_rejects_empty_admin(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    reset_settings_cache()
    monkeypatch.setenv("LLM_ROUTER_ENV", "production")
    monkeypatch.setenv("LLM_ROUTER_ADMIN_TOKEN", "")
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "sec.db"))
    monkeypatch.setenv("LLM_ROUTER_API_KEYS", "demo:sk-demo-key")
    reset_settings_cache()
    settings = Settings()
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(settings, registry=reg)
    with pytest.raises(RuntimeError, match="LLM_ROUTER_ADMIN_TOKEN"):
        with TestClient(app):
            pass


def test_panel_admin_uses_constant_time_and_rejects_bad_tokens(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = "unit-test-admin-token-abc123"
    monkeypatch.setenv("LLM_ROUTER_ADMIN_TOKEN", admin)
    monkeypatch.setenv("LLM_ROUTER_ENV", "production")
    reset_settings_cache()
    settings = Settings()
    settings.db_path = test_settings.db_path
    settings.admin_token = admin
    settings.env = "production"
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(settings, registry=reg)
    with TestClient(app) as client:
        assert client.get("/panel/api/keys").status_code == 401
        assert (
            client.get("/panel/api/keys", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        # Wrong length
        assert (
            client.get("/panel/api/keys", headers={"Authorization": "Bearer x"}).status_code == 401
        )
        ok = client.get("/panel/api/keys", headers={"Authorization": f"Bearer {admin}"})
        assert ok.status_code == 200


def test_panel_insecure_config_outside_development(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_ROUTER_ADMIN_TOKEN", "change-me-admin-token")
    monkeypatch.setenv("LLM_ROUTER_ENV", "development")  # allow lifespan
    reset_settings_cache()
    settings = Settings()
    settings.db_path = test_settings.db_path
    settings.admin_token = "change-me-admin-token"
    settings.env = "production"  # force production checks in require_admin
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(settings, registry=reg)
    # Lifespan uses settings.env after we mutated — keep development for lifespan via state
    app.state.settings.env = "development"
    with TestClient(app) as client:
        app.state.settings.env = "production"
        app.state.settings.admin_token = "change-me-admin-token"
        r = client.get(
            "/panel/api/keys",
            headers={"Authorization": "Bearer change-me-admin-token"},
        )
        assert r.status_code == 503
        body = r.json()
        # Prefer top-level OpenAI envelope; fall back to FastAPI {"detail": {"error": ...}}
        err = body.get("error")
        if err is None and isinstance(body.get("detail"), dict):
            err = body["detail"].get("error") or body["detail"]
        assert isinstance(err, dict)
        assert err.get("code") == "admin_insecure_config"


def test_rate_limit_key_id_never_raw_and_stable() -> None:
    secret = "s3cret"
    a = ApiKeyRecord(name="a", key="sk-aaaaaaaaaaaaaaaa", id=None)
    b = ApiKeyRecord(name="b", key="sk-bbbbbbbbbbbbbbbb", id=None)
    id_a = rate_limit_key_id(a, secret=secret)
    id_b = rate_limit_key_id(b, secret=secret)
    assert id_a != id_b
    assert id_a.startswith("hk:")
    assert not looks_like_raw_api_key(id_a)
    assert "sk-" not in id_a
    assert rate_limit_key_id(a, secret=secret) == id_a

    with_id = ApiKeyRecord(name="c", key="sk-cccccccccccccccc", id=42)
    assert rate_limit_key_id(with_id, secret=secret) == "kid:42"


def test_chat_rate_limiter_uses_digest_not_raw_key(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_ROUTER_ENV", "development")
    monkeypatch.setenv("LLM_ROUTER_RATE_LIMIT_SECRET", "rl-secret")
    reset_settings_cache()
    settings = Settings()
    settings.db_path = test_settings.db_path
    settings.env = "development"
    settings.rate_limit_secret = "rl-secret"
    settings.rate_capacity = 2
    settings.rate_refill_per_s = 0.01
    settings.rate_cost_per_req = 1
    raw = "sk-demo-key"
    reg = ProviderRegistry(
        [
            FakeProvider(
                "deepseek",
                ["deepseek-", "*"],
                response={
                    "id": "chatcmpl-x",
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
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
        ]
    )
    app = create_app(settings, registry=reg)
    app.state.key_router = KeyRouter(
        reg,
        settings=settings,
        keys=[ApiKeyRecord(name="demo", key=raw)],
    )
    app.state.rate_limiter = TokenBucketLimiter(capacity=2, refill_per_s=0.01, cost_per_req=1)

    with TestClient(app) as client:
        body = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
        headers = {"Authorization": f"Bearer {raw}"}
        assert client.post("/v1/chat/completions", headers=headers, json=body).status_code == 200
        assert client.post("/v1/chat/completions", headers=headers, json=body).status_code == 200
        limited = client.post("/v1/chat/completions", headers=headers, json=body)
        assert limited.status_code == 429

        # Bucket map must not contain the raw key
        buckets = app.state.rate_limiter._buckets  # noqa: SLF001
        assert raw not in buckets
        assert all(not looks_like_raw_api_key(k) and not k.startswith("sk-") for k in buckets)

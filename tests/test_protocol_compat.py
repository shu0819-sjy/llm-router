"""OpenAI protocol compatibility: /v1/models, error envelopes, tool fields."""

from __future__ import annotations

from typing import Any
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import ApiKeyRecord, ChatRequest
from app.providers.base import Provider
from app.providers.registry import ProviderRegistry
from app.ratelimit.token_bucket import TokenBucketLimiter
from app.routing.key_router import KeyRouter
from tests.conftest import FakeProvider


class CapturingProvider(FakeProvider):
    """Records the ChatRequest seen by chat() for round-trip assertions."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.last_req: ChatRequest | None = None

    async def chat(self, req: ChatRequest, *, timeout_ms: int) -> dict[str, Any]:
        self.last_req = req
        return await super().chat(req, timeout_ms=timeout_ms)

    async def chat_stream(
        self, req: ChatRequest, *, timeout_ms: int
    ) -> AsyncIterator[bytes]:
        self.last_req = req
        async for c in super().chat_stream(req, timeout_ms=timeout_ms):
            yield c


def _app_with(provider: Provider, test_settings, *, key: str = "sk-demo-key"):
    from app.api.errors import install_openai_exception_handlers

    reg = ProviderRegistry([provider])
    app = create_app(test_settings, registry=reg)
    # Ensure OpenAI envelopes are active for this app (also installed lazily via
    # /v1 router dependency; explicit install covers 422 body-validation paths).
    install_openai_exception_handlers(app)
    app.state.rate_limiter = TokenBucketLimiter(capacity=100, refill_per_s=10)
    app.state.key_router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key=key)],
    )
    return app


def test_v1_models_openai_shape(test_settings) -> None:
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = _app_with(FakeProvider("deepseek", ["deepseek-"]), test_settings)
    # silence unused
    _ = reg
    with TestClient(app) as client:
        resp = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer sk-demo-key"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "list"
        assert isinstance(body["data"], list)
        assert body["data"], "expected at least one model"
        card = body["data"][0]
        assert card["object"] == "model"
        assert "id" in card
        assert "owned_by" in card


def test_v1_models_requires_auth(test_settings) -> None:
    app = _app_with(FakeProvider("deepseek", ["deepseek-"]), test_settings)
    with TestClient(app) as client:
        resp = client.get("/v1/models")
        assert resp.status_code == 401
        body = resp.json()
        assert "error" in body
        assert "message" in body["error"]
        assert "detail" not in body  # OpenAI envelope, not FastAPI-wrapped


def test_v1_models_respects_forced_provider(test_settings) -> None:
    deepseek = FakeProvider("deepseek", ["deepseek-"])
    openai = FakeProvider("openai", ["gpt-"])
    reg = ProviderRegistry([deepseek, openai])
    app = create_app(test_settings, registry=reg)
    app.state.key_router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="forced", key="sk-forced", provider_id="deepseek")],
    )

    with TestClient(app) as client:
        resp = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer sk-forced"},
        )
        assert resp.status_code == 200
        model_ids = {item["id"] for item in resp.json()["data"]}
        assert model_ids
        assert all(model_id.startswith("deepseek-") for model_id in model_ids)


def test_validation_error_openai_envelope(test_settings) -> None:
    from app.api.errors import install_openai_exception_handlers

    app = _app_with(FakeProvider("deepseek", ["deepseek-"]), test_settings)
    # Request body validation runs before router deps; install handlers explicitly
    # (production can also call this from create_app — kept out of main.py scope here).
    install_openai_exception_handlers(app)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={"messages": [{"role": "user", "content": "hi"}]},  # missing model
        )
        assert resp.status_code == 422
        body = resp.json()
        assert "error" in body
        assert body["error"]["type"] == "invalid_request_error"
        assert "detail" not in body


def test_http_exception_unwrap_unit() -> None:
    """Handlers convert FastAPI HTTPException detail={error:...} to top-level error."""
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient as TC

    from app.api.errors import install_openai_exception_handlers

    app = FastAPI()
    install_openai_exception_handlers(app)

    @app.get("/boom")
    def boom() -> None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "nope",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        )

    with TC(app) as client:
        resp = client.get("/boom")
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["code"] == "invalid_api_key"
        assert "detail" not in body


def test_tools_round_trip_openai_compat(test_settings) -> None:
    provider = CapturingProvider("deepseek", ["deepseek-"])
    app = _app_with(provider, test_settings)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": tools,
                "tool_choice": "auto",
                "response_format": {"type": "json_object"},
            },
        )
        assert resp.status_code == 200
        assert provider.last_req is not None
        assert provider.last_req.tools == tools
        assert provider.last_req.tool_choice == "auto"
        assert provider.last_req.response_format is not None


def test_tools_rejected_for_anthropic_only(test_settings) -> None:
    provider = CapturingProvider("anthropic", ["claude-"])
    app = _app_with(provider, test_settings)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={
                "model": "claude-3-haiku-20240307",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "x", "parameters": {"type": "object"}},
                    }
                ],
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "unsupported_parameter"
        assert "tools" in body["error"]["message"]

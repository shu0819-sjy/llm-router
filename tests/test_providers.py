"""Provider adapter unit tests (httpx MockTransport)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.models import ChatRequest
from app.providers.base import UpstreamError, UpstreamTimeout
from app.providers.claude import ClaudeProvider
from app.providers.deepseek import DeepSeekProvider
from app.providers.gpt import GPTProvider
from app.providers.qwen import QwenProvider
from app.providers.registry import ProviderRegistry, build_default_registry
from app.config import Settings


def _req(model: str = "gpt-4o-mini") -> ChatRequest:
    return ChatRequest(model=model, messages=[{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_gpt_provider_chat_success(openai_completion_json: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers.get("Authorization") == "Bearer sk-test"
        body = json.loads(request.content)
        assert body["model"] == "gpt-4o-mini"
        assert body["stream"] is False
        return httpx.Response(200, json=openai_completion_json)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://api.openai.com")
    provider = GPTProvider(api_key="sk-test", client=client)
    result = await provider.chat(_req(), timeout_ms=500)
    assert result["choices"][0]["message"]["content"] == "hello"
    assert provider.supports_model("gpt-4o")
    assert provider.supports_model("o1-preview")
    assert not provider.supports_model("claude-3")
    await provider.aclose()


@pytest.mark.asyncio
async def test_deepseek_and_qwen_openai_compat() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-x",
                "object": "chat.completion",
                "created": 1,
                "model": "m",
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

    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    ds = DeepSeekProvider(api_key="ds", base_url="https://api.deepseek.com", client=async_client)
    qw = QwenProvider(
        api_key="qw",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        client=async_client,
    )
    r1 = await ds.chat(_req("deepseek-chat"), timeout_ms=200)
    r2 = await qw.chat(_req("qwen-turbo"), timeout_ms=200)
    assert r1["choices"][0]["message"]["content"] == "ok"
    assert r2["choices"][0]["message"]["content"] == "ok"
    assert ds.supports_model("deepseek-chat")
    assert qw.supports_model("qwen-plus")
    await async_client.aclose()


@pytest.mark.asyncio
async def test_openai_compat_timeout_and_5xx() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler))
    p = GPTProvider(api_key="k", client=client)
    with pytest.raises(UpstreamTimeout):
        await p.chat(_req(), timeout_ms=50)
    await client.aclose()

    def err_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    client2 = httpx.AsyncClient(transport=httpx.MockTransport(err_handler))
    p2 = GPTProvider(api_key="k", client=client2)
    with pytest.raises(UpstreamError) as ei:
        await p2.chat(_req(), timeout_ms=50)
    assert ei.value.status_code == 503
    await client2.aclose()


@pytest.mark.asyncio
async def test_claude_maps_messages_to_openai_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/messages")
        assert request.headers.get("x-api-key") == "ak"
        body = json.loads(request.content)
        assert body["model"] == "claude-3-5-sonnet-latest"
        assert body["system"] == "be nice"
        assert body["messages"][0]["role"] == "user"
        return httpx.Response(
            200,
            json={
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "bonjour"}],
                "model": "claude-3-5-sonnet-latest",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = ClaudeProvider(api_key="ak", client=client)
    req = ChatRequest(
        model="claude-3-5-sonnet-latest",
        messages=[
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": "hi"},
        ],
    )
    result = await provider.chat(req, timeout_ms=300)
    assert result["object"] == "chat.completion"
    assert result["choices"][0]["message"]["content"] == "bonjour"
    assert result["usage"]["prompt_tokens"] == 10
    assert result["usage"]["completion_tokens"] == 3
    assert provider.supports_model("claude-3-opus")
    await provider.aclose()


def test_registry_match_longest_prefix(test_settings: Settings) -> None:
    reg = build_default_registry(test_settings)
    matched = reg.match_by_model("gpt-4o-mini")
    assert matched and matched[0].id == "openai"
    matched2 = reg.match_by_model("deepseek-chat")
    assert matched2 and matched2[0].id == "deepseek"
    matched3 = reg.match_by_model("claude-3-haiku")
    assert matched3 and matched3[0].id == "anthropic"
    assert len(reg.enabled()) >= 3


def test_app_boots_and_exposes_chat_route(test_settings: Settings) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers.registry import ProviderRegistry
    from tests.conftest import FakeProvider

    reg = ProviderRegistry(
        [
            FakeProvider("deepseek", ["deepseek-"]),
            FakeProvider("openai", ["gpt-"]),
            FakeProvider("anthropic", ["claude-"]),
        ]
    )
    app = create_app(test_settings, registry=reg)
    with TestClient(app) as client:
        # OpenAPI / routes present
        paths = {r.path for r in app.routes}
        assert "/v1/chat/completions" in paths
        assert "/health" in paths
        h = client.get("/health")
        assert h.status_code == 200
        body = h.json()
        assert body["version"]
        assert "deepseek" in body["providers"]

        # Auth required
        bad = client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "x"}]},
        )
        assert bad.status_code == 401

        ok = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "x"}]},
        )
        assert ok.status_code == 200
        data = ok.json()
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["role"] == "assistant"
        assert ok.headers.get("X-LLM-Router-Provider") == "deepseek"

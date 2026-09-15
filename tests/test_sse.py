"""SSE streaming passthrough + pre-first-byte failover tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.failover.circuit_breaker import CircuitBreakerRegistry
from app.main import create_app
from app.models import ApiKeyRecord, ChatRequest
from app.providers.base import UpstreamTimeout
from app.providers.registry import ProviderRegistry
from app.ratelimit.token_bucket import TokenBucketLimiter
from app.routing.key_router import KeyRouter
from app.streaming.sse import iter_openai_sse, stream_chat_with_failover
from tests.conftest import FakeClock, FakeProvider


@pytest.mark.asyncio
async def test_iter_openai_sse_passthrough() -> None:
    async def src():
        yield b"data: 1\n\n"
        yield b"data: [DONE]\n\n"

    out = []
    async for c in iter_openai_sse(src()):
        out.append(c)
    assert out == [b"data: 1\n\n", b"data: [DONE]\n\n"]


@pytest.mark.asyncio
async def test_stream_failover_before_first_byte(fake_clock: FakeClock) -> None:
    primary = FakeProvider(
        "deepseek",
        ["deepseek-"],
        fail_times=1,
        fail_with=UpstreamTimeout("stream timeout"),
        clock=fake_clock,
        delay_s=0.05,
    )
    secondary = FakeProvider("openai", ["gpt-"], clock=fake_clock, delay_s=0.01)
    breakers = CircuitBreakerRegistry(clock=fake_clock)
    started = await stream_chat_with_failover(
        ChatRequest(model="deepseek-chat", messages=[{"role": "user", "content": "x"}]),
        [primary, secondary],
        breakers=breakers,
        default_timeout_ms=200,
        failover_budget_ms=500,
        clock=fake_clock,
    )
    assert started.provider_id == "openai"
    chunks = []
    async for c in started.chunks:
        chunks.append(c)
    assert any(b"[DONE]" in c for c in chunks)
    assert primary.calls == 1
    assert secondary.calls == 1


def test_http_sse_stream_true(test_settings) -> None:
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-", "deepseek"])])
    app = create_app(test_settings, registry=reg)
    app.state.rate_limiter = TokenBucketLimiter(capacity=100, refill_per_s=10)
    app.state.key_router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo-key")],
    )
    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            assert resp.headers.get("X-LLM-Router-Provider") == "deepseek"
            text = "".join(resp.iter_text())
            assert "data:" in text
            assert "[DONE]" in text

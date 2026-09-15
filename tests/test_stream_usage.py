"""SSE first-byte failover boundary + streamed usage accounting."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.failover.circuit_breaker import CircuitBreakerRegistry
from app.main import create_app
from app.models import ApiKeyRecord, ChatRequest
from app.providers.base import UpstreamTimeout
from app.providers.registry import ProviderRegistry
from app.ratelimit.token_bucket import TokenBucketLimiter
from app.routing.key_router import KeyRouter
from app.streaming.sse import (
    is_valid_sse_data_chunk,
    iter_openai_sse_with_usage,
    parse_usage_from_sse_chunk,
    sse_error_event,
    stream_chat_with_failover,
    StreamUsage,
)
from tests.conftest import FakeClock, FakeProvider


def test_is_valid_sse_data_chunk() -> None:
    assert is_valid_sse_data_chunk(b"data: {\"x\":1}\n\n")
    assert is_valid_sse_data_chunk(b": keepalive\n\ndata: [DONE]\n\n")
    assert not is_valid_sse_data_chunk(b"")
    assert not is_valid_sse_data_chunk(b": comment only\n\n")
    assert not is_valid_sse_data_chunk(b"event: ping\n\n")


def test_parse_usage_from_sse_chunk() -> None:
    chunk = (
        b'data: {"id":"c","object":"chat.completion.chunk","choices":[],'
        b'"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n'
    )
    usage = parse_usage_from_sse_chunk(chunk)
    assert usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert parse_usage_from_sse_chunk(b"data: [DONE]\n\n") is None


@pytest.mark.asyncio
async def test_iter_scrapes_usage_and_marks_actual() -> None:
    usage = StreamUsage()

    async def src():
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield (
            b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1,'
            b'"total_tokens":4}}\n\n'
        )
        yield b"data: [DONE]\n\n"

    out = []
    async for c in iter_openai_sse_with_usage(src(), usage):
        out.append(c)
    assert usage.accounting_status == "actual"
    assert usage.prompt_tokens == 3
    assert usage.completion_tokens == 1
    assert any(b"[DONE]" in c for c in out)


@pytest.mark.asyncio
async def test_iter_marks_unavailable_without_usage() -> None:
    usage = StreamUsage()

    async def src():
        yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    async for _ in iter_openai_sse_with_usage(src(), usage):
        pass
    assert usage.accounting_status == "unavailable"
    assert usage.total_tokens == 0


@pytest.mark.asyncio
async def test_post_first_byte_error_emits_sse_error_no_raise() -> None:
    usage = StreamUsage()

    async def src():
        yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        raise RuntimeError("upstream cut")

    chunks = []
    async for c in iter_openai_sse_with_usage(src(), usage):
        chunks.append(c)
    assert any(b"stream_error" in c for c in chunks)
    # Should not raise to the caller
    assert chunks[-1] == sse_error_event("upstream cut", code="stream_error")


class _KeepaliveThenData(FakeProvider):
    """Emits a comment keepalive before the first data chunk."""

    async def chat_stream(
        self, req: ChatRequest, *, timeout_ms: int
    ) -> AsyncIterator[bytes]:
        self._calls += 1
        if self._calls <= self.fail_times:
            raise self.fail_with
        yield b": keepalive\n\n"
        yield b'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"ok"},"finish_reason":null}]}\n\n'
        yield b"data: [DONE]\n\n"


class _UsageStreamingProvider(FakeProvider):
    async def chat_stream(
        self, req: ChatRequest, *, timeout_ms: int
    ) -> AsyncIterator[bytes]:
        self._calls += 1
        yield b'data: {"id":"chatcmpl-u","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"hi"},"finish_reason":null}]}\n\n'
        yield (
            b'data: {"id":"chatcmpl-u","object":"chat.completion.chunk","choices":[],'
            b'"usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}\n\n'
        )
        yield b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_failover_skips_keepalive_until_valid_data(fake_clock: FakeClock) -> None:
    primary = FakeProvider(
        "deepseek",
        ["deepseek-"],
        fail_times=1,
        fail_with=UpstreamTimeout("stream timeout"),
        clock=fake_clock,
        delay_s=0.05,
    )
    secondary = _KeepaliveThenData("openai", ["gpt-"], clock=fake_clock, delay_s=0.01)
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
    assert any(b"data:" in c for c in chunks)
    assert primary.calls == 1
    assert secondary.calls == 1


@pytest.mark.asyncio
async def test_no_failover_after_first_valid_byte(fake_clock: FakeClock) -> None:
    """Once committed, mid-stream failure stays on that provider (no hop)."""

    class BoomAfterFirst(FakeProvider):
        async def chat_stream(
            self, req: ChatRequest, *, timeout_ms: int
        ) -> AsyncIterator[bytes]:
            self._calls += 1
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise UpstreamTimeout("mid-stream timeout")

    primary = BoomAfterFirst("deepseek", ["deepseek-"], clock=fake_clock)
    secondary = FakeProvider("openai", ["gpt-"], clock=fake_clock)
    breakers = CircuitBreakerRegistry(clock=fake_clock)
    started = await stream_chat_with_failover(
        ChatRequest(model="deepseek-chat", messages=[{"role": "user", "content": "x"}]),
        [primary, secondary],
        breakers=breakers,
        default_timeout_ms=200,
        failover_budget_ms=500,
        clock=fake_clock,
    )
    assert started.provider_id == "deepseek"
    # Secondary must not have been contacted after commit
    assert secondary.calls == 0
    usage = StreamUsage()
    chunks = []
    async for c in iter_openai_sse_with_usage(started.chunks, usage):
        chunks.append(c)
    assert any(b"stream_error" in c or b"partial" in c for c in chunks)
    assert secondary.calls == 0


def test_http_stream_records_actual_usage(test_settings) -> None:
    reg = ProviderRegistry([_UsageStreamingProvider("deepseek", ["deepseek-", "deepseek"])])
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
            text = "".join(resp.iter_text())
            assert "[DONE]" in text

        import sqlite3

        conn = sqlite3.connect(test_settings.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT prompt_tokens, completion_tokens, accounting_status, status "
            "FROM usage_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["accounting_status"] == "actual"
        assert row["prompt_tokens"] == 7
        assert row["completion_tokens"] == 2
        assert row["status"] == "ok"


def test_http_stream_records_unavailable_without_usage(test_settings) -> None:
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
            _ = "".join(resp.iter_text())

        import sqlite3

        conn = sqlite3.connect(test_settings.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT prompt_tokens, accounting_status FROM usage_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["accounting_status"] == "unavailable"
        assert row["prompt_tokens"] == 0

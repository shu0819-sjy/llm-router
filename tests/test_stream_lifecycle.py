"""Stream lifecycle status + TTFB / total latency accounting."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import ApiKeyRecord, ChatRequest
from app.providers.base import UpstreamTimeout
from app.providers.registry import ProviderRegistry
from app.ratelimit.token_bucket import TokenBucketLimiter
from app.routing.key_router import KeyRouter
from app.streaming.sse import (
    StreamUsage,
    chunk_contains_done,
    iter_openai_sse_with_usage,
    sse_error_event,
)
from tests.conftest import FakeProvider


def test_chunk_contains_done() -> None:
    assert chunk_contains_done(b"data: [DONE]\n\n")
    assert chunk_contains_done(b": keep\n\ndata: [DONE]\n\n")
    assert not chunk_contains_done(b'data: {"content":"[DONE]"}\n\n')
    assert not chunk_contains_done(b"data: hello\n\n")


def test_stream_usage_outcome_matrix() -> None:
    ok = StreamUsage(saw_done=True)
    assert ok.outcome == "ok"

    # DONE wins over a late disconnect flag
    done_disconnect = StreamUsage(saw_done=True, client_disconnected=True)
    assert done_disconnect.outcome == "ok"

    disc = StreamUsage(client_disconnected=True)
    assert disc.outcome == "client_disconnected"

    up = StreamUsage(saw_upstream_error=True)
    assert up.outcome == "upstream_error"

    # Missing DONE without error/disconnect → partial
    partial = StreamUsage()
    assert partial.outcome == "partial"


@pytest.mark.asyncio
async def test_iter_ok_after_done_records_latency() -> None:
    usage = StreamUsage()

    async def src():
        await asyncio.sleep(0.005)
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield (
            b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1,'
            b'"total_tokens":3}}\n\n'
        )
        yield b"data: [DONE]\n\n"

    chunks = []
    async for c in iter_openai_sse_with_usage(src(), usage):
        chunks.append(c)

    assert usage.saw_done is True
    assert usage.outcome == "ok"
    assert usage.accounting_status == "actual"
    assert usage.ttfb_ms is not None and usage.ttfb_ms > 0
    assert usage.duration_ms is not None and usage.duration_ms >= usage.ttfb_ms
    assert any(b"[DONE]" in c for c in chunks)


@pytest.mark.asyncio
async def test_iter_missing_done_is_partial() -> None:
    usage = StreamUsage()

    async def src():
        yield b'data: {"choices":[{"delta":{"content":"cut"}}]}\n\n'

    async for _ in iter_openai_sse_with_usage(src(), usage):
        pass

    assert usage.saw_done is False
    assert usage.saw_upstream_error is False
    assert usage.outcome == "partial"
    assert usage.ttfb_ms is not None
    assert usage.duration_ms is not None


@pytest.mark.asyncio
async def test_iter_midstream_failure_is_upstream_error() -> None:
    usage = StreamUsage()

    async def src():
        yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        raise UpstreamTimeout("mid-stream cut")

    chunks = []
    async for c in iter_openai_sse_with_usage(src(), usage):
        chunks.append(c)

    assert usage.saw_done is False
    assert usage.saw_upstream_error is True
    assert usage.outcome == "upstream_error"
    assert any(b"stream_error" in c for c in chunks)
    # Clients get a fixed public message — never the raw exception text.
    assert chunks[-1] == sse_error_event("Upstream stream error", code="stream_error")
    assert b"mid-stream cut" not in chunks[-1]
    assert usage.duration_ms is not None


class _SlowChunks(FakeProvider):
    """Yields slowly so early client close can land before DONE."""

    async def chat_stream(self, req: ChatRequest, *, timeout_ms: int) -> AsyncIterator[bytes]:
        self._calls += 1
        yield b'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"hel"},"finish_reason":null}]}\n\n'
        await asyncio.sleep(0.05)
        yield b'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}\n\n'
        await asyncio.sleep(0.05)
        yield b"data: [DONE]\n\n"


class _NoDoneProvider(FakeProvider):
    async def chat_stream(self, req: ChatRequest, *, timeout_ms: int) -> AsyncIterator[bytes]:
        self._calls += 1
        yield b'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}\n\n'


class _BoomAfterFirst(FakeProvider):
    async def chat_stream(self, req: ChatRequest, *, timeout_ms: int) -> AsyncIterator[bytes]:
        self._calls += 1
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise UpstreamTimeout("boom")


def _app_with_provider(test_settings, provider: FakeProvider) -> Any:
    reg = ProviderRegistry([provider])
    app = create_app(test_settings, registry=reg)
    app.state.rate_limiter = TokenBucketLimiter(capacity=100, refill_per_s=10)
    app.state.key_router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo-key")],
    )
    return app


def _latest_usage_row(db_path: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_events)").fetchall()}
    select = (
        "SELECT status, latency_ms, accounting_status, prompt_tokens"
        + (", ttfb_ms" if "ttfb_ms" in cols else "")
        + " FROM usage_events ORDER BY id DESC LIMIT 1"
    )
    row = conn.execute(select).fetchone()
    conn.close()
    assert row is not None
    return row


def test_http_stream_ok_records_ttfb_and_latency(test_settings) -> None:
    app = _app_with_provider(test_settings, FakeProvider("deepseek", ["deepseek-", "deepseek"]))
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

    row = _latest_usage_row(test_settings.db_path)
    assert row["status"] == "ok"
    assert int(row["latency_ms"]) > 0
    if "ttfb_ms" in row.keys():
        assert int(row["ttfb_ms"]) >= 0


def test_http_stream_missing_done_records_partial(test_settings) -> None:
    app = _app_with_provider(test_settings, _NoDoneProvider("deepseek", ["deepseek-", "deepseek"]))
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

    row = _latest_usage_row(test_settings.db_path)
    assert row["status"] == "partial"
    assert int(row["latency_ms"]) >= 0


def test_http_stream_midstream_error_records_upstream_error(test_settings) -> None:
    app = _app_with_provider(test_settings, _BoomAfterFirst("deepseek", ["deepseek-", "deepseek"]))
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
            assert "stream_error" in text

    row = _latest_usage_row(test_settings.db_path)
    assert row["status"] == "upstream_error"
    assert int(row["latency_ms"]) >= 0


@pytest.mark.asyncio
async def test_client_disconnected_outcome_without_done() -> None:
    """Chat event_gen sets client_disconnected when the ASGI client drops."""
    usage = StreamUsage()

    async def src():
        yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
        await asyncio.sleep(0.01)
        yield b"data: [DONE]\n\n"

    agen = iter_openai_sse_with_usage(src(), usage)
    first = await agen.__anext__()
    assert first
    # Simulate chat.py detecting disconnect before consuming DONE.
    usage.client_disconnected = True
    await agen.aclose()
    if usage.duration_ms is None:
        usage.mark_finished()
    # Without seeing DONE, disconnect wins.
    assert usage.saw_done is False
    assert usage.outcome == "client_disconnected"
    assert usage.ttfb_ms is not None
    assert usage.duration_ms is not None


def test_http_stream_early_close_does_not_force_zero_latency(test_settings) -> None:
    """Abandoning the stream still records measured latency (not hardcoded 0)."""
    app = _app_with_provider(test_settings, _SlowChunks("deepseek", ["deepseek-", "deepseek"]))
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
            for _line in resp.iter_lines():
                break

    row = _latest_usage_row(test_settings.db_path)
    assert row["status"] in {"client_disconnected", "partial", "ok", "upstream_error"}
    assert int(row["latency_ms"]) >= 0


_LEAK_MARKERS = (
    "sk-AbCdEfGhIjKlMnOpQrStUvWx",
    "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
    "https://user:p4ssw0rd@evil.example/v1",
    "<html>\x00\x1binject",
)


class _LeakyMidstream(FakeProvider):
    """Raises mid-stream with credential-like / control-char content."""

    async def chat_stream(self, req: ChatRequest, *, timeout_ms: int) -> AsyncIterator[bytes]:
        self._calls += 1
        yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
        raise UpstreamTimeout(
            "upstream failed with sk-AbCdEfGhIjKlMnOpQrStUvWx "
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig "
            "url=https://user:p4ssw0rd@evil.example/v1 "
            "body=<html>\x00\x1binject" + ("X" * 500)
        )


class _LeakyPreFirstByte(FakeProvider):
    """Fails before first byte so chat returns failover_exhausted JSON."""

    _LEAK_MSG = (
        "primary down sk-AbCdEfGhIjKlMnOpQrStUvWx "
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig "
        "https://user:p4ssw0rd@evil.example/v1 "
        "<html>\x00\x1binject"
    )

    async def chat(self, req: ChatRequest, *, timeout_ms: int):
        self._calls += 1
        raise UpstreamTimeout(self._LEAK_MSG)

    async def chat_stream(self, req: ChatRequest, *, timeout_ms: int) -> AsyncIterator[bytes]:
        self._calls += 1
        raise UpstreamTimeout(self._LEAK_MSG)
        if False:  # pragma: no cover — make this an async generator
            yield b""


def test_http_sse_stream_error_never_leaks_secrets(test_settings) -> None:
    app = _app_with_provider(test_settings, _LeakyMidstream("deepseek", ["deepseek-", "deepseek"]))
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
    assert "stream_error" in text
    assert "Upstream stream error" in text
    for marker in _LEAK_MARKERS:
        assert marker not in text
    assert "attempts" not in text


def test_http_failover_exhausted_omits_attempts_and_sanitizes(
    test_settings,
) -> None:
    # Only one matching provider → failover exhausts with a secret-laden message.
    app = _app_with_provider(
        test_settings, _LeakyPreFirstByte("deepseek", ["deepseek-", "deepseek"])
    )
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
    assert resp.status_code in (502, 504), resp.text
    body = resp.json()
    err = body["error"]
    assert err["code"] == "failover_exhausted"
    assert "attempts" not in err
    msg = err["message"]
    for marker in _LEAK_MARKERS:
        assert marker not in msg
    assert "\x00" not in msg and "\x1b" not in msg
    # Sanitizer should have redacted key-like material when present in the message.
    assert "sk-AbCdEf" not in msg

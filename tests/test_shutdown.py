"""Graceful shutdown: in-flight request cancellation + resource cleanup.

Covers the v0.3 hardening requirement that shutdown tests exercise both
cancellation of in-flight work and release of resources (providers, DB
connection, global DB handle), including after a client cancels mid-stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db.database import get_db
from app.main import create_app
from app.providers.registry import ProviderRegistry
from tests.conftest import FakeProvider

AUTH = {"Authorization": "Bearer sk-demo-key"}
CHAT_BODY = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}


class TrackingProvider(FakeProvider):
    """FakeProvider that records aclose() and stream finalization."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.aclose_calls = 0
        self.stream_finalized = False

    async def aclose(self) -> None:
        self.aclose_calls += 1


class SlowStreamProvider(TrackingProvider):
    """Provider whose SSE stream yields slowly so it can be cancelled mid-flight."""

    async def chat_stream(self, req, *, timeout_ms: int):
        self._calls += 1
        chunk = (
            b'data: {"id":"chatcmpl-s","object":"chat.completion.chunk",'
            b'"choices":[{"index":0,"delta":{"role":"assistant","content":"x"},'
            b'"finish_reason":null}]}\n\n'
        )
        try:
            for _ in range(50):
                yield chunk
                await asyncio.sleep(0.04)
            yield b"data: [DONE]\n\n"
        finally:
            self.stream_finalized = True


def test_lifespan_shutdown_closes_providers_db_and_global_handle(
    test_settings,
) -> None:
    deepseek = TrackingProvider("deepseek", ["deepseek-"])
    openai = TrackingProvider("openai", ["gpt-"])
    reg = ProviderRegistry([deepseek, openai])
    app = create_app(test_settings, registry=reg)

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert app.state.db.connected is True

    assert deepseek.aclose_calls == 1
    assert openai.aclose_calls == 1
    assert app.state.db.connected is False
    assert get_db() is None


async def test_client_disconnect_mid_stream_finalizes_and_accounts(
    test_settings,
) -> None:
    """A client disconnecting mid-stream must finalize the upstream generator,
    record usage, and leave the app serviceable (graceful cancellation path)."""
    provider = SlowStreamProvider("deepseek", ["deepseek-"])
    reg = ProviderRegistry([provider])
    app = create_app(test_settings, registry=reg)

    async with app.router.lifespan_context(app):
        payload = json.dumps({**CHAT_BODY, "stream": True}).encode()
        scope = {
            "type": "http",
            "asgi": {"version": "2.3", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"authorization", AUTH["Authorization"].encode()),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                (b"accept", b"text/event-stream"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        state = {"sent_body": False, "disconnected": False, "chunks": 0}

        async def receive():
            if not state["sent_body"]:
                state["sent_body"] = True
                return {"type": "http.request", "body": payload, "more_body": False}
            state["disconnected"] = True
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                state["chunks"] += 1

        # The app must complete on its own once it observes the disconnect.
        await asyncio.wait_for(app(scope, receive, send), timeout=10)

        assert state["disconnected"] is True
        assert state["chunks"] >= 1
        # Upstream generator finalized — no leaked async generator or task.
        assert provider.stream_finalized is True
        # Background usage tasks outlive the response cancel scope.
        pending = set(getattr(app.state, "_bg_usage_tasks", set()))
        if pending:
            await asyncio.wait(pending, timeout=5)
        recent = await app.state.ledger.recent(limit=5)
        assert recent, "expected a usage row after mid-stream disconnect"
        assert recent[0]["status"] in {
            "client_disconnected",
            "partial",
            "ok",
            "upstream_error",
        }
        assert int(recent[0]["latency_ms"]) >= 0

    # Shutdown after the cancelled stream still releases every resource.
    assert provider.aclose_calls == 1
    assert app.state.db.connected is False


async def test_task_cancelled_request_keeps_app_serviceable(test_settings) -> None:
    """Cancelling the request task outright must not wedge the app or shutdown."""
    provider = SlowStreamProvider("deepseek", ["deepseek-"])
    reg = ProviderRegistry([provider])
    app = create_app(test_settings, registry=reg)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:

            async def consume():
                async with ac.stream(
                    "POST",
                    "/v1/chat/completions",
                    headers=AUTH,
                    json={**CHAT_BODY, "stream": True},
                ) as resp:
                    async for _ in resp.aiter_bytes():
                        pass

            task = asyncio.create_task(consume())
            await asyncio.sleep(0.15)  # let a few chunks flow
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

            # App remains serviceable after the hard cancellation.
            resp = await ac.get("/health/live")
            assert resp.status_code == 200

    # Shutdown completes cleanly with every resource released.
    assert provider.aclose_calls == 1
    assert app.state.db.connected is False
    assert get_db() is None


async def test_shutdown_after_completed_request_releases_resources(
    test_settings,
) -> None:
    provider = TrackingProvider("deepseek", ["deepseek-", "*"], delay_s=0.05)
    reg = ProviderRegistry([provider])
    app = create_app(test_settings, registry=reg)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post("/v1/chat/completions", headers=AUTH, json=CHAT_BODY)
            assert r.status_code == 200
            rows = await app.state.ledger.recent(5)
            assert rows and rows[0]["status"] == "ok"

    assert provider.aclose_calls == 1
    assert app.state.db.connected is False
    assert get_db() is None


async def test_shutdown_with_inflight_request_completes_cleanly(test_settings) -> None:
    """Exiting the lifespan while a slow request is still running must not hang."""
    provider = TrackingProvider("deepseek", ["deepseek-", "*"], delay_s=1.0)
    reg = ProviderRegistry([provider])
    app = create_app(test_settings, registry=reg)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            task = asyncio.create_task(
                ac.post("/v1/chat/completions", headers=AUTH, json=CHAT_BODY)
            )
            await asyncio.sleep(0.05)  # request is now in flight
            # Exit lifespan while the request is still awaiting the provider.

    # Shutdown completed without hanging; resources released.
    assert app.state.db.connected is False
    assert provider.aclose_calls == 1
    # Clean up the orphaned in-flight request.
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
        await task


async def test_double_entry_into_lifespan_is_safe(test_settings) -> None:
    """Entering the lifespan context twice (nested) keeps a single DB/session."""
    provider = TrackingProvider("deepseek", ["deepseek-"])
    reg = ProviderRegistry([provider])
    app = create_app(test_settings, registry=reg)

    async with app.router.lifespan_context(app):
        async with app.router.lifespan_context(app):
            assert app.state.db.connected is True
    assert provider.aclose_calls >= 1
    assert app.state.db.connected is False

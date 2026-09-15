"""Request boundary limits: body size, chat-schema bounds, and concurrency.

All rejections must be bounded OpenAI-shaped envelopes (413/400/429) produced
by app.api.errors.RequestBoundaryMiddleware, wired via app.add_middleware.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.errors import RequestBoundaryMiddleware
from app.config import Settings
from app.main import create_app
from app.providers.registry import ProviderRegistry
from tests.conftest import FakeProvider

AUTH = {"Authorization": "Bearer sk-demo-key"}


def _app(test_settings: Settings, **attr_overrides) -> object:
    for key, value in attr_overrides.items():
        setattr(test_settings, key, value)
    # Keep the token-bucket limiter out of the way; limits under test are the
    # boundary middleware's, not the per-key rate limiter's.
    test_settings.rate_capacity = 1000
    test_settings.rate_refill_per_s = 1000
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-", "*"])])
    app = create_app(test_settings, registry=reg)
    # Wired explicitly here; production create_app should add the same middleware
    # (coordinate via main.py owner — auto-install from exception handlers
    # regresses stream disconnect accounting under anyio cancel scopes).
    app.add_middleware(RequestBoundaryMiddleware)
    return app


def _chat_body(messages: int = 1, tools: int = 0) -> dict:
    body: dict = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "user", "content": f"msg {i}"} for i in range(messages)
        ],
    }
    if tools:
        body["tools"] = [
            {"type": "function", "function": {"name": f"t{i}", "parameters": {}}}
            for i in range(tools)
        ]
    return body


# ---------------------------------------------------------------------------
# Body size limits → 413
# ---------------------------------------------------------------------------


def test_body_size_limit_returns_413_openai_envelope(test_settings: Settings) -> None:
    app = _app(test_settings, max_body_bytes=4096)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json=_chat_body(messages=1) | {"ignored": "x" * 20000},
        )
        assert r.status_code == 413
        err = r.json()["error"]
        assert err["code"] == "request_too_large"
        assert err["type"] == "invalid_request_error"
        assert "detail" not in r.json()
        # Correlation header present even on boundary rejections.
        assert r.headers.get("X-Request-Id")


def test_body_size_limit_enforced_without_content_length() -> None:
    """Chunked requests (no Content-Length) are capped via accumulation."""

    async def downstream(scope, receive, send):
        raise AssertionError("downstream must not be reached on overflow")

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    chunks = [
        {"type": "http.request", "body": b"a" * 600, "more_body": True},
        {"type": "http.request", "body": b"b" * 600, "more_body": True},
        {"type": "http.request", "body": b"c" * 600, "more_body": False},
    ]
    it = iter(chunks)

    async def receive():
        return next(it)

    middleware = RequestBoundaryMiddleware(downstream, max_body_bytes=1024)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [],
        "query_string": b"",
    }
    asyncio.run(middleware(scope, receive, send))

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413


# ---------------------------------------------------------------------------
# Chat schema bounds → 400
# ---------------------------------------------------------------------------


def test_too_many_messages_returns_400(test_settings: Settings) -> None:
    app = _app(test_settings, max_messages=4)
    provider = app.state.registry.get("deepseek")
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions", headers=AUTH, json=_chat_body(messages=5))
        assert r.status_code == 400
        err = r.json()["error"]
        assert err["code"] == "too_many_messages"
        assert err["param"] == "messages"
        assert "limit is 4" in err["message"]
        # Rejected before dispatch: the provider must never be called.
        assert provider.calls == 0


def test_too_many_tools_returns_400(test_settings: Settings) -> None:
    app = _app(test_settings, max_tools=2)
    provider = app.state.registry.get("deepseek")
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions", headers=AUTH, json=_chat_body(messages=1, tools=3)
        )
        assert r.status_code == 400
        err = r.json()["error"]
        assert err["code"] == "too_many_tools"
        assert err["param"] == "tools"
        assert provider.calls == 0


def test_valid_request_passes_boundary(test_settings: Settings) -> None:
    app = _app(test_settings, max_messages=4, max_tools=2, max_body_bytes=65536)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json=_chat_body(messages=2, tools=1),
        )
        assert r.status_code == 200
        assert r.json()["object"] == "chat.completion"


def test_malformed_json_still_maps_to_422(test_settings: Settings) -> None:
    app = _app(test_settings)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            headers={**AUTH, "Content-Type": "application/json"},
            content=b'{"model": "deepseek-chat", "messages": [not-json',
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_request"


def test_get_health_unaffected_by_boundary(test_settings: Settings) -> None:
    app = _app(test_settings, max_body_bytes=4096)
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200


def test_panel_post_small_body_passes_boundary(
    test_settings: Settings,
) -> None:
    test_settings.admin_token = "panel-admin-token-abc123"
    app = _app(test_settings)
    with TestClient(app) as client:
        r = client.post(
            "/panel/api/keys",
            headers={"Authorization": "Bearer panel-admin-token-abc123"},
            json={"name": "boundary-test-key"},
        )
        assert r.status_code == 200
        assert r.json()["key"].startswith("sk-")


# ---------------------------------------------------------------------------
# Concurrency limits → 429
# ---------------------------------------------------------------------------


async def test_concurrency_limit_returns_429_with_retry_after(
    test_settings: Settings,
) -> None:
    slow = FakeProvider("deepseek", ["deepseek-", "*"], delay_s=0.3)
    test_settings.rate_capacity = 1000
    test_settings.rate_refill_per_s = 1000
    test_settings.max_concurrent_chat_requests = 1
    reg = ProviderRegistry([slow])
    app = create_app(test_settings, registry=reg)
    app.add_middleware(RequestBoundaryMiddleware)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            body = _chat_body(messages=1)
            r1, r2 = await asyncio.gather(
                ac.post("/v1/chat/completions", headers=AUTH, json=body),
                ac.post("/v1/chat/completions", headers=AUTH, json=body),
            )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 429]

    limited = r2 if r2.status_code == 429 else r1
    err = limited.json()["error"]
    assert err["code"] == "concurrency_limit"
    assert err["type"] == "rate_limit_error"
    assert limited.headers.get("Retry-After") == "1"
    assert limited.headers.get("X-Request-Id")


def test_concurrency_slot_released_after_completion(test_settings: Settings) -> None:
    app = _app(test_settings, max_concurrent_chat_requests=1)
    with TestClient(app) as client:
        for _ in range(3):
            r = client.post("/v1/chat/completions", headers=AUTH, json=_chat_body())
            assert r.status_code == 200


async def test_concurrency_slot_released_on_cancelled_request(
    test_settings: Settings,
) -> None:
    """A cancelled in-flight chat request must release its concurrency slot."""
    slow = FakeProvider("deepseek", ["deepseek-", "*"], delay_s=5.0)
    test_settings.rate_capacity = 1000
    test_settings.rate_refill_per_s = 1000
    test_settings.max_concurrent_chat_requests = 1
    reg = ProviderRegistry([slow])
    app = create_app(test_settings, registry=reg)
    app.add_middleware(RequestBoundaryMiddleware)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            task = asyncio.create_task(
                ac.post("/v1/chat/completions", headers=AUTH, json=_chat_body())
            )
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, httpx.HTTPError):
                pass
            # Slot must be free again: next request is served, not 429'd.
            r = await ac.post("/v1/chat/completions", headers=AUTH, json=_chat_body())
            assert r.status_code == 200

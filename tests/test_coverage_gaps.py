"""Targeted coverage for auth, routing allowlist, DB helpers, SSE edges, providers."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import extract_bearer
from app.config import Settings
from app.db.database import Database, get_db, hash_api_key, set_db
from app.db.ledger import UsageLedger
from app.failover.circuit_breaker import CircuitBreaker, CircuitBreakerRegistry, CircuitState
from app.failover.orchestrator import FailoverExhausted, FailoverOrchestrator
from app.main import create_app
from app.models import ApiKeyRecord, ChatRequest
from app.providers.base import ProviderError, UpstreamError, UpstreamTimeout
from app.providers.claude import ClaudeProvider
from app.providers.gpt import GPTProvider
from app.providers.registry import ProviderRegistry
from app.routing.key_router import KeyRouter
from app.streaming.sse import sse_error_event, stream_chat_with_failover
from tests.conftest import FakeClock, FakeProvider


def _req(model: str = "gpt-4o-mini", **kwargs: Any) -> ChatRequest:
    messages = kwargs.pop("messages", [{"role": "user", "content": "hi"}])
    return ChatRequest(model=model, messages=messages, **kwargs)


# ----- auth -----


def test_extract_bearer_variants() -> None:
    assert extract_bearer(None) is None
    assert extract_bearer("") is None
    assert extract_bearer("Bearer sk-abc") == "sk-abc"
    assert extract_bearer("bearer sk-abc") == "sk-abc"
    assert extract_bearer("sk-raw-token") == "sk-raw-token"
    # "Bearer " splits to ["Bearer"] only (trailing ws discarded) → raw strip path
    assert extract_bearer("Bearer ") == "Bearer"
    assert extract_bearer("Bearer   sk-z") == "sk-z"


# ----- circuit breaker snapshot -----


def test_circuit_breaker_snapshot(fake_clock: FakeClock) -> None:
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=5.0, clock=fake_clock)
    snap = cb.snapshot()
    assert snap["state"] == "closed"
    cb.record_failure()
    snap2 = cb.snapshot()
    assert snap2["state"] == "open"
    assert snap2["opened_at"] is not None


# ----- key router allowlist / forced unavailable -----


def test_key_router_allowlist_and_forced_unavailable(test_settings: Settings) -> None:
    reg = ProviderRegistry(
        [
            FakeProvider("deepseek", ["deepseek-"], enabled=False),
            FakeProvider("openai", ["gpt-"]),
            FakeProvider("qwen", ["qwen"]),
        ]
    )
    router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[
            ApiKeyRecord(
                name="allow",
                key="sk-allow",
                model_allowlist=["gpt-*", "deepseek-chat"],
            ),
            ApiKeyRecord(name="forced-bad", key="sk-forced-bad", provider_id="deepseek"),
            ApiKeyRecord(name="star", key="sk-star", model_allowlist=["claude-"]),
        ],
    )
    allow = router.authenticate("Bearer sk-allow")
    assert allow is not None
    ok = router.resolve(allow, "gpt-4o-mini")
    assert ok.primary and ok.primary.id == "openai"
    denied = router.resolve(allow, "claude-3-haiku")
    assert denied.reason == "model_not_allowlisted"
    assert denied.candidates == []

    forced = router.authenticate("sk-forced-bad")
    assert forced is not None
    bad = router.resolve(forced, "anything")
    assert "unavailable" in bad.reason
    assert bad.candidates == []

    # Exact allowlist match, but the only prefix-compatible provider is disabled
    # → v0.3 model_not_supported (no unrelated-provider fallback).
    allow2 = router.authenticate("sk-allow")
    assert allow2 is not None
    exact = router.resolve(allow2, "deepseek-chat")
    assert exact.reason == "model_not_supported"
    assert exact.primary is None
    assert exact.candidates == []

    star = router.authenticate("sk-star")
    assert star is not None
    deny_star = router.resolve(star, "gpt-4o")
    assert deny_star.reason == "model_not_allowlisted"

    # inactive key
    dead = ApiKeyRecord(name="dead", key="sk-dead", is_active=False)
    router._keys_by_value["sk-dead"] = dead
    assert router.authenticate("sk-dead") is None


def test_key_router_unknown_model_not_supported(test_settings: Settings) -> None:
    """v0.3 contract: an unknown model is no longer routed via provider order."""
    reg = ProviderRegistry(
        [
            FakeProvider("deepseek", ["deepseek-"]),
            FakeProvider("openai", ["gpt-"]),
        ]
    )
    router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo-key")],
    )
    rec = router.authenticate("sk-demo-key")
    assert rec is not None
    d = router.resolve(rec, "mystery-model-xyz")
    assert d.reason == "model_not_supported"
    assert d.primary is None
    assert d.candidates == []


def test_http_model_not_allowlisted_403(test_settings: Settings) -> None:
    reg = ProviderRegistry([FakeProvider("openai", ["gpt-"])])
    app = create_app(test_settings, registry=reg)
    app.state.key_router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[
            ApiKeyRecord(
                name="strict",
                key="sk-strict",
                model_allowlist=["deepseek-*"],
            )
        ],
    )
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-strict"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "x"}]},
        )
        assert r.status_code == 403
        body = r.json()
        err = body.get("error") or (body.get("detail") or {}).get("error") or {}
        assert err.get("code") == "model_not_allowed"


def test_http_no_providers_502(test_settings: Settings) -> None:
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"], enabled=False)])
    app = create_app(test_settings, registry=reg)
    app.state.key_router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="forced", key="sk-forced", provider_id="deepseek")],
    )
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-forced"},
            json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "x"}]},
        )
        assert r.status_code == 502
        body = r.json()
        err = body.get("error") or (body.get("detail") or {}).get("error") or {}
        assert err.get("code") == "no_providers"


def test_http_model_not_supported_400(test_settings: Settings) -> None:
    """v0.3 contract: unknown model → router-level 400, not a 502 upstream error."""
    reg = ProviderRegistry([FakeProvider("openai", ["gpt-"])])
    app = create_app(test_settings, registry=reg)
    app.state.key_router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo")],
    )
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo"},
            json={"model": "mystery-model-xyz", "messages": [{"role": "user", "content": "x"}]},
        )
        assert r.status_code == 400
        body = r.json()
        err = body.get("error") or (body.get("detail") or {}).get("error") or {}
        assert err.get("code") == "model_not_supported"
        assert err.get("type") == "invalid_request_error"
        assert "no upstream request was made" in err.get("message", "")


# ----- database -----


@pytest.mark.asyncio
async def test_database_upsert_update_prefix_price_and_globals(tmp_path) -> None:
    path = str(tmp_path / "db.sqlite")
    db = Database(path)
    assert db.connected is False
    await db.connect()
    assert db.connected is True
    # idempotent reconnect
    await db.connect()
    assert await db.ping() is True

    kid = await db.upsert_api_key(raw_key="sk-one", name="one", provider_id="openai")
    kid2 = await db.upsert_api_key(
        raw_key="sk-one",
        name="one-renamed",
        provider_id="deepseek",
        model_allowlist="gpt-*",
        rate_capacity=10,
        rate_refill_per_s=1,
        is_active=True,
    )
    assert kid == kid2
    row = await db.fetchone("SELECT name, provider_id FROM api_keys WHERE id = ?", (kid,))
    assert row["name"] == "one-renamed"
    assert row["provider_id"] == "deepseek"

    assert await db.get_api_key_id_by_raw("sk-one") == kid
    assert await db.get_api_key_id_by_raw("sk-missing") is None

    exact = await db.get_model_price("deepseek-chat")
    assert exact is not None
    # prefix fallback for versioned model name
    prefixed = await db.get_model_price("deepseek-chat-0324")
    assert prefixed is not None
    unknown = await db.get_model_price("totally-unknown-model-zzz")
    assert unknown is None

    set_db(db)
    assert get_db() is db
    set_db(None)
    assert get_db() is None

    assert hash_api_key("sk-one") == hash_api_key("sk-one")
    await db.close()
    assert db.connected is False


@pytest.mark.asyncio
async def test_ledger_zero_cost_unknown_model(tmp_path) -> None:
    db = Database(str(tmp_path / "u.db"))
    await db.connect()
    ledger = UsageLedger(db)
    row = await ledger.record(
        api_key_id=None,
        provider_id="x",
        model="no-such-price-model",
        prompt_tokens=10,
        completion_tokens=5,
    )
    assert row["cost_usd"] == 0.0
    await db.close()


# ----- openai_compat gaps -----


@pytest.mark.asyncio
async def test_openai_compat_health_disabled_4xx_stream_and_aclose() -> None:
    def models_ok(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(models_ok),
        base_url="https://api.openai.com/v1",
    )
    p = GPTProvider(api_key="k", client=client, enabled=True)
    assert await p.health() is True

    disabled = GPTProvider(api_key="", client=client, enabled=False)
    assert await disabled.health() is False
    with pytest.raises(UpstreamError):
        await disabled.chat(_req(), timeout_ms=50)

    def err4(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope")

    client4 = httpx.AsyncClient(transport=httpx.MockTransport(err4))
    p4 = GPTProvider(api_key="k", client=client4)
    with pytest.raises(UpstreamError) as ei:
        await p4.chat(_req(), timeout_ms=50)
    assert ei.value.status_code == 401
    await client4.aclose()

    def conn_err(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client_c = httpx.AsyncClient(transport=httpx.MockTransport(conn_err))
    pc = GPTProvider(api_key="k", client=client_c)
    with pytest.raises(UpstreamError):
        await pc.chat(_req(), timeout_ms=50)
    await client_c.aclose()

    # streaming success
    stream_body = (
        b'data: {"id":"1","object":"chat.completion.chunk","choices":'
        b'[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def stream_handler(request: httpx.Request) -> httpx.Response:
        assert b'"stream": true' in request.content or b'"stream":true' in request.content
        return httpx.Response(200, content=stream_body)

    client_s = httpx.AsyncClient(transport=httpx.MockTransport(stream_handler))
    ps = GPTProvider(api_key="k", client=client_s)
    chunks: list[bytes] = []
    async for c in ps.chat_stream(_req(), timeout_ms=200):
        chunks.append(c)
    assert any(b"[DONE]" in c for c in chunks)
    await client_s.aclose()

    # stream 5xx
    def stream_5xx(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b'{"error":"down"}')

    client_5 = httpx.AsyncClient(transport=httpx.MockTransport(stream_5xx))
    p5 = GPTProvider(api_key="k", client=client_5)
    with pytest.raises(UpstreamError) as e5:
        agen = p5.chat_stream(_req(), timeout_ms=100)
        await agen.__anext__()
    assert e5.value.status_code == 503
    await client_5.aclose()

    # stream 4xx
    def stream_4xx(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"error":"bad"}')

    client_b = httpx.AsyncClient(transport=httpx.MockTransport(stream_4xx))
    pb = GPTProvider(api_key="k", client=client_b)
    with pytest.raises(UpstreamError) as eb:
        agen = pb.chat_stream(_req(), timeout_ms=100)
        await agen.__anext__()
    assert eb.value.status_code == 400
    await client_b.aclose()

    # stream timeout
    def stream_to(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client_t = httpx.AsyncClient(transport=httpx.MockTransport(stream_to))
    pt = GPTProvider(api_key="k", client=client_t)
    with pytest.raises(UpstreamTimeout):
        agen = pt.chat_stream(_req(), timeout_ms=50)
        await agen.__anext__()
    await client_t.aclose()

    # disabled stream
    with pytest.raises(UpstreamError):
        agen = disabled.chat_stream(_req(), timeout_ms=50)
        await agen.__anext__()

    # owned client aclose
    owned = GPTProvider(api_key="k")
    await owned.aclose()
    await client.aclose()


# ----- claude gaps -----


@pytest.mark.asyncio
async def test_claude_mapping_edges_errors_and_stream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["temperature"] == 0.2
        assert body["top_p"] == 0.9
        assert body["stop_sequences"] == ["END"]
        # list content flattened
        assert isinstance(body["messages"][0]["content"], str)
        return httpx.Response(
            200,
            json={
                "id": "msg_x",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "A"}, "B"],
                "model": "claude-3-5-sonnet-latest",
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = ClaudeProvider(api_key="ak", client=client)
    assert await p.health() is True
    disabled = ClaudeProvider(api_key="", client=client, enabled=False)
    assert await disabled.health() is False
    with pytest.raises(UpstreamError):
        await disabled.chat(_req("claude-3-haiku"), timeout_ms=50)

    req = ChatRequest(
        model="claude-3-5-sonnet-latest",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    "world",
                ],
            },
            {"role": "tool", "content": "ignored-role"},
        ],
        temperature=0.2,
        top_p=0.9,
        stop="END",
        max_tokens=64,
    )
    result = await p.chat(req, timeout_ms=200)
    assert result["choices"][0]["message"]["content"] == "AB"
    assert result["choices"][0]["finish_reason"] == "length"

    # empty messages → synthetic user
    empty_payload = p._to_anthropic(ChatRequest(model="claude-3-haiku", messages=[]))
    assert empty_payload["messages"]

    # 5xx / timeout / connect
    def e5(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="fail")

    c5 = httpx.AsyncClient(transport=httpx.MockTransport(e5))
    with pytest.raises(UpstreamError):
        await ClaudeProvider(api_key="ak", client=c5).chat(_req("claude-3-haiku"), timeout_ms=50)
    await c5.aclose()

    def e4(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad req")

    c4 = httpx.AsyncClient(transport=httpx.MockTransport(e4))
    with pytest.raises(UpstreamError):
        await ClaudeProvider(api_key="ak", client=c4).chat(_req("claude-3-haiku"), timeout_ms=50)
    await c4.aclose()

    def et(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("t", request=request)

    ct = httpx.AsyncClient(transport=httpx.MockTransport(et))
    with pytest.raises(UpstreamTimeout):
        await ClaudeProvider(api_key="ak", client=ct).chat(_req("claude-3-haiku"), timeout_ms=50)
    await ct.aclose()

    def ec(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("c", request=request)

    cc = httpx.AsyncClient(transport=httpx.MockTransport(ec))
    with pytest.raises(UpstreamError):
        await ClaudeProvider(api_key="ak", client=cc).chat(_req("claude-3-haiku"), timeout_ms=50)
    await cc.aclose()

    # streaming remap
    anthropic_sse = (
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hel"}}\n\n'
        'data: not-json\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
    )

    def stream_ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=anthropic_sse.encode())

    cs = httpx.AsyncClient(transport=httpx.MockTransport(stream_ok))
    ps = ClaudeProvider(api_key="ak", client=cs)
    out: list[bytes] = []
    async for chunk in ps.chat_stream(_req("claude-3-haiku"), timeout_ms=200):
        out.append(chunk)
    joined = b"".join(out)
    assert b"chat.completion.chunk" in joined
    assert b"[DONE]" in joined
    await cs.aclose()

    def stream_err(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"slow down")

    ce = httpx.AsyncClient(transport=httpx.MockTransport(stream_err))
    with pytest.raises(UpstreamError):
        agen = ClaudeProvider(api_key="ak", client=ce).chat_stream(
            _req("claude-3-haiku"), timeout_ms=100
        )
        await agen.__anext__()
    await ce.aclose()

    with pytest.raises(UpstreamError):
        agen = disabled.chat_stream(_req("claude-3-haiku"), timeout_ms=50)
        await agen.__anext__()

    owned = ClaudeProvider(api_key="ak")
    await owned.aclose()
    await client.aclose()


# ----- failover orchestrator edges -----


@pytest.mark.asyncio
async def test_orchestrator_empty_candidates_and_4xx_and_provider_error(
    fake_clock: FakeClock,
) -> None:
    orch = FailoverOrchestrator(
        breakers=CircuitBreakerRegistry(clock=fake_clock),
        default_timeout_ms=100,
        failover_budget_ms=500,
        clock=fake_clock,
    )
    with pytest.raises(FailoverExhausted) as e0:
        await orch.chat(_req("deepseek-chat"), [])
    assert e0.value.status_code == 502

    bad4 = FakeProvider(
        "deepseek",
        ["deepseek-"],
        fail_times=99,
        fail_with=UpstreamError("auth", status_code=401),
        clock=fake_clock,
    )
    with pytest.raises(FailoverExhausted) as e4:
        await orch.chat(_req("deepseek-chat"), [bad4])
    assert e4.value.status_code == 401
    assert e4.value.attempts and e4.value.attempts[0].get("failover") is False

    class Boom(FakeProvider):
        async def chat(self, req: ChatRequest, *, timeout_ms: int):
            self._calls += 1
            raise ProviderError("hard fail", status_code=502)

    boom = Boom("openai", ["gpt-"], clock=fake_clock)
    with pytest.raises(FailoverExhausted) as ep:
        await orch.chat(_req("gpt-4o"), [boom])
    assert ep.value.status_code == 502


# ----- SSE edges -----


@pytest.mark.asyncio
async def test_sse_skip_open_circuit_empty_and_error_event(fake_clock: FakeClock) -> None:
    with pytest.raises(FailoverExhausted):
        await stream_chat_with_failover(
            _req("deepseek-chat"),
            [],
            breakers=CircuitBreakerRegistry(clock=fake_clock),
            clock=fake_clock,
        )

    bad = FakeProvider(
        "deepseek",
        ["deepseek-"],
        fail_times=99,
        fail_with=UpstreamTimeout("t"),
        clock=fake_clock,
    )
    good = FakeProvider("openai", ["gpt-"], clock=fake_clock)
    breakers = CircuitBreakerRegistry(failure_threshold=1, clock=fake_clock)
    breakers.get("deepseek").record_failure()
    assert breakers.get("deepseek").state == CircuitState.OPEN

    started = await stream_chat_with_failover(
        _req("deepseek-chat"),
        [bad, good],
        breakers=breakers,
        default_timeout_ms=200,
        failover_budget_ms=500,
        clock=fake_clock,
    )
    assert started.provider_id == "openai"
    assert any(a.get("skipped") for a in started.attempts)

    evt = sse_error_event("boom", code="x")
    assert b"boom" in evt and b"x" in evt


@pytest.mark.asyncio
async def test_sse_4xx_does_not_failover(fake_clock: FakeClock) -> None:
    bad = FakeProvider(
        "deepseek",
        ["deepseek-"],
        fail_times=99,
        fail_with=UpstreamError("bad model", status_code=400),
        clock=fake_clock,
    )
    good = FakeProvider("openai", ["gpt-"], clock=fake_clock)
    with pytest.raises(FailoverExhausted) as ei:
        await stream_chat_with_failover(
            _req("deepseek-chat"),
            [bad, good],
            breakers=CircuitBreakerRegistry(clock=fake_clock),
            clock=fake_clock,
        )
    assert ei.value.status_code == 400
    assert good.calls == 0


@pytest.mark.asyncio
async def test_sse_empty_stream_yields_done(fake_clock: FakeClock) -> None:
    class EmptyStream(FakeProvider):
        async def chat_stream(self, req: ChatRequest, *, timeout_ms: int):
            self._calls += 1
            if False:
                yield b""
            return

    p = EmptyStream("deepseek", ["deepseek-"], clock=fake_clock)
    started = await stream_chat_with_failover(
        _req("deepseek-chat"),
        [p],
        breakers=CircuitBreakerRegistry(clock=fake_clock),
        clock=fake_clock,
    )
    chunks = [c async for c in started.chunks]
    assert chunks == [b"data: [DONE]\n\n"]

"""Failover + circuit breaker tests (budget < 500ms)."""

from __future__ import annotations

import pytest

from app.failover.circuit_breaker import CircuitBreaker, CircuitBreakerRegistry, CircuitState
from app.failover.orchestrator import FailoverExhausted, FailoverOrchestrator
from app.models import ApiKeyRecord, ChatRequest
from app.providers.base import UpstreamError, UpstreamTimeout
from app.providers.registry import ProviderRegistry
from app.routing.key_router import KeyRouter
from tests.conftest import FakeClock, FakeProvider


def _req(model: str = "deepseek-chat") -> ChatRequest:
    return ChatRequest(model=model, messages=[{"role": "user", "content": "ping"}])


def test_circuit_breaker_opens_and_half_open_recovers(fake_clock: FakeClock) -> None:
    cb = CircuitBreaker(
        failure_threshold=3,
        recovery_timeout_s=10.0,
        half_open_max=1,
        clock=fake_clock,
    )
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request() is True
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False

    fake_clock.advance(9.0)
    assert cb.state == CircuitState.OPEN
    fake_clock.advance(1.5)
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.allow_request() is True
    # only one half-open trial
    assert cb.allow_request() is False
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


def test_circuit_breaker_half_open_failure_reopens(fake_clock: FakeClock) -> None:
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=5.0, clock=fake_clock)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    fake_clock.advance(5.0)
    assert cb.allow_request() is True
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_failover_primary_timeout_to_secondary_under_500ms(
    fake_clock: FakeClock,
) -> None:
    primary = FakeProvider(
        "deepseek",
        ["deepseek-"],
        fail_times=1,
        fail_with=UpstreamTimeout("primary timeout"),
        clock=fake_clock,
        delay_s=0.08,  # 80ms simulated
    )
    secondary = FakeProvider(
        "openai",
        ["gpt-"],
        clock=fake_clock,
        delay_s=0.02,
    )
    breakers = CircuitBreakerRegistry(failure_threshold=3, clock=fake_clock)
    orch = FailoverOrchestrator(
        breakers=breakers,
        default_timeout_ms=200,
        failover_budget_ms=500,
        clock=fake_clock,
    )
    result = await orch.chat(_req(), [primary, secondary])
    assert result.provider_id == "openai"
    assert result.elapsed_ms < 500
    assert primary.calls == 1
    assert secondary.calls == 1
    assert any(a.get("failover") for a in result.attempts)


@pytest.mark.asyncio
async def test_failover_skips_open_circuit(fake_clock: FakeClock) -> None:
    bad = FakeProvider(
        "deepseek",
        ["deepseek-"],
        fail_times=99,
        fail_with=UpstreamError("boom", status_code=503),
        clock=fake_clock,
    )
    good = FakeProvider("openai", ["gpt-"], clock=fake_clock)
    breakers = CircuitBreakerRegistry(failure_threshold=1, clock=fake_clock)
    # Trip deepseek open
    b = breakers.get("deepseek")
    b.record_failure()
    assert b.state == CircuitState.OPEN

    orch = FailoverOrchestrator(
        breakers=breakers,
        default_timeout_ms=100,
        failover_budget_ms=500,
        clock=fake_clock,
    )
    result = await orch.chat(_req(), [bad, good])
    assert result.provider_id == "openai"
    assert bad.calls == 0  # skipped due to open circuit
    assert any(a.get("skipped") for a in result.attempts)


@pytest.mark.asyncio
async def test_failover_budget_exhausted(fake_clock: FakeClock) -> None:
    a = FakeProvider(
        "deepseek",
        ["deepseek-"],
        fail_times=99,
        fail_with=UpstreamTimeout("t1"),
        clock=fake_clock,
        delay_s=0.3,
    )
    b = FakeProvider(
        "openai",
        ["gpt-"],
        fail_times=99,
        fail_with=UpstreamTimeout("t2"),
        clock=fake_clock,
        delay_s=0.3,
    )
    orch = FailoverOrchestrator(
        breakers=CircuitBreakerRegistry(clock=fake_clock),
        default_timeout_ms=400,
        failover_budget_ms=500,
        clock=fake_clock,
    )
    with pytest.raises(FailoverExhausted) as ei:
        await orch.chat(_req(), [a, b])
    assert ei.value.status_code == 504
    assert ei.value.attempts
    # Wall under budget accounting via fake clock (~0.6 but break after budget)
    assert fake_clock.now - 1000.0 >= 0.3


def test_key_router_forced_provider_and_prefix(test_settings) -> None:
    reg = ProviderRegistry(
        [
            FakeProvider("deepseek", ["deepseek-"]),
            FakeProvider("openai", ["gpt-"]),
            FakeProvider("anthropic", ["claude-"]),
            FakeProvider("qwen", ["qwen"]),
        ]
    )
    router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[
            ApiKeyRecord(name="demo", key="sk-demo-key"),
            ApiKeyRecord(name="forced", key="sk-forced", provider_id="deepseek"),
        ],
    )
    demo = router.authenticate("Bearer sk-demo-key")
    assert demo is not None
    d = router.resolve(demo, "gpt-4o-mini")
    assert d.reason == "model_prefix"
    assert d.primary and d.primary.id == "openai"
    # v0.3 contract: candidates are limited to providers whose model prefixes
    # match — unrelated providers are no longer appended (no pollution).
    assert [c.id for c in d.candidates] == ["openai"]

    forced = router.authenticate("sk-forced")
    assert forced is not None
    # Forced provider does not serve this model → explicit mismatch, no candidates.
    d2 = router.resolve(forced, "gpt-4o-mini")
    assert d2.reason == "forced_provider_model_mismatch"
    assert d2.primary is None
    assert d2.candidates == []
    # Forced provider serving the model → single candidate, still forced.
    d3 = router.resolve(forced, "deepseek-chat")
    assert d3.reason == "key_forced_provider"
    assert len(d3.candidates) == 1 and d3.candidates[0].id == "deepseek"

    assert router.authenticate("sk-missing") is None


def test_http_failover_under_500ms_via_testclient(test_settings, fake_clock: FakeClock) -> None:
    """End-to-end: primary fails once → compatible secondary succeeds; elapsed < 500.

    Both candidates must share the model prefix — v0.3 no longer appends
    unrelated providers (e.g. gpt-only openai) as deepseek-chat failover.
    """
    from fastapi.testclient import TestClient

    from app.failover.circuit_breaker import CircuitBreakerRegistry
    from app.failover.orchestrator import FailoverOrchestrator
    from app.main import create_app
    from app.models import ApiKeyRecord
    from app.providers.registry import ProviderRegistry
    from app.routing.key_router import KeyRouter

    primary = FakeProvider(
        "deepseek",
        ["deepseek-"],
        fail_times=1,
        fail_with=UpstreamTimeout("primary down"),
        clock=fake_clock,
        delay_s=0.05,
    )
    # Compatible secondary: same model prefix family, different provider id.
    secondary = FakeProvider(
        "deepseek-backup",
        ["deepseek-"],
        clock=fake_clock,
        delay_s=0.01,
        response={
            "id": "chatcmpl-fallback",
            "object": "chat.completion",
            "created": 1710000000,
            "model": "deepseek-chat",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "fallback ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    registry = ProviderRegistry([primary, secondary])
    app = create_app(test_settings, registry=registry)
    breakers = CircuitBreakerRegistry(
        failure_threshold=test_settings.cb_failure_threshold,
        recovery_timeout_s=test_settings.cb_recovery_timeout_s,
        half_open_max=test_settings.cb_half_open_max,
        clock=fake_clock,
    )
    app.state.breakers = breakers
    app.state.orchestrator = FailoverOrchestrator(
        breakers=breakers,
        default_timeout_ms=test_settings.default_timeout_ms,
        failover_budget_ms=test_settings.failover_budget_ms,
        clock=fake_clock,
    )
    app.state.key_router = KeyRouter(
        registry,
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo-key")],
    )
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["choices"][0]["message"]["content"] == "fallback ok"
        assert resp.headers.get("X-LLM-Router-Provider") == "deepseek-backup"
        elapsed = float(resp.headers.get("X-LLM-Router-Elapsed-Ms", "9999"))
        assert elapsed < 500


def test_key_forced_routing_http(client_with_fakes) -> None:
    # v0.3 contract: forced key + model the forced provider does not serve is a
    # router-level 400 (no upstream request was made), not a 502 failover.
    resp = client_with_fakes.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-forced"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "x"}],
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "forced_provider_model_mismatch"
    assert "no upstream request was made" in body["error"]["message"]

    # Forced key with a supported model only talks to deepseek; the fixture's
    # deepseek fails once and there is no fallback — first request → exhausted.
    resp2 = client_with_fakes.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-forced"},
        json={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "x"}],
        },
    )
    assert resp2.status_code in (504, 502)
    body2 = resp2.json()
    assert body2["error"]["code"] == "failover_exhausted"

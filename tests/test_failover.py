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
    assert d.primary and d.primary.id == "openai"
    assert d.candidates[0].id == "openai"
    assert "deepseek" in [c.id for c in d.candidates]

    forced = router.authenticate("sk-forced")
    assert forced is not None
    d2 = router.resolve(forced, "gpt-4o-mini")
    assert d2.reason == "key_forced_provider"
    assert len(d2.candidates) == 1 and d2.candidates[0].id == "deepseek"

    assert router.authenticate("sk-missing") is None


def test_http_failover_under_500ms_via_testclient(client_with_fakes) -> None:
    """End-to-end: primary fails once → secondary succeeds; elapsed header < 500."""
    resp = client_with_fakes.post(
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
    assert resp.headers.get("X-LLM-Router-Provider") == "openai"
    elapsed = float(resp.headers.get("X-LLM-Router-Elapsed-Ms", "9999"))
    assert elapsed < 500


def test_key_forced_routing_http(client_with_fakes) -> None:
    # forced key only talks to deepseek; first call fails (FakeProvider fail_times=1),
    # second would succeed — but only one candidate so first request → 504
    resp = client_with_fakes.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-forced"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "x"}],
        },
    )
    # Primary deepseek fails once; no fallback because key-forced
    assert resp.status_code in (504, 502)
    body = resp.json()
    assert body["error"]["code"] == "failover_exhausted"

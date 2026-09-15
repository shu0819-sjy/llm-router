"""Shared pytest fixtures for llm-router."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, reset_settings_cache
from app.failover.circuit_breaker import CircuitBreakerRegistry
from app.failover.orchestrator import FailoverOrchestrator
from app.main import create_app
from app.models import ApiKeyRecord, ChatRequest
from app.providers.base import Provider, UpstreamError, UpstreamTimeout
from app.providers.claude import ClaudeProvider
from app.providers.deepseek import DeepSeekProvider
from app.providers.gpt import GPTProvider
from app.providers.qwen import QwenProvider
from app.providers.registry import ProviderRegistry
from app.routing.key_router import KeyRouter


class FakeClock:
    """Controllable monotonic clock for breaker / failover tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProvider(Provider):
    """In-memory provider for routing / failover unit tests."""

    def __init__(
        self,
        provider_id: str,
        prefixes: list[str],
        *,
        enabled: bool = True,
        delay_s: float = 0.0,
        fail_times: int = 0,
        fail_with: Exception | None = None,
        response: dict[str, Any] | None = None,
        clock: FakeClock | None = None,
    ) -> None:
        self.id = provider_id
        self.supported_prefixes = prefixes
        self.enabled = enabled
        self.delay_s = delay_s
        self.fail_times = fail_times
        self.fail_with = fail_with or UpstreamTimeout(f"{provider_id} timeout")
        self._calls = 0
        self.clock = clock
        self.response = response or {
            "id": f"chatcmpl-{provider_id}",
            "object": "chat.completion",
            "created": 1710000000,
            "model": "fake",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": f"hi from {provider_id}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    async def health(self) -> bool:
        return self.enabled

    async def chat(self, req: ChatRequest, *, timeout_ms: int) -> dict[str, Any]:
        self._calls += 1
        if self.clock and self.delay_s:
            self.clock.advance(self.delay_s)
        elif self.delay_s:
            # Real sleep only when no fake clock (keep unit tests fast via clock)
            import asyncio

            await asyncio.sleep(self.delay_s)
        if self._calls <= self.fail_times:
            raise self.fail_with
        out = dict(self.response)
        out["model"] = req.model
        return out

    async def chat_stream(
        self, req: ChatRequest, *, timeout_ms: int
    ) -> AsyncIterator[bytes]:
        self._calls += 1
        if self.clock and self.delay_s:
            self.clock.advance(self.delay_s)
        if self._calls <= self.fail_times:
            raise self.fail_with
        yield b'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"hel"},"finish_reason":null}]}\n\n'
        yield b'data: {"id":"chatcmpl-s","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}\n\n'
        yield b"data: [DONE]\n\n"

    @property
    def calls(self) -> int:
        return self._calls


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def test_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    reset_settings_cache()
    db_path = str(tmp_path / "test_llm_router.db")
    monkeypatch.setenv("LLM_ROUTER_API_KEYS", "demo:sk-demo-key,forced:sk-forced:deepseek")
    monkeypatch.setenv("LLM_ROUTER_DEFAULT_TIMEOUT_MS", "200")
    monkeypatch.setenv("LLM_ROUTER_FAILOVER_BUDGET_MS", "500")
    monkeypatch.setenv("LLM_ROUTER_CB_FAILURE_THRESHOLD", "3")
    monkeypatch.setenv("LLM_ROUTER_CB_RECOVERY_TIMEOUT_S", "30")
    monkeypatch.setenv("LLM_ROUTER_PROVIDER_ORDER", "deepseek,openai,anthropic,qwen")
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", db_path)
    monkeypatch.setenv("LLM_ROUTER_RATE_CAPACITY", "5")
    monkeypatch.setenv("LLM_ROUTER_RATE_REFILL_PER_S", "1")
    monkeypatch.setenv("LLM_ROUTER_RATE_COST_PER_REQ", "1")
    monkeypatch.setenv("LLM_ROUTER_ENABLE_PROMETHEUS", "true")
    # Tests run in development mode so empty/placeholder admin tokens do not hard-fail lifespan.
    monkeypatch.setenv("LLM_ROUTER_ENV", "development")
    monkeypatch.setenv("LLM_ROUTER_RATE_LIMIT_SECRET", "test-rate-limit-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek")
    monkeypatch.setenv("QWEN_API_KEY", "test-qwen")
    reset_settings_cache()
    return Settings()


@pytest.fixture
def openai_completion_json() -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1710000000,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }


def make_mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.fixture
def client_with_fakes(test_settings: Settings, fake_clock: FakeClock) -> TestClient:
    primary = FakeProvider(
        "deepseek",
        ["deepseek-"],
        fail_times=1,
        fail_with=UpstreamTimeout("primary down"),
        clock=fake_clock,
        delay_s=0.05,
    )
    secondary = FakeProvider(
        "openai",
        ["gpt-"],
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
    # Rebuild orchestrator with fake clock for deterministic timing
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
        keys=[
            ApiKeyRecord(name="demo", key="sk-demo-key"),
            ApiKeyRecord(name="forced", key="sk-forced", provider_id="deepseek"),
        ],
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def chat_request() -> ChatRequest:
    return ChatRequest(
        model="deepseek-chat",
        messages=[{"role": "user", "content": "hi"}],
    )

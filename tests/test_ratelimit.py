"""Token-bucket rate limit tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import ApiKeyRecord
from app.providers.registry import ProviderRegistry
from app.ratelimit.token_bucket import RateLimitExceeded, TokenBucketLimiter
from app.routing.key_router import KeyRouter
from tests.conftest import FakeClock, FakeProvider


def test_token_bucket_allows_burst_then_429(fake_clock: FakeClock) -> None:
    limiter = TokenBucketLimiter(capacity=3, refill_per_s=1.0, cost_per_req=1, clock=fake_clock)
    for _ in range(3):
        limit, remaining = limiter.allow("k1")
        assert limit == 3
        assert remaining >= 0
    with pytest.raises(RateLimitExceeded) as ei:
        limiter.allow("k1")
    assert ei.value.retry_after_s > 0
    # refill one token
    fake_clock.advance(1.0)
    limit, remaining = limiter.allow("k1")
    assert remaining >= 0


def test_per_key_isolation(fake_clock: FakeClock) -> None:
    limiter = TokenBucketLimiter(capacity=1, refill_per_s=0.1, clock=fake_clock)
    limiter.allow("a")
    with pytest.raises(RateLimitExceeded):
        limiter.allow("a")
    # different key still allowed
    limiter.allow("b")


def test_http_429_with_retry_after(test_settings) -> None:
    # Tiny bucket so second request trips 429
    from app.ratelimit.token_bucket import TokenBucketLimiter

    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(test_settings, registry=reg)
    app.state.rate_limiter = TokenBucketLimiter(
        capacity=1, refill_per_s=0.01, cost_per_req=1
    )
    app.state.key_router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo-key")],
    )
    with TestClient(app) as client:
        r1 = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "a"}]},
        )
        assert r1.status_code == 200
        assert "X-RateLimit-Limit" in r1.headers
        r2 = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "b"}]},
        )
        assert r2.status_code == 429
        assert "Retry-After" in r2.headers
        body = r2.json()
        assert body["detail"]["error"]["code"] == "rate_limit_exceeded"

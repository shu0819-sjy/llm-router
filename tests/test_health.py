"""Health + Prometheus metrics endpoint tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.metrics.prometheus import MetricsRegistry, render_prometheus
from app.providers.registry import ProviderRegistry
from tests.conftest import FakeProvider


def test_health_includes_circuits_and_db(test_settings) -> None:
    reg = ProviderRegistry(
        [
            FakeProvider("deepseek", ["deepseek-"]),
            FakeProvider("openai", ["gpt-"]),
            FakeProvider("anthropic", ["claude-"], enabled=True),
        ]
    )
    app = create_app(test_settings, registry=reg)
    # Trip one circuit open
    app.state.breakers.get("openai").record_failure()
    app.state.breakers.get("openai").record_failure()
    app.state.breakers.get("openai").record_failure()

    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["version"]
        assert body["db"] == "ok"
        assert "deepseek" in body["providers"]
        assert body["providers"]["openai"]["circuit"] == "open"
        assert body["providers"]["openai"]["healthy"] is False
        assert body["status"] in ("ok", "degraded")
        assert body["status"] == "degraded"


def test_metrics_prometheus_wired(test_settings) -> None:
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(test_settings, registry=reg)
    app.state.metrics.inc_request("deepseek", "ok")
    app.state.metrics.add_tokens(3, 5)
    app.state.metrics.inc_rate_limited()

    with TestClient(app) as client:
        r = client.get("/metrics")
        assert r.status_code == 200
        text = r.text
        assert "llm_router_requests_total" in text
        assert 'provider="deepseek"' in text
        assert "llm_router_rate_limited_total" in text
        assert "llm_router_tokens_total" in text
        assert "llm_router_circuit_state" in text


def test_render_prometheus_format() -> None:
    reg = MetricsRegistry()
    reg.inc_request("openai", "ok")
    reg.inc_failover("openai", "deepseek")
    reg.set_circuit("openai", "open")
    text = render_prometheus(reg)
    assert 'llm_router_requests_total{provider="openai",status="ok"} 1' in text
    assert "llm_router_failover_total" in text
    assert 'llm_router_circuit_state{provider="openai"} 2' in text


def test_metrics_disabled_returns_404(test_settings, monkeypatch) -> None:
    from app.config import reset_settings_cache

    monkeypatch.setenv("LLM_ROUTER_ENABLE_PROMETHEUS", "false")
    reset_settings_cache()
    from app.config import Settings

    settings = Settings()
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(settings, registry=reg)
    with TestClient(app) as client:
        r = client.get("/metrics")
        assert r.status_code == 404

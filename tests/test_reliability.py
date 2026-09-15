"""Retry classification, health semantics, request IDs, and admin audit events."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, reset_settings_cache
from app.failover.orchestrator import FailoverExhausted, FailoverOrchestrator
from app.failover.retry import RetryClass, classify_upstream_error, is_retryable
from app.main import create_app
from app.metrics.audit import AuditLog, scrub_detail
from app.metrics.request_id import normalize_request_id, resolve_request_id
from app.models import ApiKeyRecord, ChatRequest
from app.providers.base import ProviderError, UpstreamError, UpstreamTimeout
from app.providers.registry import ProviderRegistry
from app.routing.key_router import KeyRouter
from tests.conftest import FakeClock, FakeProvider


@pytest.mark.parametrize(
    "exc,retryable,reason_substr",
    [
        (UpstreamTimeout("t"), True, "timeout"),
        (TimeoutError("t"), True, "transport"),
        (ConnectionError("c"), True, "transport"),
        (UpstreamError("rate", status_code=429), True, "429"),
        (UpstreamError("timeout", status_code=408), True, "408"),
        (UpstreamError("boom", status_code=503), True, "503"),
        (UpstreamError("no status"), True, "upstream_no_status"),
        (UpstreamError("bad key", status_code=401), False, "401"),
        (UpstreamError("bad req", status_code=400), False, "400"),
        (UpstreamError("not found", status_code=404), False, "404"),
        (ProviderError("local misconfig", status_code=500), False, "provider_error"),
    ],
)
def test_retry_classification_matrix(exc, retryable: bool, reason_substr: str) -> None:
    c = classify_upstream_error(exc)
    assert c.retryable is retryable
    assert is_retryable(exc) is retryable
    assert reason_substr in c.reason
    assert c.retry_class is (RetryClass.RETRYABLE if retryable else RetryClass.NON_RETRYABLE)


@pytest.mark.asyncio
async def test_orchestrator_does_not_failover_on_non_retryable_4xx(fake_clock: FakeClock) -> None:
    primary = FakeProvider(
        "deepseek",
        ["deepseek-"],
        fail_times=1,
        fail_with=UpstreamError("invalid api key", status_code=401),
        clock=fake_clock,
    )
    secondary = FakeProvider("openai", ["gpt-"], clock=fake_clock)
    from app.failover.circuit_breaker import CircuitBreakerRegistry

    orch = FailoverOrchestrator(
        breakers=CircuitBreakerRegistry(clock=fake_clock),
        default_timeout_ms=200,
        failover_budget_ms=500,
        clock=fake_clock,
    )
    req = ChatRequest(model="deepseek-chat", messages=[{"role": "user", "content": "hi"}])
    with pytest.raises(FailoverExhausted) as ei:
        await orch.chat(req, [primary, secondary])
    assert ei.value.status_code == 401
    assert secondary.calls == 0
    assert any(a.get("failover") is False for a in ei.value.attempts)


@pytest.mark.asyncio
async def test_orchestrator_failovers_on_retryable_5xx(fake_clock: FakeClock) -> None:
    primary = FakeProvider(
        "deepseek",
        ["deepseek-"],
        fail_times=1,
        fail_with=UpstreamError("upstream down", status_code=503),
        clock=fake_clock,
        delay_s=0.01,
    )
    secondary = FakeProvider(
        "openai",
        ["gpt-"],
        clock=fake_clock,
        delay_s=0.01,
        response={
            "id": "chatcmpl-fb",
            "object": "chat.completion",
            "created": 1,
            "model": "deepseek-chat",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    from app.failover.circuit_breaker import CircuitBreakerRegistry

    orch = FailoverOrchestrator(
        breakers=CircuitBreakerRegistry(clock=fake_clock),
        default_timeout_ms=200,
        failover_budget_ms=500,
        clock=fake_clock,
    )
    req = ChatRequest(model="deepseek-chat", messages=[{"role": "user", "content": "hi"}])
    result = await orch.chat(req, [primary, secondary])
    assert result.provider_id == "openai"
    assert secondary.calls == 1


def test_health_live_ready_providers_and_legacy(test_settings: Settings) -> None:
    reg = ProviderRegistry(
        [
            FakeProvider("deepseek", ["deepseek-"]),
            FakeProvider("openai", ["gpt-"]),
        ]
    )
    app = create_app(test_settings, registry=reg)
    app.state.breakers.get("openai").record_failure()
    app.state.breakers.get("openai").record_failure()
    app.state.breakers.get("openai").record_failure()

    with TestClient(app) as client:
        legacy = client.get("/health")
        assert legacy.status_code == 200
        body = legacy.json()
        assert set(body) >= {"status", "version", "uptime_s", "providers", "db"}
        assert body["db"] == "ok"
        assert body["status"] == "degraded"

        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json()["status"] == "ok"
        assert "providers" not in live.json()

        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ok"
        assert ready.json()["db"] == "ok"

        providers = client.get("/health/providers")
        assert providers.status_code == 200
        pdata = providers.json()
        assert "providers" in pdata
        assert pdata["providers"]["openai"]["circuit"] == "open"
        assert pdata["providers"]["openai"]["healthy"] is False


def test_health_ready_fails_when_db_down(test_settings: Settings) -> None:
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(test_settings, registry=reg)

    class _BrokenDB:
        async def ping(self) -> bool:
            return False

    with TestClient(app) as client:
        app.state.db = _BrokenDB()
        r = client.get("/health/ready")
        assert r.status_code == 503
        assert r.json()["status"] == "error"
        # Legacy /health still answers 200 with degraded/error db
        legacy = client.get("/health")
        assert legacy.status_code == 200
        assert legacy.json()["db"] == "error"


def test_request_id_normalize_and_generate() -> None:
    assert normalize_request_id("abc-123_OK") == "abc-123_OK"
    assert normalize_request_id("bad id with spaces") is None
    assert normalize_request_id("") is None
    generated = resolve_request_id(None, "!!!")
    assert generated.startswith("req_")


def test_request_id_propagates_on_chat_and_ledger(test_settings: Settings) -> None:
    reg = ProviderRegistry(
        [
            FakeProvider(
                "deepseek",
                ["deepseek-", "*"],
                response={
                    "id": "chatcmpl-rid",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "deepseek-chat",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
        ]
    )
    app = create_app(test_settings, registry=reg)
    app.state.key_router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo-key")],
    )
    with TestClient(app) as client:
        inbound = "client-req-42"
        r = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer sk-demo-key",
                "X-Request-Id": inbound,
            },
            json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        assert r.headers.get("X-Request-Id") == inbound

        # Ledger row uses the same request id (sync read of the shared SQLite file)
        import sqlite3

        with sqlite3.connect(test_settings.db_path) as conn:
            row = conn.execute(
                "SELECT request_id FROM usage_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        assert row[0] == inbound

        # Generated when absent
        r2 = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r2.status_code == 200
        assert r2.headers.get("X-Request-Id", "").startswith("req_")


def test_admin_mutation_emits_structured_audit(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = "audit-admin-token-xyz"
    monkeypatch.setenv("LLM_ROUTER_ADMIN_TOKEN", admin)
    monkeypatch.setenv("LLM_ROUTER_ENV", "development")
    reset_settings_cache()
    settings = Settings()
    settings.db_path = test_settings.db_path
    settings.admin_token = admin
    settings.env = "development"
    reg = ProviderRegistry(
        [
            FakeProvider("deepseek", ["deepseek-"], enabled=True),
        ]
    )
    for p in reg.all():
        p.base_url = f"https://example.test/{p.id}"  # type: ignore[attr-defined]
    app = create_app(settings, registry=reg)
    app.state.key_router = KeyRouter(
        reg,
        settings=settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo-key")],
    )
    headers = {"Authorization": f"Bearer {admin}", "X-Request-Id": "audit-req-1"}
    with TestClient(app) as client:
        created = client.post(
            "/panel/api/keys",
            headers=headers,
            json={"name": "audited-key", "provider_id": "deepseek"},
        )
        assert created.status_code == 200
        key_id = created.json()["id"]
        raw_once = created.json()["key"]

        events = app.state.audit.recent(10)
        assert events
        create_evt = next(e for e in events if e["action"] == "api_key.create")
        assert create_evt["actor"] == "admin"
        assert create_evt["request_id"] == "audit-req-1"
        assert create_evt["resource_id"] == str(key_id)
        # Raw key must never appear in audit detail
        assert raw_once not in str(create_evt)
        assert "key" not in create_evt["detail"] or create_evt["detail"].get("key") == "[redacted]"

        deactivated = client.delete(f"/panel/api/keys/{key_id}", headers=headers)
        assert deactivated.status_code == 200
        events2 = app.state.audit.recent(10)
        assert any(e["action"] == "api_key.deactivate" for e in events2)


def test_audit_scrub_detail_redacts_secrets() -> None:
    cleaned = scrub_detail({"name": "x", "key": "sk-secret", "nested": {"token": "abc"}})
    assert cleaned["name"] == "x"
    assert cleaned["key"] == "[redacted]"
    assert cleaned["nested"]["token"] == "[redacted]"
    log = AuditLog(maxlen=3)
    log.emit("t", detail={"api_key": "nope"})
    assert log.recent(1)[0]["detail"]["api_key"] == "[redacted]"

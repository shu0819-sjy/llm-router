"""Production security boundaries:

- admin-token quality policy (documented minimum; development stays explicit)
- diagnostics/metrics auth policy and Prometheus label hygiene
- bounded, sanitized client-facing errors with request-correlated detail logs
"""

from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.errors import (
    MAX_CLIENT_ERROR_MESSAGE_CHARS,
    bounded_error_response,
    sanitize_client_error_message,
)
from app.auth import (
    DEFAULT_ADMIN_TOKEN_MIN_LENGTH,
    admin_token_quality_issues,
    assert_secure_admin_token,
)
from app.config import Settings
from app.main import create_app
from app.metrics.prometheus import (
    MAX_SERIES,
    MetricsRegistry,
    render_prometheus,
    sanitize_metric_label,
    sanitize_status_label,
)
from app.providers.registry import ProviderRegistry
from tests.conftest import FakeProvider

STRONG_ADMIN = "RouterAdmin-2024-Xk9qz-PLWmn-741"


def _prod_settings(test_settings: Settings, **overrides) -> Settings:
    return Settings(
        env="production",
        admin_token=STRONG_ADMIN,
        db_path=test_settings.db_path,
        api_keys="demo:sk-demo-key",
        rate_limit_secret="rl-secret",
        enable_prometheus=True,
        default_timeout_ms=200,
        failover_budget_ms=500,
        **overrides,
    )


# ---------------------------------------------------------------------------
# Admin-token quality policy
# ---------------------------------------------------------------------------


def test_admin_token_quality_policy_rules() -> None:
    # Missing / placeholder
    assert admin_token_quality_issues(None)
    assert admin_token_quality_issues("")
    assert admin_token_quality_issues("change-me-admin-token")

    # Too short
    issues = admin_token_quality_issues("Short1-token!")
    assert any("minimum" in i for i in issues)

    # Single character class (lowercase only, long enough)
    issues = admin_token_quality_issues("a" * 40)
    assert any("mix at least 3" in i for i in issues)
    # Digits only
    assert admin_token_quality_issues("1" * 40)

    # Whitespace
    assert any("whitespace" in i for i in admin_token_quality_issues("has space in it 123 !"))

    # Single repeated character (mixed class impossible, but explicit)
    assert any("repeated" in i for i in admin_token_quality_issues("a" * 40))

    # Strong token: no issues
    assert admin_token_quality_issues(STRONG_ADMIN) == []


def test_default_min_length_is_documented_value() -> None:
    assert DEFAULT_ADMIN_TOKEN_MIN_LENGTH == 24


def test_assert_secure_admin_token_enforces_quality_in_production(
    tmp_path,
) -> None:
    settings = Settings(
        env="production", admin_token="shorttoken123", db_path=str(tmp_path / "s.db")
    )
    with pytest.raises(RuntimeError, match="LLM_ROUTER_ADMIN_TOKEN"):
        assert_secure_admin_token(settings)


def test_assert_secure_admin_token_accepts_strong_production_token(
    tmp_path,
) -> None:
    settings = Settings(env="production", admin_token=STRONG_ADMIN, db_path=str(tmp_path / "s.db"))
    assert_secure_admin_token(settings)  # does not raise


def test_assert_secure_admin_token_development_bypass_is_explicit(
    tmp_path,
) -> None:
    # Development mode remains an explicit escape hatch for placeholders.
    settings = Settings(env="development", admin_token="", db_path=str(tmp_path / "s.db"))
    assert_secure_admin_token(settings)  # does not raise


def test_admin_token_min_length_override(tmp_path) -> None:
    settings = Settings(
        env="production",
        admin_token=STRONG_ADMIN,
        admin_token_min_length=64,
        db_path=str(tmp_path / "s.db"),
    )
    with pytest.raises(RuntimeError, match="minimum"):
        assert_secure_admin_token(settings)


def test_production_startup_fails_on_weak_admin_token(test_settings: Settings, tmp_path) -> None:
    settings = Settings(
        env="production",
        admin_token="weak-token",
        db_path=str(tmp_path / "startup.db"),
        api_keys="demo:sk-demo-key",
    )
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(settings, registry=reg)
    with pytest.raises(RuntimeError, match="LLM_ROUTER_ADMIN_TOKEN"):
        with TestClient(app):
            pass


def test_panel_rejects_weak_token_in_production_mode(
    test_settings: Settings,
) -> None:
    """require_admin refuses a weak configured token outside development (503)."""
    settings = Settings(
        env="development",
        admin_token="shorttoken123",
        db_path=test_settings.db_path,
        api_keys="demo:sk-demo-key",
    )
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(settings, registry=reg)
    with TestClient(app) as client:
        # Simulate a production deployment with a weak token configured.
        app.state.settings.env = "production"
        r = client.get("/panel/api/keys", headers={"Authorization": "Bearer shorttoken123"})
        assert r.status_code == 503
        body = r.json()
        err = body.get("error") or (body.get("detail") or {}).get("error") or {}
        assert err.get("code") == "admin_insecure_config"


# ---------------------------------------------------------------------------
# Diagnostics / metrics auth policy
# ---------------------------------------------------------------------------


def test_diagnostics_admin_gated_in_production(test_settings: Settings) -> None:
    settings = _prod_settings(test_settings)
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(settings, registry=reg)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/health/providers").status_code == 401

        headers = {"Authorization": f"Bearer {STRONG_ADMIN}"}
        ok_metrics = client.get("/metrics", headers=headers)
        assert ok_metrics.status_code == 200
        assert "llm_router_circuit_state" in ok_metrics.text

        ok_providers = client.get("/health/providers", headers=headers)
        assert ok_providers.status_code == 200
        assert "deepseek" in ok_providers.json()["providers"]

        # Wrong token
        assert (
            client.get(
                "/metrics", headers={"Authorization": "Bearer wrong-token-value"}
            ).status_code
            == 401
        )

        # Liveness/readiness stay public for load balancers and probes.
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200


def test_diagnostics_open_in_development_by_default(test_settings: Settings) -> None:
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(test_settings, registry=reg)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 200
        assert client.get("/health/providers").status_code == 200


def test_diagnostics_public_override_in_production(test_settings: Settings) -> None:
    settings = _prod_settings(test_settings, diagnostics_auth="public")
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(settings, registry=reg)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 200


def test_diagnostics_admin_override_in_development(test_settings: Settings) -> None:
    test_settings.admin_token = STRONG_ADMIN
    test_settings.diagnostics_auth = "admin"
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(test_settings, registry=reg)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        assert (
            client.get("/metrics", headers={"Authorization": f"Bearer {STRONG_ADMIN}"}).status_code
            == 200
        )


def test_diagnostics_reject_invalid_policy_mode() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(env="development", diagnostics_auth="yolo")


def test_diagnostics_locked_down_for_weak_production_token(
    test_settings: Settings,
) -> None:
    settings = Settings(
        env="development",
        admin_token="shorttoken123",
        db_path=test_settings.db_path,
        api_keys="demo:sk-demo-key",
        enable_prometheus=True,
    )
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(settings, registry=reg)
    with TestClient(app) as client:
        app.state.settings.env = "production"
        r = client.get("/metrics")
        assert r.status_code == 503
        body = r.json()
        err = body.get("error") or (body.get("detail") or {}).get("error") or {}
        assert err.get("code") == "admin_insecure_config"


# ---------------------------------------------------------------------------
# Prometheus label hygiene
# ---------------------------------------------------------------------------


def test_metric_labels_sanitized_and_escaped() -> None:
    reg = MetricsRegistry()
    reg.inc_request('p"q\ninject', "ok")
    text = render_prometheus(reg)
    # Quote escaped, newline flattened: no series injection possible.
    assert 'provider="p\\"q inject"' in text
    for line in text.splitlines():
        assert line.startswith(("llm_router_", "#"))


def test_metric_labels_redact_key_material() -> None:
    reg = MetricsRegistry()
    reg.inc_request("sk-secretkeyvalue1", "ok")
    text = render_prometheus(reg)
    assert "sk-secretkeyvalue1" not in text
    assert 'provider="[redacted]"' in text


def test_unknown_status_labels_collapse_to_other() -> None:
    reg = MetricsRegistry()
    reg.inc_request("openai", "extremely-weird-status-text")
    text = render_prometheus(reg)
    assert 'status="other"' in text
    assert "extremely-weird" not in text
    assert sanitize_status_label("ok") == "ok"
    assert sanitize_status_label("failover") == "failover"


def test_metric_label_length_capped() -> None:
    long_label = "p" * 500
    assert len(sanitize_metric_label(long_label)) <= 128  # cap + escape headroom
    reg = MetricsRegistry()
    reg.inc_request(long_label, "ok")
    text = render_prometheus(reg)
    assert "p" * 200 not in text


def test_metric_series_cardinality_capped() -> None:
    reg = MetricsRegistry()
    for i in range(MAX_SERIES + 500):
        reg.inc_request(f"provider-{i}", "ok")
    assert len(reg.requests_total) <= MAX_SERIES


def test_rendered_metrics_exclude_request_ids_and_models() -> None:
    """Labels never carry request IDs / model names / raw upstream errors."""
    reg = MetricsRegistry()
    reg.inc_request("deepseek", "ok")
    reg.observe_latency("deepseek", 12.5)
    text = render_prometheus(reg, extra_circuits={"deepseek": "closed"})
    assert "req_" not in text
    assert "model=" not in text
    assert "deepseek-chat" not in text


# ---------------------------------------------------------------------------
# Client-facing error sanitization
# ---------------------------------------------------------------------------


def test_sanitize_client_error_message_redacts_secrets() -> None:
    assert "sk-AbCdEf12345678" not in sanitize_client_error_message(
        "upstream rejected key sk-AbCdEf12345678"
    )
    assert "[redacted]" in sanitize_client_error_message("upstream rejected key sk-AbCdEf12345678")
    # Bearer tokens
    out = sanitize_client_error_message("header was Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    # Creds in URLs
    out = sanitize_client_error_message("failed calling https://user:pw@host/path")
    assert "user:pw" not in out
    # Long hex / base64 runs
    out = sanitize_client_error_message("hash 0123456789abcdef0123456789abcdef")
    assert "0123456789abcdef" not in out


def test_sanitize_client_error_message_bounds_and_flattens() -> None:
    assert sanitize_client_error_message("line1\nline2\r\ninject") == "line1 line2 inject"
    assert sanitize_client_error_message(None) == "error"
    assert sanitize_client_error_message("") == "error"
    long = "a" * 5000
    out = sanitize_client_error_message(long)
    assert len(out) <= MAX_CLIENT_ERROR_MESSAGE_CHARS


def test_bounded_error_response_sanitizes() -> None:
    resp = bounded_error_response(502, "upstream said: sk-SecretKey123456 down")
    assert resp.status_code == 502
    assert "sk-SecretKey123456" not in resp.body.decode()
    assert "[redacted]" in resp.body.decode()


def test_http_exception_detail_message_is_sanitized(
    test_settings: Settings,
) -> None:
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(test_settings, registry=reg)

    async def leaky_detail():
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "message": "upstream failed with key sk-LeakMe12345678",
                    "type": "api_error",
                }
            },
        )

    app.add_api_route("/leaky", leaky_detail, methods=["GET"])
    with TestClient(app) as client:
        r = client.get("/leaky")
        assert r.status_code == 502
        assert "sk-LeakMe12345678" not in r.text
        assert "[redacted]" in r.text
        assert "detail" not in r.json()


def test_unhandled_exception_returns_bounded_generic_error(
    test_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    app = create_app(test_settings, registry=reg)

    async def boom():
        raise RuntimeError("secret sk-SuperSecret12345 leaked in traceback")

    app.add_api_route("/boom", boom, methods=["GET"])
    with TestClient(app, raise_server_exceptions=False) as client:
        with caplog.at_level(logging.ERROR, logger="llm_router.errors"):
            r = client.get("/boom", headers={"X-Request-Id": "req-boundary-42"})

    assert r.status_code == 500
    err = r.json()["error"]
    assert err["message"] == "Internal server error"
    assert err["code"] == "internal_error"
    assert err["type"] == "api_error"
    assert "sk-SuperSecret12345" not in r.text
    # Client correlation via the request-id header.
    assert r.headers.get("X-Request-Id") == "req-boundary-42"
    # Detailed, request-correlated logging is retained server-side.
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "req-boundary-42" in joined

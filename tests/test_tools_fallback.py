"""API-level behavior for tool-capability routing and structured fallback errors.

Covers the wiring added in 0.3.2:
- requests carrying `tools` / `tool_choice` / `response_format` resolve only
  tool-capable candidates (routing-level filter, existing failover iterates the
  filtered list);
- when no tool-capable provider serves the model (auto-routed or forced key),
  the gateway returns a structured 400 that names the unsupported fields
  instead of a generic model-mismatch message.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import ApiKeyRecord
from app.providers.registry import ProviderRegistry
from app.ratelimit.token_bucket import TokenBucketLimiter
from app.routing.key_router import KeyRouter
from tests.conftest import FakeProvider

_TOOLS_BODY = {
    "tools": [
        {
            "type": "function",
            "function": {"name": "x", "parameters": {"type": "object"}},
        }
    ]
}


def _app(providers: list[FakeProvider], test_settings, *, key: str = "sk-demo-key"):
    reg = ProviderRegistry(providers)
    app = create_app(test_settings, registry=reg)
    app.state.rate_limiter = TokenBucketLimiter(capacity=100, refill_per_s=10)
    app.state.key_router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key=key)],
    )
    return app


def test_tools_request_skips_non_tool_provider_and_fails_over(test_settings) -> None:
    """Two providers share a prefix; only the tool-capable one is a candidate,
    so a tools request succeeds against it even when configured first."""
    no_tools = FakeProvider("acme-legacy", ["acme-"])
    no_tools.capabilities = frozenset()
    with_tools = FakeProvider("acme-pro", ["acme-"])
    with_tools.capabilities = frozenset({"tools", "tool_choice", "structured_output"})
    # provider_order puts acme-legacy first for prefix ties.
    app = _app([no_tools, with_tools], test_settings)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={"model": "acme-chat", "messages": [{"role": "user", "content": "hi"}], **_TOOLS_BODY},
        )
    assert resp.status_code == 200
    assert resp.headers.get("X-LLM-Router-Provider") == "acme-pro"
    # The non-tool-capable provider must never be attempted.
    assert no_tools.calls == 0
    assert with_tools.calls == 1


def test_plain_chat_unaffected_by_capability_filter(test_settings) -> None:
    """Without tool fields, both providers remain candidates (legacy first)."""
    no_tools = FakeProvider("acme-legacy", ["acme-"])
    no_tools.capabilities = frozenset()
    with_tools = FakeProvider("acme-pro", ["acme-"])
    with_tools.capabilities = frozenset({"tools", "tool_choice", "structured_output"})
    app = _app([no_tools, with_tools], test_settings)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={"model": "acme-chat", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    assert resp.headers.get("X-LLM-Router-Provider") == "acme-legacy"


def test_tools_request_without_capable_provider_names_fields(test_settings) -> None:
    """Auto-route: model only served by a non-tool provider -> structured 400."""
    no_tools = FakeProvider("anthropic", ["claude-"])
    no_tools.capabilities = frozenset()
    app = _app([no_tools], test_settings)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={
                "model": "claude-3-haiku",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": _TOOLS_BODY["tools"],
                "tool_choice": "auto",
            },
        )
    assert resp.status_code == 400
    body = resp.json()
    err = body["error"]
    assert err["code"] == "unsupported_parameter"
    assert err["type"] == "invalid_request_error"
    assert "tools" in err["message"] and "tool_choice" in err["message"]
    # No upstream request was made.
    assert no_tools.calls == 0


def test_forced_non_tool_provider_with_tools_names_fields(test_settings) -> None:
    """Forced key on a non-tool provider carrying tools -> structured 400."""
    no_tools = FakeProvider("anthropic", ["claude-"])
    no_tools.capabilities = frozenset()
    forced_key = ApiKeyRecord(name="forced-anthropic", key="sk-forced-anthropic", provider_id="anthropic")
    reg = ProviderRegistry([no_tools])
    app = _app([no_tools], test_settings)
    app.state.key_router = KeyRouter(reg, settings=test_settings, keys=[forced_key])
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-forced-anthropic"},
            json={
                "model": "claude-3-haiku",
                "messages": [{"role": "user", "content": "hi"}],
                **_TOOLS_BODY,
            },
        )
    assert resp.status_code == 400
    body = resp.json()
    err = body["error"]
    assert err["code"] == "unsupported_parameter"
    assert "tools" in err["message"]
    assert no_tools.calls == 0


def test_tool_capable_failover_between_capable_candidates(test_settings) -> None:
    """Within the tool-capable candidate set, a failing preferred provider
    fails over to the next capable provider inside the same budget."""
    failing = FakeProvider("acme-a", ["acme-"], fail_times=1)
    failing.capabilities = frozenset({"tools", "tool_choice", "structured_output"})
    healthy = FakeProvider("acme-b", ["acme-"])
    healthy.capabilities = frozenset({"tools", "tool_choice", "structured_output"})
    app = _app([failing, healthy], test_settings)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-demo-key"},
            json={"model": "acme-chat", "messages": [{"role": "user", "content": "hi"}], **_TOOLS_BODY},
        )
    assert resp.status_code == 200
    assert resp.headers.get("X-LLM-Router-Provider") == "acme-b"
    assert failing.calls == 1 and healthy.calls == 1

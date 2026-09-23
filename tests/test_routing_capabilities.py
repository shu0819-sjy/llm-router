"""Regression: candidate filtering, forced-provider mismatch, tool capabilities."""

from __future__ import annotations

from app.config import Settings
from app.models import ApiKeyRecord
from app.providers.base import Provider
from app.providers.claude import ClaudeProvider
from app.providers.deepseek import DeepSeekProvider
from app.providers.gpt import GPTProvider
from app.providers.qwen import QwenProvider
from app.providers.registry import ProviderRegistry
from app.routing.key_router import (
    KeyRouter,
    capabilities_for_tools_request,
)
from tests.conftest import FakeProvider


def _registry() -> ProviderRegistry:
    return ProviderRegistry(
        [
            FakeProvider("deepseek", ["deepseek-"]),
            FakeProvider("openai", ["gpt-", "o1-"]),
            FakeProvider("anthropic", ["claude-"]),
            FakeProvider("qwen", ["qwen"]),
        ]
    )


def test_candidates_filtered_by_model_prefix_only(test_settings: Settings) -> None:
    router = KeyRouter(
        _registry(),
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo")],
    )
    rec = router.authenticate("sk-demo")
    assert rec is not None

    d = router.resolve(rec, "gpt-4o-mini")
    assert d.reason == "model_prefix"
    assert d.primary is not None and d.primary.id == "openai"
    assert [c.id for c in d.candidates] == ["openai"]

    d2 = router.resolve(rec, "deepseek-chat")
    assert [c.id for c in d2.candidates] == ["deepseek"]

    d3 = router.resolve(rec, "claude-3-5-sonnet-latest")
    assert [c.id for c in d3.candidates] == ["anthropic"]


def test_no_match_returns_model_not_supported(test_settings: Settings) -> None:
    router = KeyRouter(
        _registry(),
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo")],
    )
    rec = router.authenticate("sk-demo")
    assert rec is not None
    d = router.resolve(rec, "mystery-model-xyz")
    assert d.reason == "model_not_supported"
    assert d.primary is None
    assert d.candidates == []


def test_provider_order_among_matching_prefixes_only(test_settings: Settings) -> None:
    """Two providers sharing a prefix stay ordered by provider_order; others excluded."""
    reg = ProviderRegistry(
        [
            FakeProvider("openai", ["gpt-"]),
            FakeProvider("openai-backup", ["gpt-"]),
            FakeProvider("deepseek", ["deepseek-"]),
        ]
    )
    # provider_order from test_settings: deepseek,openai,anthropic,qwen
    router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo")],
    )
    rec = router.authenticate("sk-demo")
    assert rec is not None
    d = router.resolve(rec, "gpt-4o")
    ids = [c.id for c in d.candidates]
    assert "deepseek" not in ids
    assert set(ids) == {"openai", "openai-backup"}
    assert ids[0] == "openai"  # appears in provider_order before the backup id
    # Deterministic / deduped
    d_again = router.resolve(rec, "gpt-4o")
    assert [c.id for c in d_again.candidates] == ids


def test_forced_provider_supported_model(test_settings: Settings) -> None:
    router = KeyRouter(
        _registry(),
        settings=test_settings,
        keys=[ApiKeyRecord(name="forced", key="sk-forced", provider_id="deepseek")],
    )
    rec = router.authenticate("sk-forced")
    assert rec is not None
    d = router.resolve(rec, "deepseek-chat")
    assert d.reason == "key_forced_provider"
    assert len(d.candidates) == 1 and d.candidates[0].id == "deepseek"


def test_forced_provider_unsupported_model_mismatch(test_settings: Settings) -> None:
    router = KeyRouter(
        _registry(),
        settings=test_settings,
        keys=[ApiKeyRecord(name="forced", key="sk-forced", provider_id="deepseek")],
    )
    rec = router.authenticate("sk-forced")
    assert rec is not None
    d = router.resolve(rec, "gpt-4o-mini")
    assert d.reason == "forced_provider_model_mismatch"
    assert d.candidates == []
    assert d.primary is None


def test_forced_provider_unavailable_keeps_reason(test_settings: Settings) -> None:
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"], enabled=False)])
    router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="forced", key="sk-forced", provider_id="deepseek")],
    )
    rec = router.authenticate("sk-forced")
    assert rec is not None
    d = router.resolve(rec, "deepseek-chat")
    assert "unavailable" in d.reason
    assert d.candidates == []


def test_forced_provider_respects_allowlist(test_settings: Settings) -> None:
    router = KeyRouter(
        _registry(),
        settings=test_settings,
        keys=[
            ApiKeyRecord(
                name="forced",
                key="sk-forced",
                provider_id="deepseek",
                model_allowlist=["deepseek-chat"],
            )
        ],
    )
    rec = router.authenticate("sk-forced")
    assert rec is not None
    denied = router.resolve(rec, "deepseek-reasoner")
    assert denied.reason == "model_not_allowlisted"
    ok = router.resolve(rec, "deepseek-chat")
    assert ok.reason == "key_forced_provider"


def test_real_providers_declare_tool_capabilities() -> None:
    gpt = GPTProvider(api_key="x", enabled=True)
    ds = DeepSeekProvider(api_key="x", enabled=True)
    qwen = QwenProvider(api_key="x", enabled=True)
    claude = ClaudeProvider(api_key="x", enabled=True)
    for p in (gpt, ds, qwen):
        assert p.supports_capabilities({"tools", "structured_output"})
    assert not claude.supports_capabilities({"tools"})


def test_tools_request_uses_capability_filtered_candidates(test_settings: Settings) -> None:
    openai = FakeProvider("openai", ["gpt-"])
    openai.capabilities = frozenset({"tools", "tool_choice", "structured_output"})
    # Hypothetical second gpt-prefix provider without tools must be excluded.
    legacy = FakeProvider("openai-legacy", ["gpt-"])
    legacy.capabilities = frozenset()
    anthropic = FakeProvider("anthropic", ["claude-"])
    anthropic.capabilities = frozenset()

    reg = ProviderRegistry([openai, legacy, anthropic])
    router = KeyRouter(
        reg,
        settings=test_settings,
        keys=[ApiKeyRecord(name="demo", key="sk-demo")],
    )
    rec = router.authenticate("sk-demo")
    assert rec is not None

    needed = capabilities_for_tools_request(has_tools=True, has_response_format=True)
    assert needed == frozenset({"tools", "structured_output"})

    d = router.resolve(rec, "gpt-4o", require_capabilities=needed)
    assert d.reason == "model_prefix"
    assert [c.id for c in d.candidates] == ["openai"]

    # Plain chat still sees both gpt-prefix providers.
    plain = router.resolve(rec, "gpt-4o")
    assert set(c.id for c in plain.candidates) == {"openai", "openai-legacy"}

    # Claude + tools → no compatible candidate.
    no_tools = router.resolve(rec, "claude-3-haiku", require_capabilities=needed)
    assert no_tools.reason == "model_not_supported"
    assert no_tools.candidates == []


def test_forced_provider_capability_mismatch(test_settings: Settings) -> None:
    anth = FakeProvider("anthropic", ["claude-"])
    anth.capabilities = frozenset()
    router = KeyRouter(
        ProviderRegistry([anth]),
        settings=test_settings,
        keys=[ApiKeyRecord(name="forced", key="sk-f", provider_id="anthropic")],
    )
    rec = router.authenticate("sk-f")
    assert rec is not None
    d = router.resolve(
        rec,
        "claude-3-haiku",
        require_capabilities=capabilities_for_tools_request(has_tools=True),
    )
    assert d.reason == "forced_provider_capability_mismatch"
    assert d.candidates == []


def test_registry_compatible_helper() -> None:
    openai = FakeProvider("openai", ["gpt-"])
    openai.capabilities = frozenset({"tools"})
    deepseek = FakeProvider("deepseek", ["deepseek-"])
    deepseek.capabilities = frozenset({"tools"})
    anth = FakeProvider("anthropic", ["claude-"])
    anth.capabilities = frozenset()
    reg = ProviderRegistry([openai, deepseek, anth])

    assert [p.id for p in reg.compatible("gpt-4o")] == ["openai"]
    assert [p.id for p in reg.compatible("gpt-4o", require_capabilities=["tools"])] == [
        "openai"
    ]
    assert reg.compatible("claude-3", require_capabilities=["tools"]) == []
    # isinstance smoke for Protocol-ish Provider
    assert all(isinstance(p, Provider) for p in reg.enabled())

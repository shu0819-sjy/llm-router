"""Opt-in live provider integration matrix.

Never selected by default: the `integration` marker is deselected via
``addopts = -m "not integration"`` **and** every case additionally requires
``LLM_ROUTER_RUN_INTEGRATION=1`` plus the specific provider credential.
Default CI keeps the flag unset (``LLM_ROUTER_RUN_INTEGRATION=0``).

Matrix: each configured provider × {sync, SSE} × {plain, tools}.

Coverage is opt-in and paid (tiny requests against live upstreams); it is a
compatibility smoke, not a production validation claim. The Anthropic adapter
does not map tools / structured output fields — that limitation is asserted
declaratively below instead of issuing a doomed live call.

Enable:

    export LLM_ROUTER_RUN_INTEGRATION=1
    export OPENAI_API_KEY=...          # and/or ANTHROPIC/DEEPSEEK/QWEN_API_KEY
    python -m pytest -m integration -q
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.models import ChatRequest
from app.providers.claude import ClaudeProvider
from app.providers.deepseek import DeepSeekProvider
from app.providers.gpt import GPTProvider
from app.providers.qwen import QwenProvider

RUN_INTEGRATION = os.getenv("LLM_ROUTER_RUN_INTEGRATION", "").strip() == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not RUN_INTEGRATION,
        reason="Set LLM_ROUTER_RUN_INTEGRATION=1 to enable live provider tests",
    ),
]

LIVE_TIMEOUT_MS = 30_000

_TOOLS_PAYLOAD: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


def _provider_factories() -> dict[str, tuple[str, str, Any]]:
    """provider_id → (credential env var, default model, factory)."""
    return {
        "openai": (
            "OPENAI_API_KEY",
            "gpt-4o-mini",
            lambda key: GPTProvider(
                api_key=key, base_url=os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
            ),
        ),
        "deepseek": (
            "DEEPSEEK_API_KEY",
            "deepseek-chat",
            lambda key: DeepSeekProvider(
                api_key=key, base_url=os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
            ),
        ),
        "qwen": (
            "QWEN_API_KEY",
            "qwen-turbo",
            lambda key: QwenProvider(
                api_key=key,
                base_url=os.getenv("QWEN_BASE_URL")
                or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
        ),
        "anthropic": (
            "ANTHROPIC_API_KEY",
            "claude-3-5-haiku-latest",
            lambda key: ClaudeProvider(
                api_key=key, base_url=os.getenv("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
            ),
        ),
    }


def _credential(provider_id: str) -> str | None:
    env_var = _provider_factories()[provider_id][0]
    value = os.getenv(env_var, "").strip()
    return value or None


def _build_provider(provider_id: str) -> Any:
    _, _, factory = _provider_factories()[provider_id]
    return factory(_credential(provider_id))


def _chat_request(provider_id: str, *, stream: bool, tools: bool) -> ChatRequest:
    model = _provider_factories()[provider_id][1]
    return ChatRequest(
        model=model,
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        max_tokens=16,
        stream=stream,
        tools=_TOOLS_PAYLOAD if tools else None,
    )


# (provider, mode, payload) — tools cases only for OpenAI-compatible adapters
# whose declared capabilities include tool forwarding.
_MATRIX_SYNC_PLAIN = ["openai", "deepseek", "qwen", "anthropic"]
_MATRIX_STREAM_PLAIN = ["openai", "deepseek", "qwen", "anthropic"]
_MATRIX_SYNC_TOOLS = ["openai", "deepseek", "qwen"]
_MATRIX_STREAM_TOOLS = ["openai", "deepseek", "qwen"]


async def _drain_stream(chunks: AsyncIterator[bytes]) -> list[bytes]:
    collected: list[bytes] = []
    async for chunk in chunks:
        if chunk:
            collected.append(chunk)
    return collected


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", _MATRIX_SYNC_PLAIN)
def test_live_sync_plain(provider_id: str) -> None:
    if _credential(provider_id) is None:
        pytest.skip(f"no credential for {provider_id}")
    result = asyncio.run(_run_sync(provider_id, tools=False))
    assert result


async def _run_sync(provider_id: str, *, tools: bool) -> bool:
    provider = _build_provider(provider_id)
    try:
        response = await provider.chat(
            _chat_request(provider_id, stream=False, tools=tools), timeout_ms=LIVE_TIMEOUT_MS
        )
        assert isinstance(response, dict)
        assert response.get("choices"), f"no choices in response: {response}"
        return True
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", _MATRIX_SYNC_TOOLS)
def test_live_sync_tools(provider_id: str) -> None:
    if _credential(provider_id) is None:
        pytest.skip(f"no credential for {provider_id}")
    result = asyncio.run(_run_sync(provider_id, tools=True))
    assert result


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", _MATRIX_STREAM_PLAIN)
def test_live_stream_plain(provider_id: str) -> None:
    if _credential(provider_id) is None:
        pytest.skip(f"no credential for {provider_id}")
    result = asyncio.run(_run_stream(provider_id, tools=False))
    assert result


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", _MATRIX_STREAM_TOOLS)
def test_live_stream_tools(provider_id: str) -> None:
    if _credential(provider_id) is None:
        pytest.skip(f"no credential for {provider_id}")
    result = asyncio.run(_run_stream(provider_id, tools=True))
    assert result


async def _run_stream(provider_id: str, *, tools: bool) -> bool:
    provider = _build_provider(provider_id)
    try:
        chunks = await provider.chat_stream(
            _chat_request(provider_id, stream=True, tools=tools), timeout_ms=LIVE_TIMEOUT_MS
        )
        collected = await _drain_stream(chunks)
        assert collected, "stream produced no chunks"
        joined = b"".join(collected)
        assert b"data:" in joined, "stream contained no SSE data events"
        assert b"[DONE]" in joined, "stream missing [DONE] terminator"
        return True
    finally:
        await provider.aclose()


def test_anthropic_tools_capability_is_declared_unsupported() -> None:
    """The Anthropic adapter must declare that it does not map tools.

    This documents, in executable form, why the tools matrix above omits
    Anthropic: such requests are rejected at the routing layer instead of
    being forwarded to an adapter that would silently drop the fields.
    """
    provider = ClaudeProvider(api_key="capability-declaration-only")
    try:
        assert "tools" not in provider.capabilities
        assert "structured_output" not in provider.capabilities
        assert not provider.supports_capabilities({"tools"})
    finally:
        pass


def test_openai_compat_tools_capability_is_declared() -> None:
    """OpenAI-compatible adapters must declare tools / structured-output support."""
    provider = GPTProvider(api_key="capability-declaration-only")
    try:
        assert provider.supports_capabilities({"tools", "tool_choice", "structured_output"})
    finally:
        pass

"""Provider Protocol / ABC for upstream LLM adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from app.models import ChatRequest


class ProviderError(Exception):
    """Base provider error."""

    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body


class UpstreamTimeout(ProviderError):
    """Upstream timed out within the attempt budget."""


class UpstreamError(ProviderError):
    """Upstream returned 5xx / connection failure (failover-eligible)."""


class Provider(ABC):
    """Abstract upstream provider."""

    id: str
    supported_prefixes: list[str]
    enabled: bool = True
    # Declared feature flags used by routing filters (e.g. tools / structured output).
    capabilities: frozenset[str] = frozenset()

    @abstractmethod
    async def health(self) -> bool:
        """Return True if the provider appears reachable."""

    @abstractmethod
    async def chat(self, req: ChatRequest, *, timeout_ms: int) -> dict[str, Any]:
        """Non-streaming chat completion → OpenAI-shaped dict."""

    @abstractmethod
    async def chat_stream(self, req: ChatRequest, *, timeout_ms: int) -> AsyncIterator[bytes]:
        """Streaming chat → raw OpenAI-shaped SSE byte chunks."""
        # pragma: no cover - abstract async generator
        if False:  # noqa: SIM510
            yield b""

    def supports_model(self, model: str) -> bool:
        m = (model or "").lower()
        for prefix in self.supported_prefixes:
            p = prefix.lower()
            if p == "*":
                return True
            if m.startswith(p):
                return True
        return False

    def supports_capabilities(self, required: frozenset[str] | set[str] | list[str]) -> bool:
        """Return True when this provider declares every required capability."""
        if not required:
            return True
        caps = self.capabilities or frozenset()
        return set(required).issubset(caps)

    async def aclose(self) -> None:
        """Release HTTP resources if any."""
        return None

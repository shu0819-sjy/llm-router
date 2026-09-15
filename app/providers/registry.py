"""Provider id → instance registry."""

from __future__ import annotations

from typing import Iterable

from app.config import Settings, get_settings
from app.providers.base import Provider
from app.providers.claude import ClaudeProvider
from app.providers.deepseek import DeepSeekProvider
from app.providers.gpt import GPTProvider
from app.providers.qwen import QwenProvider


class ProviderRegistry:
    def __init__(self, providers: Iterable[Provider] | None = None) -> None:
        self._providers: dict[str, Provider] = {}
        if providers:
            for p in providers:
                self.register(p)

    def register(self, provider: Provider) -> None:
        self._providers[provider.id] = provider

    def get(self, provider_id: str) -> Provider | None:
        return self._providers.get(provider_id)

    def all(self) -> list[Provider]:
        return list(self._providers.values())

    def enabled(self) -> list[Provider]:
        return [p for p in self._providers.values() if p.enabled]

    def match_by_model(self, model: str) -> list[Provider]:
        """Providers whose prefixes match model, longest-prefix first."""
        scored: list[tuple[int, Provider]] = []
        for p in self.enabled():
            best = 0
            m = (model or "").lower()
            for prefix in p.supported_prefixes:
                pl = prefix.lower()
                if pl != "*" and m.startswith(pl):
                    best = max(best, len(pl))
            if best:
                scored.append((best, p))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in scored]

    async def aclose(self) -> None:
        for p in self._providers.values():
            await p.aclose()


def build_default_registry(settings: Settings | None = None) -> ProviderRegistry:
    s = settings or get_settings()
    registry = ProviderRegistry()
    registry.register(
        DeepSeekProvider(api_key=s.deepseek_api_key, base_url=s.deepseek_base_url)
    )
    registry.register(
        GPTProvider(api_key=s.openai_api_key, base_url=s.openai_base_url)
    )
    registry.register(
        ClaudeProvider(api_key=s.anthropic_api_key, base_url=s.anthropic_base_url)
    )
    registry.register(
        QwenProvider(api_key=s.qwen_api_key, base_url=s.qwen_base_url)
    )
    return registry

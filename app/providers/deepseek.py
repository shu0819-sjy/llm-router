"""DeepSeek provider (OpenAI-compatible)."""

from __future__ import annotations

import httpx

from app.providers.openai_compat import OpenAICompatProvider


class DeepSeekProvider(OpenAICompatProvider):
    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.deepseek.com",
        client: httpx.AsyncClient | None = None,
        enabled: bool | None = None,
    ) -> None:
        super().__init__(
            provider_id="deepseek",
            base_url=base_url,
            api_key=api_key,
            supported_prefixes=["deepseek-", "deepseek"],
            enabled=enabled,
            client=client,
        )

"""OpenAI GPT provider (OpenAI-compatible)."""

from __future__ import annotations

import httpx

from app.providers.openai_compat import OpenAICompatProvider


class GPTProvider(OpenAICompatProvider):
    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        client: httpx.AsyncClient | None = None,
        enabled: bool | None = None,
    ) -> None:
        super().__init__(
            provider_id="openai",
            base_url=base_url,
            api_key=api_key,
            supported_prefixes=["gpt-", "o1-", "o3-", "o4-"],
            enabled=enabled,
            client=client,
        )

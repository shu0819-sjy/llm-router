"""Qwen / DashScope provider (OpenAI-compatible)."""

from __future__ import annotations

import httpx

from app.providers.openai_compat import OpenAICompatProvider


class QwenProvider(OpenAICompatProvider):
    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        client: httpx.AsyncClient | None = None,
        enabled: bool | None = None,
    ) -> None:
        super().__init__(
            provider_id="qwen",
            base_url=base_url,
            api_key=api_key,
            supported_prefixes=["qwen-", "qwen"],
            enabled=enabled,
            client=client,
        )

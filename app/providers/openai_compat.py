"""Shared OpenAI-compatible HTTP adapter (GPT / DeepSeek / Qwen)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.models import ChatRequest
from app.providers.base import Provider, UpstreamError, UpstreamTimeout


class OpenAICompatProvider(Provider):
    """Thin httpx adapter for OpenAI Chat Completions-compatible APIs."""

    # OpenAI-compatible upstreams forward tools / tool_choice / response_format.
    capabilities: frozenset[str] = frozenset({"tools", "tool_choice", "structured_output"})

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        api_key: str,
        supported_prefixes: list[str],
        enabled: bool | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.id = provider_id
        self.supported_prefixes = list(supported_prefixes)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.enabled = bool(api_key) if enabled is None else enabled
        self.capabilities = type(self).capabilities
        self._owns_client = client is None
        # trust_env=False: ignore broken HTTP(S)_PROXY from the host shell
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            trust_env=False,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, req: ChatRequest, *, stream: bool) -> dict[str, Any]:
        data = req.model_dump(exclude_none=True)
        data["stream"] = stream
        return data

    def _url(self, path: str) -> str:
        base = self.base_url
        if base.endswith("/v1"):
            return f"{base}{path}"
        return f"{base}/v1{path}"

    async def health(self) -> bool:
        if not self.enabled:
            return False
        try:
            # Lightweight probe — many providers expose /models
            resp = await self._client.get(
                self._url("/models"),
                headers=self._headers(),
                timeout=2.0,
            )
            return resp.status_code < 500
        except Exception:
            return False

    async def chat(self, req: ChatRequest, *, timeout_ms: int) -> dict[str, Any]:
        if not self.enabled:
            raise UpstreamError(f"provider {self.id} disabled", status_code=503)
        timeout_s = max(timeout_ms, 1) / 1000.0
        try:
            resp = await self._client.post(
                self._url("/chat/completions"),
                headers=self._headers(),
                json=self._payload(req, stream=False),
                timeout=timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout(f"{self.id} timed out after {timeout_ms}ms") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"{self.id} connection error: {exc}") from exc

        if resp.status_code >= 500:
            raise UpstreamError(
                f"{self.id} upstream {resp.status_code}",
                status_code=resp.status_code,
                body=_safe_json(resp),
            )
        if resp.status_code >= 400:
            # 4xx generally not failover-eligible (auth/model errors)
            raise UpstreamError(
                f"{self.id} client error {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
                body=_safe_json(resp),
            )
        return resp.json()

    async def chat_stream(
        self, req: ChatRequest, *, timeout_ms: int
    ) -> AsyncIterator[bytes]:
        if not self.enabled:
            raise UpstreamError(f"provider {self.id} disabled", status_code=503)
        timeout_s = max(timeout_ms, 1) / 1000.0
        try:
            async with self._client.stream(
                "POST",
                self._url("/chat/completions"),
                headers=self._headers(),
                json=self._payload(req, stream=True),
                timeout=timeout_s,
            ) as resp:
                if resp.status_code >= 500:
                    body = await resp.aread()
                    raise UpstreamError(
                        f"{self.id} upstream {resp.status_code}",
                        status_code=resp.status_code,
                        body=body.decode("utf-8", errors="replace"),
                    )
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise UpstreamError(
                        f"{self.id} client error {resp.status_code}",
                        status_code=resp.status_code,
                        body=body.decode("utf-8", errors="replace"),
                    )
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout(f"{self.id} stream timed out after {timeout_ms}ms") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"{self.id} stream connection error: {exc}") from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError):
        return resp.text

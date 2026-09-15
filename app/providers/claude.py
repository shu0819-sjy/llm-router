"""Anthropic Claude Messages API → OpenAI chat shape adapter."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.models import ChatRequest
from app.providers.base import Provider, UpstreamError, UpstreamTimeout


class ClaudeProvider(Provider):
    """Maps OpenAI chat requests to Anthropic Messages and back."""

    id = "anthropic"
    supported_prefixes = ["claude-"]
    # Anthropic adapter does not map tools / response_format in this release.
    capabilities: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.anthropic.com",
        client: httpx.AsyncClient | None = None,
        enabled: bool | None = None,
        anthropic_version: str = "2023-06-01",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.enabled = bool(api_key) if enabled is None else enabled
        self.capabilities = type(self).capabilities
        self.anthropic_version = anthropic_version
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            trust_env=False,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
        }

    def _to_anthropic(self, req: ChatRequest) -> dict[str, Any]:
        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []
        for msg in req.messages:
            role = msg.role
            content = msg.content
            if isinstance(content, list):
                # Flatten simple text blocks; pass through otherwise
                texts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(str(block.get("text", "")))
                    elif isinstance(block, str):
                        texts.append(block)
                content = "\n".join(texts) if texts else json.dumps(content)
            if role == "system":
                system_parts.append(str(content or ""))
                continue
            if role not in ("user", "assistant"):
                role = "user"
            messages.append({"role": role, "content": content or ""})
        if not messages:
            messages = [{"role": "user", "content": ""}]
        payload: dict[str, Any] = {
            "model": req.model,
            "messages": messages,
            "max_tokens": req.max_tokens or 1024,
        }
        if system_parts:
            payload["system"] = "\n".join(system_parts)
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.top_p is not None:
            payload["top_p"] = req.top_p
        if req.stop is not None:
            payload["stop_sequences"] = (
                [req.stop] if isinstance(req.stop, str) else list(req.stop)
            )
        return payload

    def _from_anthropic(self, data: dict[str, Any], *, model: str) -> dict[str, Any]:
        content_blocks = data.get("content") or []
        texts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                texts.append(block)
        text = "".join(texts)
        stop_reason = data.get("stop_reason") or "end_turn"
        finish = {
            "end_turn": "stop",
            "max_tokens": "length",
            "stop_sequence": "stop",
            "tool_use": "tool_calls",
        }.get(stop_reason, "stop")
        usage_in = data.get("usage") or {}
        prompt_tokens = int(usage_in.get("input_tokens") or 0)
        completion_tokens = int(usage_in.get("output_tokens") or 0)
        return {
            "id": f"chatcmpl-{data.get('id', uuid.uuid4().hex[:24])}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model or data.get("model") or "claude",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish,
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    async def health(self) -> bool:
        if not self.enabled:
            return False
        # Anthropic has no cheap public ping; treat configured key as healthy for v0.1
        return True

    async def chat(self, req: ChatRequest, *, timeout_ms: int) -> dict[str, Any]:
        if not self.enabled:
            raise UpstreamError(f"provider {self.id} disabled", status_code=503)
        timeout_s = max(timeout_ms, 1) / 1000.0
        payload = self._to_anthropic(req)
        try:
            resp = await self._client.post(
                f"{self.base_url}/v1/messages",
                headers=self._headers(),
                json=payload,
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
            raise UpstreamError(
                f"{self.id} client error {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
                body=_safe_json(resp),
            )
        return self._from_anthropic(resp.json(), model=req.model)

    async def chat_stream(
        self, req: ChatRequest, *, timeout_ms: int
    ) -> AsyncIterator[bytes]:
        """Stream Anthropic SSE and remap to OpenAI chat.completion.chunk SSE."""
        if not self.enabled:
            raise UpstreamError(f"provider {self.id} disabled", status_code=503)
        timeout_s = max(timeout_ms, 1) / 1000.0
        payload = self._to_anthropic(req)
        payload["stream"] = True
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        model = req.model

        try:
            async with self._client.stream(
                "POST",
                f"{self.base_url}/v1/messages",
                headers=self._headers(),
                json=payload,
                timeout=timeout_s,
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise UpstreamError(
                        f"{self.id} stream error {resp.status_code}",
                        status_code=resp.status_code,
                        body=body.decode("utf-8", errors="replace"),
                    )
                # Emit role chunk first (OpenAI convention)
                first = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                    ],
                }
                yield f"data: {json.dumps(first)}\n\n".encode()

                buffer = ""
                async for raw in resp.aiter_text():
                    buffer += raw
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        etype = event.get("type")
                        if etype == "content_block_delta":
                            delta = event.get("delta") or {}
                            if delta.get("type") == "text_delta":
                                text = delta.get("text") or ""
                                chunk = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": text},
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                yield f"data: {json.dumps(chunk)}\n\n".encode()
                        elif etype == "message_delta":
                            stop = (event.get("delta") or {}).get("stop_reason")
                            finish = {
                                "end_turn": "stop",
                                "max_tokens": "length",
                                "stop_sequence": "stop",
                            }.get(stop or "", "stop")
                            chunk = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [
                                    {"index": 0, "delta": {}, "finish_reason": finish}
                                ],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n".encode()
                yield b"data: [DONE]\n\n"
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

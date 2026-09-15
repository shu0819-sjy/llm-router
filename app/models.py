"""Pydantic request/response models (OpenAI-compatible chat shapes)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    role: str
    content: str | list[Any] | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    model_config = ConfigDict(extra="allow")


class ResponseFormat(BaseModel):
    type: Literal["text", "json_object", "json_schema"] = "text"
    json_schema: dict[str, Any] | None = None

    model_config = ConfigDict(extra="allow")


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    n: int | None = None
    stop: str | list[str] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    user: str | None = None
    # OpenAI tool / structured-output fields (forwarded to OpenAI-compatible upstreams)
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: ResponseFormat | dict[str, Any] | None = None
    stream_options: dict[str, Any] | None = None

    model_config = ConfigDict(extra="allow")

    def has_tools_or_format(self) -> bool:
        return bool(self.tools or self.tool_choice or self.response_format)


class ChatChoiceMessage(BaseModel):
    role: str
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None

    model_config = ConfigDict(extra="allow")


class ChatChoice(BaseModel):
    index: int = 0
    message: ChatChoiceMessage
    finish_reason: str | None = "stop"

    model_config = ConfigDict(extra="allow")


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    model_config = ConfigDict(extra="allow")


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatChoice]
    usage: UsageInfo = Field(default_factory=UsageInfo)

    model_config = ConfigDict(extra="allow")


class ErrorDetail(BaseModel):
    message: str
    type: str = "invalid_request_error"
    param: str | None = None
    code: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "llm-router"

    model_config = ConfigDict(extra="allow")


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelCard] = Field(default_factory=list)


class ApiKeyRecord(BaseModel):
    """API key record (env-backed and/or SQLite)."""

    name: str
    key: str
    id: int | None = None
    provider_id: str | None = None
    model_allowlist: list[str] | None = None
    rate_capacity: float | None = None
    rate_refill_per_s: float | None = None
    is_active: bool = True

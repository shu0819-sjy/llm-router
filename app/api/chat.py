"""POST /v1/chat/completions — OpenAI-compatible sync + SSE stream."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.errors import ensure_openai_handlers, openai_error_response
from app.api.models import list_models
from app.auth import require_api_key
from app.failover.orchestrator import FailoverExhausted
from app.metrics.request_id import REQUEST_ID_HEADER, request_id_from_request
from app.models import ApiKeyRecord, ChatRequest, ModelListResponse
from app.ratelimit.token_bucket import RateLimitExceeded
from app.routing.key_digest import rate_limit_key_id
from app.streaming.sse import (
    SSE_CONTENT_TYPE,
    iter_openai_sse_with_usage,
    stream_chat_with_failover,
)

router = APIRouter(
    prefix="/v1",
    tags=["chat"],
    dependencies=[Depends(ensure_openai_handlers)],
)

# OpenAI-compatible model list lives on the same /v1 router (no main.py wiring).
router.add_api_route(
    "/models",
    list_models,
    methods=["GET"],
    response_model=ModelListResponse,
    tags=["models"],
)

# Providers that forward OpenAI tool / response_format fields via model_dump.
_OPENAI_COMPAT_TOOL_PROVIDERS = frozenset({"openai", "deepseek", "qwen", "gpt"})


def _rate_limit_secret(request: Request) -> str:
    settings = request.app.state.settings
    return (
        getattr(settings, "rate_limit_secret", "")
        or getattr(settings, "admin_token", "")
        or ""
    )


def _rate_limit_or_raise(request: Request, api_key: ApiKeyRecord) -> dict[str, str]:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return {}
    # Never use the raw bearer key as the bucket identity.
    key_id = rate_limit_key_id(api_key, secret=_rate_limit_secret(request))
    try:
        limit, remaining = limiter.allow(
            key_id,
            capacity=api_key.rate_capacity,
            refill_per_s=api_key.rate_refill_per_s,
        )
        return {
            "X-RateLimit-Limit": str(int(limit)),
            "X-RateLimit-Remaining": str(int(remaining)),
        }
    except RateLimitExceeded as exc:
        metrics = getattr(request.app.state, "metrics", None)
        if metrics is not None:
            metrics.inc_rate_limited()
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "message": exc.message,
                    "type": "rate_limit_error",
                    "code": "rate_limit_exceeded",
                }
            },
            headers={
                "Retry-After": str(max(1, int(exc.retry_after_s + 0.999))),
                "X-RateLimit-Limit": str(int(exc.limit)),
                "X-RateLimit-Remaining": str(int(exc.remaining)),
            },
        ) from exc


def _reject_unsupported_tools(body: ChatRequest, provider_ids: list[str]) -> JSONResponse | None:
    """
    tools / tool_choice / response_format round-trip on OpenAI-compatible upstreams.
    Anthropic adapter does not yet map these — reject explicitly with 400.
    """
    if not body.has_tools_or_format():
        return None
    if any(
        pid in _OPENAI_COMPAT_TOOL_PROVIDERS or pid.startswith("openai")
        for pid in provider_ids
    ):
        return None
    if all(pid == "anthropic" for pid in provider_ids):
        unsupported = []
        if body.tools:
            unsupported.append("tools")
        if body.tool_choice is not None:
            unsupported.append("tool_choice")
        if body.response_format is not None:
            unsupported.append("response_format")
        return openai_error_response(
            400,
            (
                "Fields "
                + ", ".join(unsupported)
                + " are not supported for Anthropic-routed requests in this release; "
                "use an OpenAI-compatible model/provider."
            ),
            type="invalid_request_error",
            code="unsupported_parameter",
            param=unsupported[0] if unsupported else None,
        )
    return None


async def _record_usage(
    request: Request,
    *,
    api_key: ApiKeyRecord,
    provider_id: str,
    model: str,
    response: dict[str, Any] | None,
    latency_ms: float,
    status: str,
    accounting_status: str = "actual",
    request_id: str | None = None,
) -> None:
    ledger = getattr(request.app.state, "ledger", None)
    db = getattr(request.app.state, "db", None)
    metrics = getattr(request.app.state, "metrics", None)
    if ledger is None:
        return
    usage = (response or {}).get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    if accounting_status == "unavailable":
        prompt = 0
        completion = 0
    api_key_id = api_key.id
    if api_key_id is None and db is not None:
        api_key_id = await db.get_api_key_id_by_raw(api_key.key)
    rid = request_id or request_id_from_request(request)
    await ledger.record(
        api_key_id=api_key_id,
        provider_id=provider_id,
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        latency_ms=int(latency_ms),
        status=status,
        request_id=rid,
        accounting_status=accounting_status,
    )
    if metrics is not None:
        metrics.add_tokens(prompt, completion)
        metrics.inc_request(provider_id, status)
        metrics.observe_latency(provider_id, latency_ms)


@router.post("/chat/completions")
async def chat_completions(
    body: ChatRequest,
    request: Request,
    api_key: ApiKeyRecord = Depends(require_api_key),
) -> Any:
    key_router = request.app.state.key_router
    orchestrator = request.app.state.orchestrator
    settings = request.app.state.settings
    request_id = request_id_from_request(request)

    rate_headers = _rate_limit_or_raise(request, api_key)
    rate_headers[REQUEST_ID_HEADER] = request_id

    decision = key_router.resolve(api_key, body.model)
    if decision.reason == "model_not_allowlisted":
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "message": f"Model '{body.model}' is not allowed for this API key",
                    "type": "invalid_request_error",
                    "code": "model_not_allowed",
                }
            },
        )
    if not decision.candidates:
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "message": decision.reason or "No upstream providers available",
                    "type": "api_error",
                    "code": "no_providers",
                }
            },
        )

    rejected = _reject_unsupported_tools(body, [p.id for p in decision.candidates])
    if rejected is not None:
        return rejected

    if body.stream:
        try:
            started = await stream_chat_with_failover(
                body,
                decision.candidates,
                breakers=request.app.state.breakers,
                default_timeout_ms=settings.default_timeout_ms,
                failover_budget_ms=settings.failover_budget_ms,
            )
        except FailoverExhausted as exc:
            await _record_usage(
                request,
                api_key=api_key,
                provider_id=decision.primary.id if decision.primary else "none",
                model=body.model,
                response=None,
                latency_ms=0,
                status="error",
                accounting_status="unavailable",
                request_id=request_id,
            )
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "message": exc.message,
                        "type": "api_error",
                        "code": "failover_exhausted",
                        "attempts": exc.attempts,
                    }
                },
                headers=rate_headers,
            )

        async def event_gen():
            try:
                async for chunk in iter_openai_sse_with_usage(started.chunks, started.usage):
                    yield chunk
                    if await request.is_disconnected():
                        break
            finally:
                # Honest accounting after stream completes (or client disconnect).
                await _record_usage(
                    request,
                    api_key=api_key,
                    provider_id=started.provider_id,
                    model=body.model,
                    response={
                        "id": request_id,
                        "usage": {
                            "prompt_tokens": started.usage.prompt_tokens,
                            "completion_tokens": started.usage.completion_tokens,
                            "total_tokens": started.usage.total_tokens,
                        },
                    },
                    latency_ms=0,
                    status="ok",
                    accounting_status=started.usage.accounting_status,
                    request_id=request_id,
                )

        headers = {
            **rate_headers,
            REQUEST_ID_HEADER: request_id,
            "X-LLM-Router-Provider": started.provider_id,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            event_gen(),
            media_type=SSE_CONTENT_TYPE,
            headers=headers,
        )

    try:
        result = await orchestrator.chat(body, decision.candidates)
    except FailoverExhausted as exc:
        await _record_usage(
            request,
            api_key=api_key,
            provider_id=decision.primary.id if decision.primary else "none",
            model=body.model,
            response=None,
            latency_ms=0,
            status="error",
            accounting_status="unavailable",
            request_id=request_id,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "message": exc.message,
                    "type": "api_error",
                    "code": "failover_exhausted",
                    "attempts": exc.attempts,
                }
            },
            headers=rate_headers,
        )

    response = dict(result.response)
    response.setdefault("id", response.get("id") or "chatcmpl-local")
    usage_obj = response.get("usage") or {}
    has_usage = bool(
        usage_obj.get("total_tokens")
        or usage_obj.get("prompt_tokens")
        or usage_obj.get("completion_tokens")
    )
    await _record_usage(
        request,
        api_key=api_key,
        provider_id=result.provider_id,
        model=body.model,
        response=response,
        latency_ms=result.elapsed_ms,
        status="ok" if len(result.attempts) <= 1 else "failover",
        accounting_status="actual" if has_usage else "unavailable",
        request_id=request_id,
    )

    metrics = getattr(request.app.state, "metrics", None)
    if metrics is not None and len(result.attempts) > 1:
        failed = [a for a in result.attempts if not a.get("ok") and not a.get("skipped")]
        if failed:
            metrics.inc_failover(str(failed[0].get("provider_id")), result.provider_id)

    return JSONResponse(
        status_code=200,
        content=response,
        headers={
            **rate_headers,
            REQUEST_ID_HEADER: request_id,
            "X-LLM-Router-Provider": result.provider_id,
            "X-LLM-Router-Elapsed-Ms": f"{result.elapsed_ms:.1f}",
        },
    )

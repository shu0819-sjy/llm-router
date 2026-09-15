"""POST /v1/chat/completions — OpenAI-compatible sync + SSE stream."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import require_api_key
from app.failover.orchestrator import FailoverExhausted
from app.models import ApiKeyRecord, ChatRequest
from app.ratelimit.token_bucket import RateLimitExceeded
from app.streaming.sse import SSE_CONTENT_TYPE, iter_openai_sse, stream_chat_with_failover

router = APIRouter(prefix="/v1", tags=["chat"])


def _rate_limit_or_raise(request: Request, api_key: ApiKeyRecord) -> dict[str, str]:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return {}
    key_id = api_key.key  # stable per raw key
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


async def _record_usage(
    request: Request,
    *,
    api_key: ApiKeyRecord,
    provider_id: str,
    model: str,
    response: dict[str, Any] | None,
    latency_ms: float,
    status: str,
) -> None:
    ledger = getattr(request.app.state, "ledger", None)
    db = getattr(request.app.state, "db", None)
    metrics = getattr(request.app.state, "metrics", None)
    if ledger is None:
        return
    usage = (response or {}).get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    api_key_id = api_key.id
    if api_key_id is None and db is not None:
        api_key_id = await db.get_api_key_id_by_raw(api_key.key)
    await ledger.record(
        api_key_id=api_key_id,
        provider_id=provider_id,
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        latency_ms=int(latency_ms),
        status=status,
        request_id=(response or {}).get("id"),
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

    rate_headers = _rate_limit_or_raise(request, api_key)

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

        # Record a lightweight stream-ok row (token counts unknown until client aggregates)
        await _record_usage(
            request,
            api_key=api_key,
            provider_id=started.provider_id,
            model=body.model,
            response={"id": f"chatcmpl-stream-{started.provider_id}", "usage": {}},
            latency_ms=0,
            status="ok",
        )

        async def event_gen():
            async for chunk in iter_openai_sse(started.chunks):
                # Backpressure: await each yield to the ASGI transport
                yield chunk
                if await request.is_disconnected():
                    break

        headers = {
            **rate_headers,
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
    await _record_usage(
        request,
        api_key=api_key,
        provider_id=result.provider_id,
        model=body.model,
        response=response,
        latency_ms=result.elapsed_ms,
        status="ok" if len(result.attempts) <= 1 else "failover",
    )

    metrics = getattr(request.app.state, "metrics", None)
    if metrics is not None and len(result.attempts) > 1:
        # Best-effort: first failed → final success
        failed = [a for a in result.attempts if not a.get("ok") and not a.get("skipped")]
        if failed:
            metrics.inc_failover(str(failed[0].get("provider_id")), result.provider_id)

    return JSONResponse(
        status_code=200,
        content=response,
        headers={
            **rate_headers,
            "X-LLM-Router-Provider": result.provider_id,
            "X-LLM-Router-Elapsed-Ms": f"{result.elapsed_ms:.1f}",
        },
    )

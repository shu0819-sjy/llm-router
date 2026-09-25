"""POST /v1/chat/completions — OpenAI-compatible sync + SSE stream."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.errors import (
    ensure_openai_handlers,
    openai_error_response,
    sanitize_client_error_message,
)
from app.api.models import list_models
from app.auth import require_api_key
from app.failover.orchestrator import FailoverExhausted
from app.metrics.request_id import REQUEST_ID_HEADER, request_id_from_request
from app.models import ApiKeyRecord, ChatRequest, ModelListResponse
from app.ratelimit.token_bucket import RateLimitExceeded
from app.routing.key_digest import rate_limit_key_id
from app.routing.key_router import RouteDecision, capabilities_for_tools_request
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

logger = logging.getLogger("llm_router.chat")


def _spawn_usage_record(request: Request, coro: Any) -> asyncio.Task[Any]:
    """
    Run usage accounting outside the streaming response cancel scope.

    Client disconnect / BaseHTTPMiddleware cancellation aborts awaits inside
    ``StreamingResponse`` generators; a background task kept on ``app.state``
    still persists the usage row.
    """

    async def _runner() -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001 — never fail the stream on ledger errors
            logger.exception("stream usage accounting failed")

    task = asyncio.get_running_loop().create_task(_runner())
    bucket: set[asyncio.Task[Any]] | None = getattr(request.app.state, "_bg_usage_tasks", None)
    if bucket is None:
        bucket = set()
        request.app.state._bg_usage_tasks = bucket
    bucket.add(task)
    task.add_done_callback(bucket.discard)
    return task


# OpenAI-compatible model list lives on the same /v1 router (no main.py wiring).
router.add_api_route(
    "/models",
    list_models,
    methods=["GET"],
    response_model=ModelListResponse,
    tags=["models"],
)


def _unsupported_tool_fields(body: ChatRequest) -> list[str]:
    """Tool/structured-output fields present on the request."""
    unsupported: list[str] = []
    if body.tools:
        unsupported.append("tools")
    if body.tool_choice is not None:
        unsupported.append("tool_choice")
    if body.response_format is not None:
        unsupported.append("response_format")
    return unsupported


def _reject_unsupported_tools(body: ChatRequest, decision: RouteDecision) -> JSONResponse | None:
    """
    Tool-call fallback contract:

    Requests carrying `tools` / `tool_choice` / `response_format` are routed
    only to providers that declare the matching capabilities (the capability
    filter is applied in `KeyRouter.resolve`), so failover continues between
    tool-capable candidates within the same timeout budget. When no
    tool-capable provider serves the model — auto-routed or key-forced — the
    gateway returns a structured error that names the unsupported fields
    instead of silently dropping them.
    """
    if not body.has_tools_or_format():
        return None
    if decision.candidates:
        # Every remaining candidate declares the required capabilities.
        return None
    if decision.reason in (
        "model_not_supported",
        "forced_provider_model_mismatch",
        "forced_provider_capability_mismatch",
    ):
        unsupported = _unsupported_tool_fields(body)
        if not unsupported:
            return None
        return openai_error_response(
            400,
            (
                "Fields "
                + ", ".join(unsupported)
                + " require a provider that supports tool calling; no enabled "
                "provider serves model "
                f"'{body.model}' with those capabilities."
            ),
            type="invalid_request_error",
            code="unsupported_parameter",
            param=unsupported[0],
        )
    return None


def _rate_limit_secret(request: Request) -> str:
    settings = request.app.state.settings
    return getattr(settings, "rate_limit_secret", "") or getattr(settings, "admin_token", "") or ""


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


def _usage_store(request: Request) -> Any:
    """Prefer StorageBundle.usage when injected; fall back to app.state.ledger."""
    storage = getattr(request.app.state, "storage", None)
    if storage is not None and getattr(storage, "usage", None) is not None:
        return storage.usage
    return getattr(request.app.state, "ledger", None)


def _as_latency_ms(ms: float | None, *, saw_bytes: bool = False) -> int:
    """
    Integer millisecond column helper.

    Sub-millisecond streams would otherwise truncate to 0 via int(); when the
    client observed at least one byte, floor to 1 so accounting is non-zero.
    """
    if ms is None:
        return 0
    value = int(round(float(ms)))
    if saw_bytes and value <= 0:
        return 1
    return max(0, value)


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
    ttfb_ms: float | None = None,
) -> None:
    ledger = _usage_store(request)
    db = getattr(request.app.state, "db", None)
    storage = getattr(request.app.state, "storage", None)
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
    if api_key_id is None:
        key_port = getattr(storage, "api_keys", None) if storage is not None else None
        if key_port is not None and hasattr(key_port, "get_api_key_id_by_raw"):
            api_key_id = await key_port.get_api_key_id_by_raw(api_key.key)
        elif db is not None:
            api_key_id = await db.get_api_key_id_by_raw(api_key.key)
    rid = request_id or request_id_from_request(request)
    ttfb_arg = int(ttfb_ms) if ttfb_ms is not None else None
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
        ttfb_ms=ttfb_arg,
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

    needed = capabilities_for_tools_request(
        has_tools=bool(body.tools),
        has_tool_choice=body.tool_choice is not None,
        has_response_format=body.response_format is not None,
    )
    decision = key_router.resolve(api_key, body.model, require_capabilities=needed)
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
    # Tool-call fallback: when no tool-capable provider serves the model
    # (auto-routed or key-forced), fail fast with a structured error that
    # names the unsupported fields instead of a generic mismatch message.
    tool_rejected = _reject_unsupported_tools(body, decision)
    if tool_rejected is not None:
        return tool_rejected

    if decision.reason == "forced_provider_model_mismatch":
        forced = api_key.provider_id or "unknown"
        return openai_error_response(
            400,
            (
                f"Forced provider '{forced}' does not support model '{body.model}' "
                "(prefix or capability mismatch); no upstream request was made."
            ),
            type="invalid_request_error",
            code="forced_provider_model_mismatch",
            param="model",
            headers=rate_headers,
        )
    if decision.reason == "model_not_supported":
        return openai_error_response(
            400,
            (
                f"No enabled provider supports model '{body.model}' "
                "(no matching prefix/capabilities); no upstream request was made."
            ),
            type="invalid_request_error",
            code="model_not_supported",
            param="model",
            headers=rate_headers,
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
                accounting_status="unavailable",
                request_id=request_id,
            )
            # attempts retained server-side (exc.attempts) for metrics/logs only.
            logger.info(
                "failover_exhausted stream request_id=%s attempts=%s",
                request_id,
                exc.attempts,
            )
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "message": sanitize_client_error_message(exc.message),
                        "type": "api_error",
                        "code": "failover_exhausted",
                    }
                },
                headers=rate_headers,
            )

        async def event_gen():
            # Hold an explicit reference so CancelledError (Starlette disconnect)
            # still acloses nested provider generators. Breaking only on
            # request.is_disconnected() is racy on Py3.12: the response task is
            # often cancelled at `yield` before the next disconnect poll.
            stream = iter_openai_sse_with_usage(started.chunks, started.usage)
            try:
                async for chunk in stream:
                    yield chunk
                    if await request.is_disconnected():
                        started.usage.client_disconnected = True
                        break
            except asyncio.CancelledError:
                started.usage.client_disconnected = True
                raise
            finally:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    with contextlib.suppress(Exception):
                        await aclose()
                # Honest accounting after stream completes (or client disconnect).
                # Spawn outside the response cancel scope so disconnect cannot
                # drop the usage row (shield alone is insufficient under anyio).
                if started.usage.duration_ms is None:
                    started.usage.mark_finished()
                saw_bytes = started.usage.ttfb_ms is not None
                task = _spawn_usage_record(
                    request,
                    _record_usage(
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
                        latency_ms=_as_latency_ms(started.usage.duration_ms, saw_bytes=saw_bytes),
                        status=started.usage.outcome,
                        accounting_status=started.usage.accounting_status,
                        request_id=request_id,
                        ttfb_ms=_as_latency_ms(started.usage.ttfb_ms, saw_bytes=saw_bytes),
                    ),
                )
                # Best-effort wait when not cancelled; bg task outlives cancel.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(task)

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
        # attempts retained server-side (exc.attempts) for metrics/logs only.
        logger.info(
            "failover_exhausted sync request_id=%s attempts=%s",
            request_id,
            exc.attempts,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "message": sanitize_client_error_message(exc.message),
                    "type": "api_error",
                    "code": "failover_exhausted",
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

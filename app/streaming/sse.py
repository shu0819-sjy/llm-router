"""OpenAI-compatible SSE passthrough + pre-first-byte failover."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable

from app.failover.circuit_breaker import CircuitBreakerRegistry
from app.failover.orchestrator import FailoverExhausted
from app.models import ChatRequest
from app.providers.base import Provider, ProviderError, UpstreamError, UpstreamTimeout

SSE_CONTENT_TYPE = "text/event-stream"

_FAILOVER_TYPES = (UpstreamTimeout, UpstreamError, TimeoutError, ConnectionError)


@dataclass
class StreamStart:
    provider_id: str
    chunks: AsyncIterator[bytes]
    attempts: list[dict[str, Any]] = field(default_factory=list)


async def iter_openai_sse(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """
    Passthrough upstream SSE bytes with backpressure:
    each chunk is yielded to the ASGI server before pulling the next
    (async for / StreamingResponse already awaits sends).
    """
    async for chunk in chunks:
        if chunk:
            yield chunk


async def stream_chat_with_failover(
    req: ChatRequest,
    candidates: list[Provider],
    *,
    breakers: CircuitBreakerRegistry,
    default_timeout_ms: int = 500,
    failover_budget_ms: int = 500,
    clock: Callable[[], float] | None = None,
) -> StreamStart:
    """
    Try candidates until the first SSE byte is available.
    After first byte, caller streams via iter_openai_sse (no mid-stream hop).
    """
    if not candidates:
        raise FailoverExhausted("no provider candidates", status_code=502)

    clock = clock or time.perf_counter
    start = clock()
    budget_s = max(failover_budget_ms, 1) / 1000.0
    attempts: list[dict[str, Any]] = []
    last_error: Exception | None = None
    saw_try = False

    for provider in candidates:
        remaining_s = budget_s - (clock() - start)
        if remaining_s <= 0:
            break
        breaker = breakers.get(provider.id)
        if not breaker.allow_request():
            attempts.append(
                {
                    "provider_id": provider.id,
                    "skipped": True,
                    "reason": "circuit_open",
                }
            )
            continue

        timeout_ms = min(default_timeout_ms, max(1, int(remaining_s * 1000)))
        saw_try = True
        attempt_start = clock()
        try:
            agen = provider.chat_stream(req, timeout_ms=timeout_ms)
            # Pull first chunk to validate the upstream before committing to client
            first = await agen.__anext__()
            breaker.record_success()

            async def _chain() -> AsyncIterator[bytes]:
                if first:
                    yield first
                async for c in agen:
                    yield c

            attempts.append(
                {
                    "provider_id": provider.id,
                    "ok": True,
                    "elapsed_ms": (clock() - attempt_start) * 1000,
                }
            )
            return StreamStart(
                provider_id=provider.id,
                chunks=_chain(),
                attempts=attempts,
            )
        except StopAsyncIteration:
            # Empty stream — treat as success with DONE only
            breaker.record_success()

            async def _empty() -> AsyncIterator[bytes]:
                yield b"data: [DONE]\n\n"

            return StreamStart(provider_id=provider.id, chunks=_empty(), attempts=attempts)
        except _FAILOVER_TYPES as exc:
            if isinstance(exc, UpstreamError) and exc.status_code and 400 <= exc.status_code < 500:
                if exc.status_code not in (408, 429):
                    breaker.record_failure()
                    raise FailoverExhausted(
                        str(exc),
                        last_error=exc,
                        attempts=attempts
                        + [
                            {
                                "provider_id": provider.id,
                                "ok": False,
                                "error": str(exc),
                                "status_code": exc.status_code,
                                "failover": False,
                            }
                        ],
                        status_code=exc.status_code,
                    ) from exc
            breaker.record_failure()
            last_error = exc
            attempts.append(
                {
                    "provider_id": provider.id,
                    "ok": False,
                    "error": str(exc),
                    "failover": True,
                    "elapsed_ms": (clock() - attempt_start) * 1000,
                }
            )
            continue
        except ProviderError as exc:
            breaker.record_failure()
            raise FailoverExhausted(
                str(exc),
                last_error=exc,
                attempts=attempts,
                status_code=getattr(exc, "status_code", None) or 502,
            ) from exc

    status = 504 if saw_try else 502
    msg = "failover budget exhausted" if saw_try else "no healthy providers available"
    if last_error:
        msg = f"{msg}: {last_error}"
    raise FailoverExhausted(msg, last_error=last_error, attempts=attempts, status_code=status)


def sse_error_event(message: str, *, code: str = "stream_error") -> bytes:
    payload = {"error": {"message": message, "type": "api_error", "code": code}}
    return f"data: {json.dumps(payload)}\n\n".encode()

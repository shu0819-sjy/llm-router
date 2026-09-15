"""OpenAI-compatible SSE passthrough + pre-first-byte failover + usage scrape."""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from app.failover.circuit_breaker import CircuitBreakerRegistry
from app.failover.orchestrator import FailoverExhausted
from app.failover.retry import classify_upstream_error, is_retryable
from app.models import ChatRequest
from app.providers.base import Provider, ProviderError, UpstreamError, UpstreamTimeout

SSE_CONTENT_TYPE = "text/event-stream"

_FAILOVER_TYPES = (UpstreamTimeout, UpstreamError, TimeoutError, ConnectionError)

# v0.2.0 stream accounting only observes upstream usage chunks (actual) or
# marks the row unavailable — it does not invent/estimate token counts.
AccountingStatus = Literal["actual", "unavailable"]

# A valid OpenAI-style SSE data line (not a comment / keepalive)
_DATA_LINE_RE = re.compile(rb"(?m)^data:\s*\S")


def is_valid_sse_data_chunk(chunk: bytes) -> bool:
    """True when chunk contains at least one non-empty `data:` line."""
    if not chunk:
        return False
    return _DATA_LINE_RE.search(chunk) is not None


def parse_usage_from_sse_chunk(chunk: bytes) -> dict[str, int] | None:
    """
    Extract usage from an OpenAI chat.completion.chunk SSE payload if present.
    Returns dict with prompt_tokens / completion_tokens / total_tokens or None.
    """
    if not chunk or b'"usage"' not in chunk:
        return None
    text = chunk.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            total = int(usage.get("total_tokens") or (prompt + completion))
            return {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
            }
    return None


@dataclass
class StreamUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    accounting_status: AccountingStatus = "unavailable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "accounting_status": self.accounting_status,
        }


@dataclass
class StreamStart:
    provider_id: str
    chunks: AsyncIterator[bytes]
    attempts: list[dict[str, Any]] = field(default_factory=list)
    usage: StreamUsage = field(default_factory=StreamUsage)


async def iter_openai_sse(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """
    Passthrough upstream SSE bytes with backpressure:
    each chunk is yielded to the ASGI server before pulling the next
    (async for / StreamingResponse already awaits sends).
    """
    async for chunk in chunks:
        if chunk:
            yield chunk


async def iter_openai_sse_with_usage(
    chunks: AsyncIterator[bytes],
    usage: StreamUsage,
) -> AsyncIterator[bytes]:
    """
    Passthrough SSE while scraping usage from payloads.
    Post-first-byte transport errors emit a single SSE error event and stop
    (no provider hop / no duplicate upstream output).
    """
    try:
        async for chunk in chunks:
            if not chunk:
                continue
            scraped = parse_usage_from_sse_chunk(chunk)
            if scraped is not None:
                usage.prompt_tokens = scraped["prompt_tokens"]
                usage.completion_tokens = scraped["completion_tokens"]
                usage.total_tokens = scraped["total_tokens"]
                usage.accounting_status = "actual"
            yield chunk
    except Exception as exc:  # noqa: BLE001 — terminate stream cleanly for client
        yield sse_error_event(str(exc), code="stream_error")
        return

    if usage.accounting_status != "actual":
        usage.accounting_status = "unavailable"
        usage.prompt_tokens = 0
        usage.completion_tokens = 0
        usage.total_tokens = 0


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
    Try candidates until the first *valid* SSE `data:` chunk is available.
    After that boundary, the caller streams via iter_openai_sse(_with_usage);
    mid-stream errors must not hop providers.
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
            # Pull until first valid SSE data chunk before committing to client
            preamble: list[bytes] = []
            first_valid: bytes | None = None
            while True:
                chunk = await agen.__anext__()
                if is_valid_sse_data_chunk(chunk):
                    first_valid = chunk
                    break
                if chunk:
                    preamble.append(chunk)

            breaker.record_success()
            usage = StreamUsage()

            async def _chain(
                _preamble: list[bytes] = preamble,
                _first: bytes = first_valid or b"",
                _agen: AsyncIterator[bytes] = agen,
            ) -> AsyncIterator[bytes]:
                for p in _preamble:
                    yield p
                if _first:
                    yield _first
                async for c in _agen:
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
                usage=usage,
            )
        except StopAsyncIteration:
            # Empty stream — treat as success with DONE only (committed; no failover)
            breaker.record_success()
            usage = StreamUsage(accounting_status="unavailable")

            async def _empty() -> AsyncIterator[bytes]:
                yield b"data: [DONE]\n\n"

            return StreamStart(
                provider_id=provider.id,
                chunks=_empty(),
                attempts=attempts,
                usage=usage,
            )
        except _FAILOVER_TYPES as exc:
            classification = classify_upstream_error(exc)
            breaker.record_failure()
            if not classification.retryable:
                raise FailoverExhausted(
                    str(exc),
                    last_error=exc,
                    attempts=attempts
                    + [
                        {
                            "provider_id": provider.id,
                            "ok": False,
                            "error": str(exc),
                            "status_code": classification.status_code,
                            "failover": False,
                            "retry_class": classification.retry_class.value,
                            "retry_reason": classification.reason,
                        }
                    ],
                    status_code=classification.status_code
                    or getattr(exc, "status_code", None)
                    or 400,
                ) from exc
            last_error = exc
            attempts.append(
                {
                    "provider_id": provider.id,
                    "ok": False,
                    "error": str(exc),
                    "failover": True,
                    "elapsed_ms": (clock() - attempt_start) * 1000,
                    "retry_class": classification.retry_class.value,
                    "retry_reason": classification.reason,
                }
            )
            continue
        except ProviderError as exc:
            classification = classify_upstream_error(exc)
            breaker.record_failure()
            if is_retryable(exc):
                last_error = exc
                attempts.append(
                    {
                        "provider_id": provider.id,
                        "ok": False,
                        "error": str(exc),
                        "failover": True,
                        "retry_class": classification.retry_class.value,
                        "retry_reason": classification.reason,
                    }
                )
                continue
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

"""Budgeted failover orchestration across providers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.failover.circuit_breaker import CircuitBreakerRegistry
from app.failover.retry import classify_upstream_error, is_retryable
from app.models import ChatRequest
from app.providers.base import Provider, ProviderError, UpstreamError, UpstreamTimeout


class FailoverExhausted(Exception):
    """All candidates failed within the failover budget."""

    def __init__(
        self,
        message: str,
        *,
        last_error: Exception | None = None,
        attempts: list[dict[str, Any]] | None = None,
        status_code: int = 504,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.last_error = last_error
        self.attempts = attempts or []
        self.status_code = status_code


@dataclass
class FailoverResult:
    response: dict[str, Any]
    provider_id: str
    attempts: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: float = 0.0


# Errors that should be considered for failover classification
_FAILOVER_TYPES = (UpstreamTimeout, UpstreamError, TimeoutError, ConnectionError)


class FailoverOrchestrator:
    def __init__(
        self,
        *,
        breakers: CircuitBreakerRegistry,
        default_timeout_ms: int = 500,
        failover_budget_ms: int = 500,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.breakers = breakers
        self.default_timeout_ms = default_timeout_ms
        self.failover_budget_ms = failover_budget_ms
        self._clock = clock or time.perf_counter

    async def chat(
        self,
        req: ChatRequest,
        candidates: list[Provider],
    ) -> FailoverResult:
        if not candidates:
            raise FailoverExhausted("no provider candidates", status_code=502)

        start = self._clock()
        budget_s = max(self.failover_budget_ms, 1) / 1000.0
        remaining_s = budget_s
        attempts: list[dict[str, Any]] = []
        last_error: Exception | None = None
        saw_try = False

        for provider in candidates:
            elapsed = self._clock() - start
            remaining_s = budget_s - elapsed
            if remaining_s <= 0:
                break

            breaker = self.breakers.get(provider.id)
            if not breaker.allow_request():
                attempts.append(
                    {
                        "provider_id": provider.id,
                        "skipped": True,
                        "reason": "circuit_open",
                        "circuit": breaker.state.value,
                    }
                )
                continue

            attempt_timeout_ms = min(
                self.default_timeout_ms,
                max(1, int(remaining_s * 1000)),
            )
            attempt_start = self._clock()
            saw_try = True
            try:
                result = await provider.chat(req, timeout_ms=attempt_timeout_ms)
                breaker.record_success()
                attempt_elapsed_ms = (self._clock() - attempt_start) * 1000
                attempts.append(
                    {
                        "provider_id": provider.id,
                        "ok": True,
                        "elapsed_ms": attempt_elapsed_ms,
                        "timeout_ms": attempt_timeout_ms,
                    }
                )
                return FailoverResult(
                    response=result,
                    provider_id=provider.id,
                    attempts=attempts,
                    elapsed_ms=(self._clock() - start) * 1000,
                )
            except _FAILOVER_TYPES as exc:
                classification = classify_upstream_error(exc)
                breaker.record_failure()
                if not classification.retryable:
                    attempts.append(
                        {
                            "provider_id": provider.id,
                            "ok": False,
                            "error": str(exc),
                            "status_code": classification.status_code,
                            "elapsed_ms": (self._clock() - attempt_start) * 1000,
                            "failover": False,
                            "retry_class": classification.retry_class.value,
                            "retry_reason": classification.reason,
                        }
                    )
                    raise FailoverExhausted(
                        str(exc),
                        last_error=exc,
                        attempts=attempts,
                        status_code=classification.status_code or getattr(exc, "status_code", None) or 400,
                    ) from exc
                last_error = exc
                attempts.append(
                    {
                        "provider_id": provider.id,
                        "ok": False,
                        "error": str(exc),
                        "status_code": classification.status_code or getattr(exc, "status_code", None),
                        "elapsed_ms": (self._clock() - attempt_start) * 1000,
                        "failover": True,
                        "retry_class": classification.retry_class.value,
                        "retry_reason": classification.reason,
                    }
                )
                continue
            except ProviderError as exc:
                classification = classify_upstream_error(exc)
                breaker.record_failure()
                last_error = exc
                attempts.append(
                    {
                        "provider_id": provider.id,
                        "ok": False,
                        "error": str(exc),
                        "status_code": classification.status_code or getattr(exc, "status_code", None),
                        "elapsed_ms": (self._clock() - attempt_start) * 1000,
                        "failover": is_retryable(exc),
                        "retry_class": classification.retry_class.value,
                        "retry_reason": classification.reason,
                    }
                )
                if is_retryable(exc):
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
        raise FailoverExhausted(
            msg,
            last_error=last_error,
            attempts=attempts,
            status_code=status,
        )

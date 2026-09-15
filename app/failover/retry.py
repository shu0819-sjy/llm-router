"""Central retryable vs non-retryable upstream error classification.

Shared by FailoverOrchestrator and SSE pre-first-byte failover.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.providers.base import ProviderError, UpstreamError, UpstreamTimeout


class RetryClass(str, Enum):
    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"


@dataclass(frozen=True)
class Classification:
    retry_class: RetryClass
    reason: str
    status_code: int | None = None

    @property
    def retryable(self) -> bool:
        return self.retry_class is RetryClass.RETRYABLE


# Explicit matrix (documented for operators / HARDENING_PLAN F07):
# Retryable: timeouts, connection failures, HTTP 408/429, HTTP 5xx
# Non-retryable: other HTTP 4xx (auth/validation/model), generic ProviderError


def classify_upstream_error(exc: BaseException) -> Classification:
    """Classify an upstream/provider exception for failover decisions."""
    if isinstance(exc, UpstreamTimeout):
        return Classification(RetryClass.RETRYABLE, "timeout", getattr(exc, "status_code", None))
    if isinstance(exc, (TimeoutError, ConnectionError, ConnectionResetError, BrokenPipeError)):
        return Classification(RetryClass.RETRYABLE, "transport", None)
    if isinstance(exc, UpstreamError):
        code = exc.status_code
        if code is None:
            # Connection-ish upstream failures without status → retry
            return Classification(RetryClass.RETRYABLE, "upstream_no_status", None)
        if code in (408, 429) or code >= 500:
            return Classification(RetryClass.RETRYABLE, f"http_{code}", code)
        if 400 <= code < 500:
            return Classification(RetryClass.NON_RETRYABLE, f"http_{code}", code)
        return Classification(RetryClass.RETRYABLE, f"http_{code}", code)
    if isinstance(exc, ProviderError):
        # Local/adapter errors (bad config, unsupported) — do not hop
        return Classification(
            RetryClass.NON_RETRYABLE,
            "provider_error",
            getattr(exc, "status_code", None),
        )
    # Unknown — be conservative and do not infinite-hop; treat as non-retryable
    return Classification(RetryClass.NON_RETRYABLE, "unknown", None)


def is_retryable(exc: BaseException) -> bool:
    return classify_upstream_error(exc).retryable


def should_failover(exc: BaseException) -> bool:
    """Alias used by orchestrator/SSE: True → try next candidate."""
    return is_retryable(exc)


def classification_as_dict(exc: BaseException) -> dict[str, Any]:
    c = classify_upstream_error(exc)
    return {
        "retryable": c.retryable,
        "reason": c.reason,
        "status_code": c.status_code,
        "retry_class": c.retry_class.value,
    }

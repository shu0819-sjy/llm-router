"""Classic per-key token bucket (process-local)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable


class RateLimitExceeded(Exception):
    def __init__(self, message: str, *, retry_after_s: float, limit: float, remaining: float):
        super().__init__(message)
        self.message = message
        self.retry_after_s = retry_after_s
        self.limit = limit
        self.remaining = remaining


@dataclass
class _Bucket:
    tokens: float
    updated_at: float
    capacity: float
    refill_per_s: float


class TokenBucketLimiter:
    """
    Per-API-key token bucket.

    capacity / refill_per_s approximate RPM burst control.
    cost_per_req deducts tokens per accepted request (default 1).
    """

    def __init__(
        self,
        *,
        capacity: float = 60.0,
        refill_per_s: float = 1.0,
        cost_per_req: float = 1.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.default_capacity = float(capacity)
        self.default_refill_per_s = float(refill_per_s)
        self.cost_per_req = float(cost_per_req)
        self._clock = clock or time.monotonic
        self._buckets: dict[str, _Bucket] = {}
        self._lock = Lock()

    def _get_bucket(
        self,
        key_id: str,
        *,
        capacity: float | None = None,
        refill_per_s: float | None = None,
    ) -> _Bucket:
        cap = float(capacity) if capacity is not None else self.default_capacity
        refill = float(refill_per_s) if refill_per_s is not None else self.default_refill_per_s
        now = self._clock()
        b = self._buckets.get(key_id)
        if b is None:
            b = _Bucket(tokens=cap, updated_at=now, capacity=cap, refill_per_s=refill)
            self._buckets[key_id] = b
            return b
        # Allow per-key overrides to update live params
        b.capacity = cap
        b.refill_per_s = refill
        elapsed = max(0.0, now - b.updated_at)
        b.tokens = min(b.capacity, b.tokens + elapsed * b.refill_per_s)
        b.updated_at = now
        return b

    def allow(
        self,
        key_id: str,
        *,
        capacity: float | None = None,
        refill_per_s: float | None = None,
        cost: float | None = None,
    ) -> tuple[float, float]:
        """
        Try to consume tokens. Returns (limit, remaining).
        Raises RateLimitExceeded when insufficient.
        """
        need = self.cost_per_req if cost is None else float(cost)
        with self._lock:
            b = self._get_bucket(key_id, capacity=capacity, refill_per_s=refill_per_s)
            if b.tokens >= need:
                b.tokens -= need
                return b.capacity, max(0.0, b.tokens)
            missing = need - b.tokens
            retry = missing / b.refill_per_s if b.refill_per_s > 0 else 60.0
            raise RateLimitExceeded(
                "Rate limit exceeded",
                retry_after_s=retry,
                limit=b.capacity,
                remaining=max(0.0, b.tokens),
            )

    def peek(self, key_id: str) -> tuple[float, float]:
        with self._lock:
            b = self._get_bucket(key_id)
            return b.capacity, max(0.0, b.tokens)

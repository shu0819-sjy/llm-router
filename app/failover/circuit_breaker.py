"""In-memory circuit breaker: closed → open → half-open."""

from __future__ import annotations

import time
from enum import Enum
from threading import Lock
from typing import Callable


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-provider consecutive-failure circuit breaker."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_timeout_s: float = 30.0,
        half_open_max: int = 1,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout_s = max(0.0, recovery_timeout_s)
        self.half_open_max = max(1, half_open_max)
        self._clock = clock or time.monotonic
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_inflight = 0
        self._lock = Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_transition_locked()
            return self._state

    def allow_request(self) -> bool:
        with self._lock:
            self._maybe_transition_locked()
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                return False
            # half-open
            if self._half_open_inflight < self.half_open_max:
                self._half_open_inflight += 1
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._half_open_inflight = 0
            self._opened_at = None
            self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._state == CircuitState.HALF_OPEN:
                self._trip_open_locked()
                return
            if self._consecutive_failures >= self.failure_threshold:
                self._trip_open_locked()

    def _trip_open_locked(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()
        self._half_open_inflight = 0

    def _maybe_transition_locked(self) -> None:
        if self._state != CircuitState.OPEN or self._opened_at is None:
            return
        if self._clock() - self._opened_at >= self.recovery_timeout_s:
            self._state = CircuitState.HALF_OPEN
            self._half_open_inflight = 0

    def snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "consecutive_failures": self._consecutive_failures,
            "opened_at": self._opened_at,
        }


class CircuitBreakerRegistry:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_timeout_s: float = 30.0,
        half_open_max: int = 1,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout_s = recovery_timeout_s
        self._half_open_max = half_open_max
        self._clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = Lock()

    def get(self, provider_id: str) -> CircuitBreaker:
        with self._lock:
            if provider_id not in self._breakers:
                self._breakers[provider_id] = CircuitBreaker(
                    failure_threshold=self._failure_threshold,
                    recovery_timeout_s=self._recovery_timeout_s,
                    half_open_max=self._half_open_max,
                    clock=self._clock,
                )
            return self._breakers[provider_id]

    def states(self) -> dict[str, str]:
        with self._lock:
            return {pid: b.state.value for pid, b in self._breakers.items()}

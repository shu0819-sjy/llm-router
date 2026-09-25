"""Minimal Prometheus text exposition (no prometheus_client dependency).

Label policy (v0.3 hardening):
- Only provider ids and whitelisted status strings are used as label values.
- Every label value passes ``sanitize_metric_label``: control characters are
  flattened, secret-like substrings are redacted, length is capped, and
  Prometheus text-exposition escaping is applied — so hostile or accidental
  values cannot inject series or leak keys.
- Request IDs, raw API keys, arbitrary model names, and free-form upstream
  error text must NEVER be used as labels: they explode cardinality and can
  leak secrets. Statuses are whitelisted via ``sanitize_status_label``.
- Series cardinality is capped at ``MAX_SERIES`` per metric family.
"""

from __future__ import annotations

import re
from threading import Lock
from typing import Iterable

MAX_LABEL_LENGTH = 64
MAX_SERIES = 1024

# Deliberately over-approximate: key-like runs are redacted from labels.
_SECRET_LABEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"\b[0-9a-fA-F]{16,}\b"),
)

# Bounded status label values (chat.py / limiter statuses).
_KNOWN_STATUSES = frozenset({"ok", "failover", "error", "rate_limited", "other"})


def sanitize_metric_label(value: object, *, max_length: int = MAX_LABEL_LENGTH) -> str:
    """Bound, redact, and escape a value before using it as a label."""
    text = "" if value is None else str(value)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for pattern in _SECRET_LABEL_PATTERNS:
        text = pattern.sub("[redacted]", text)
    if len(text) > max_length:
        text = text[:max_length]
    # Prometheus text exposition escaping (order matters).
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def sanitize_status_label(status: object) -> str:
    """Map a status to a whitelisted label value (unknown → "other")."""
    text = sanitize_metric_label(status, max_length=32)
    return text if text in _KNOWN_STATUSES else "other"


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self.requests_total: dict[tuple[str, str], int] = {}
        self.failover_total: dict[tuple[str, str], int] = {}
        self.rate_limited_total: int = 0
        self.tokens_prompt: int = 0
        self.tokens_completion: int = 0
        self.circuit_state: dict[str, int] = {}  # 0 closed / 1 half-open / 2 open
        self.upstream_latency_ms_sum: dict[str, float] = {}
        self.upstream_latency_ms_count: dict[str, int] = {}

    @staticmethod
    def _series_allowed(store: dict, key: object) -> bool:
        """True when a new series may be created (cardinality cap)."""
        return key in store or len(store) < MAX_SERIES

    def inc_request(self, provider: str, status: str) -> None:
        key = (sanitize_metric_label(provider), sanitize_status_label(status))
        with self._lock:
            if not self._series_allowed(self.requests_total, key):
                return
            self.requests_total[key] = self.requests_total.get(key, 0) + 1

    def inc_failover(self, from_provider: str, to_provider: str) -> None:
        key = (sanitize_metric_label(from_provider), sanitize_metric_label(to_provider))
        with self._lock:
            if not self._series_allowed(self.failover_total, key):
                return
            self.failover_total[key] = self.failover_total.get(key, 0) + 1

    def inc_rate_limited(self) -> None:
        with self._lock:
            self.rate_limited_total += 1

    def add_tokens(self, prompt: int, completion: int) -> None:
        with self._lock:
            self.tokens_prompt += max(0, int(prompt))
            self.tokens_completion += max(0, int(completion))

    def set_circuit(self, provider: str, state: str) -> None:
        mapping = {"closed": 0, "half_open": 1, "open": 2}
        label = sanitize_metric_label(provider)
        with self._lock:
            if not self._series_allowed(self.circuit_state, label):
                return
            self.circuit_state[label] = mapping.get(state, 0)

    def observe_latency(self, provider: str, ms: float) -> None:
        label = sanitize_metric_label(provider)
        with self._lock:
            if not self._series_allowed(self.upstream_latency_ms_sum, label):
                return
            self.upstream_latency_ms_sum[label] = self.upstream_latency_ms_sum.get(
                label, 0.0
            ) + float(ms)
            self.upstream_latency_ms_count[label] = self.upstream_latency_ms_count.get(label, 0) + 1


def render_prometheus(reg: MetricsRegistry, *, extra_circuits: dict[str, str] | None = None) -> str:
    lines: list[str] = []
    lines.append("# HELP llm_router_requests_total Total chat requests by provider and status")
    lines.append("# TYPE llm_router_requests_total counter")
    for (provider, status), val in sorted(reg.requests_total.items()):
        lines.append(f'llm_router_requests_total{{provider="{provider}",status="{status}"}} {val}')

    lines.append("# HELP llm_router_failover_total Failover hops")
    lines.append("# TYPE llm_router_failover_total counter")
    for (src, dst), val in sorted(reg.failover_total.items()):
        lines.append(
            f'llm_router_failover_total{{from_provider="{src}",to_provider="{dst}"}} {val}'
        )

    lines.append("# HELP llm_router_rate_limited_total Rate limited requests")
    lines.append("# TYPE llm_router_rate_limited_total counter")
    lines.append(f"llm_router_rate_limited_total {reg.rate_limited_total}")

    lines.append("# HELP llm_router_tokens_total Token counters")
    lines.append("# TYPE llm_router_tokens_total counter")
    lines.append(f'llm_router_tokens_total{{direction="prompt"}} {reg.tokens_prompt}')
    lines.append(f'llm_router_tokens_total{{direction="completion"}} {reg.tokens_completion}')

    lines.append("# HELP llm_router_circuit_state Circuit state (0 closed, 1 half-open, 2 open)")
    lines.append("# TYPE llm_router_circuit_state gauge")
    circuits = dict(reg.circuit_state)
    if extra_circuits:
        mapping = {"closed": 0, "half_open": 1, "open": 2}
        for pid, st in extra_circuits.items():
            circuits[sanitize_metric_label(pid)] = mapping.get(st, 0)
    for provider, val in sorted(circuits.items()):
        lines.append(f'llm_router_circuit_state{{provider="{provider}"}} {val}')

    lines.append("# HELP llm_router_upstream_latency_ms_sum Upstream latency sum")
    lines.append("# TYPE llm_router_upstream_latency_ms_sum counter")
    for provider, val in sorted(reg.upstream_latency_ms_sum.items()):
        lines.append(f'llm_router_upstream_latency_ms_sum{{provider="{provider}"}} {val}')
    lines.append("# HELP llm_router_upstream_latency_ms_count Upstream latency count")
    lines.append("# TYPE llm_router_upstream_latency_ms_count counter")
    for provider, val in sorted(reg.upstream_latency_ms_count.items()):
        lines.append(f'llm_router_upstream_latency_ms_count{{provider="{provider}"}} {val}')

    return "\n".join(lines) + "\n"


def _unused_iterable_hint() -> Iterable[str]:
    return ()

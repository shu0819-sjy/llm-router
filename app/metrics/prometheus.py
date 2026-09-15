"""Minimal Prometheus text exposition (no prometheus_client dependency)."""

from __future__ import annotations

from threading import Lock
from typing import Iterable


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

    def inc_request(self, provider: str, status: str) -> None:
        with self._lock:
            key = (provider, status)
            self.requests_total[key] = self.requests_total.get(key, 0) + 1

    def inc_failover(self, from_provider: str, to_provider: str) -> None:
        with self._lock:
            key = (from_provider, to_provider)
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
        with self._lock:
            self.circuit_state[provider] = mapping.get(state, 0)

    def observe_latency(self, provider: str, ms: float) -> None:
        with self._lock:
            self.upstream_latency_ms_sum[provider] = (
                self.upstream_latency_ms_sum.get(provider, 0.0) + float(ms)
            )
            self.upstream_latency_ms_count[provider] = (
                self.upstream_latency_ms_count.get(provider, 0) + 1
            )


def render_prometheus(reg: MetricsRegistry, *, extra_circuits: dict[str, str] | None = None) -> str:
    lines: list[str] = []
    lines.append("# HELP llm_router_requests_total Total chat requests by provider and status")
    lines.append("# TYPE llm_router_requests_total counter")
    for (provider, status), val in sorted(reg.requests_total.items()):
        lines.append(
            f'llm_router_requests_total{{provider="{provider}",status="{status}"}} {val}'
        )

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
    lines.append(
        f'llm_router_tokens_total{{direction="completion"}} {reg.tokens_completion}'
    )

    lines.append("# HELP llm_router_circuit_state Circuit state (0 closed, 1 half-open, 2 open)")
    lines.append("# TYPE llm_router_circuit_state gauge")
    circuits = dict(reg.circuit_state)
    if extra_circuits:
        mapping = {"closed": 0, "half_open": 1, "open": 2}
        for pid, st in extra_circuits.items():
            circuits[pid] = mapping.get(st, 0)
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

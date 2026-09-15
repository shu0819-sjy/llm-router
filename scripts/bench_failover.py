#!/usr/bin/env python3
"""
Failover switch latency benchmark (no live paid APIs).

Simulates a primary UpstreamTimeout then secondary success using in-process
FakeProviders + FailoverOrchestrator. Reports wall-clock switch latency and
asserts it stays under the 500ms P1 budget.

Usage (from repo root):
  python scripts/bench_failover.py
  python scripts/bench_failover.py --rounds 50 --primary-delay-ms 80
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

# Allow `python scripts/bench_failover.py` from repo root without install
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.failover.circuit_breaker import CircuitBreakerRegistry  # noqa: E402
from app.failover.orchestrator import FailoverOrchestrator  # noqa: E402
from app.models import ChatRequest  # noqa: E402
from app.providers.base import Provider, UpstreamTimeout  # noqa: E402


class BenchProvider(Provider):
    def __init__(
        self,
        provider_id: str,
        prefixes: list[str],
        *,
        delay_s: float = 0.0,
        fail_times: int = 0,
    ) -> None:
        self.id = provider_id
        self.supported_prefixes = prefixes
        self.enabled = True
        self.delay_s = delay_s
        self.fail_times = fail_times
        self._calls = 0

    async def health(self) -> bool:
        return True

    async def chat(self, req: ChatRequest, *, timeout_ms: int) -> dict[str, Any]:
        self._calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self._calls <= self.fail_times:
            raise UpstreamTimeout(f"{self.id} simulated timeout")
        return {
            "id": f"chatcmpl-{self.id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": f"ok:{self.id}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    async def chat_stream(
        self, req: ChatRequest, *, timeout_ms: int
    ) -> AsyncIterator[bytes]:
        result = await self.chat(req, timeout_ms=timeout_ms)
        yield f"data: {result}\n\n".encode()
        yield b"data: [DONE]\n\n"


async def run_once(
    *,
    primary_delay_ms: float,
    secondary_delay_ms: float,
    budget_ms: int,
    timeout_ms: int,
) -> float:
    primary = BenchProvider(
        "primary",
        ["deepseek-"],
        delay_s=primary_delay_ms / 1000.0,
        fail_times=1,
    )
    secondary = BenchProvider(
        "secondary",
        ["gpt-"],
        delay_s=secondary_delay_ms / 1000.0,
        fail_times=0,
    )
    orch = FailoverOrchestrator(
        breakers=CircuitBreakerRegistry(failure_threshold=5),
        default_timeout_ms=timeout_ms,
        failover_budget_ms=budget_ms,
    )
    req = ChatRequest(model="deepseek-chat", messages=[{"role": "user", "content": "bench"}])
    t0 = time.perf_counter()
    result = await orch.chat(req, [primary, secondary])
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    if result.provider_id != "secondary":
        raise RuntimeError(f"expected secondary, got {result.provider_id}")
    return elapsed_ms


async def main_async(args: argparse.Namespace) -> int:
    samples: list[float] = []
    for i in range(args.rounds):
        ms = await run_once(
            primary_delay_ms=args.primary_delay_ms,
            secondary_delay_ms=args.secondary_delay_ms,
            budget_ms=args.budget_ms,
            timeout_ms=args.timeout_ms,
        )
        samples.append(ms)
        if args.verbose:
            print(f"  round {i + 1:02d}: {ms:.2f} ms")

    samples_sorted = sorted(samples)
    p50 = statistics.median(samples_sorted)
    p95 = samples_sorted[max(0, int(len(samples_sorted) * 0.95) - 1)]
    mean = statistics.fmean(samples_sorted)
    mx = max(samples_sorted)

    print("llm-router failover benchmark (fakes, no live APIs)")
    print(f"  rounds={args.rounds} primary_delay={args.primary_delay_ms}ms "
          f"secondary_delay={args.secondary_delay_ms}ms budget={args.budget_ms}ms")
    print(f"  mean={mean:.2f}ms  p50={p50:.2f}ms  p95={p95:.2f}ms  max={mx:.2f}ms")
    print(f"  budget_ok={mx < args.budget_ms}  (require max < {args.budget_ms}ms)")

    if mx >= args.budget_ms:
        print("FAIL: failover switch exceeded budget", file=sys.stderr)
        return 1
    print("PASS: failover switch under budget")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rounds", type=int, default=20)
    p.add_argument("--primary-delay-ms", type=float, default=80.0)
    p.add_argument("--secondary-delay-ms", type=float, default=20.0)
    p.add_argument("--budget-ms", type=int, default=500)
    p.add_argument("--timeout-ms", type=int, default=400)
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

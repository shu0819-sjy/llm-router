"""Leaf cost helpers + price quote type (no imports from database/ledger/storage)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PriceQuote:
    model: str
    input_per_1m_usd: float
    output_per_1m_usd: float
    version: str
    effective_from: str


def estimate_cost(
    prompt_tokens: int,
    completion_tokens: int,
    input_per_1m_usd: float,
    output_per_1m_usd: float,
) -> float:
    return (
        prompt_tokens / 1_000_000.0 * input_per_1m_usd
        + completion_tokens / 1_000_000.0 * output_per_1m_usd
    )

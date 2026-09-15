"""Usage event writer + cost estimation."""

from __future__ import annotations

import uuid
from typing import Any

from app.db.database import Database, utc_now_iso


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


class UsageLedger:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def record(
        self,
        *,
        api_key_id: int | None,
        provider_id: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
        status: str = "ok",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        price = await self.db.get_model_price(model)
        if price:
            cost = estimate_cost(prompt_tokens, completion_tokens, price[0], price[1])
        else:
            cost = 0.0
        total = int(prompt_tokens) + int(completion_tokens)
        rid = request_id or f"req_{uuid.uuid4().hex[:16]}"
        cur = await self.db.execute(
            """
            INSERT INTO usage_events(
                api_key_id, provider_id, model,
                prompt_tokens, completion_tokens, total_tokens,
                cost_usd, latency_ms, status, request_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                api_key_id,
                provider_id,
                model,
                int(prompt_tokens),
                int(completion_tokens),
                total,
                float(cost),
                int(latency_ms),
                status,
                rid,
                utc_now_iso(),
            ),
        )
        return {
            "id": int(cur.lastrowid),
            "api_key_id": api_key_id,
            "provider_id": provider_id,
            "model": model,
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "total_tokens": total,
            "cost_usd": float(cost),
            "latency_ms": int(latency_ms),
            "status": status,
            "request_id": rid,
        }

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT id, api_key_id, provider_id, model, prompt_tokens, completion_tokens,
                   total_tokens, cost_usd, latency_ms, status, request_id, created_at
            FROM usage_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]

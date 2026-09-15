"""Usage event writer + cost estimation with price-version snapshots."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from app.db.cost import estimate_cost
from app.db.database import Database, utc_now_iso

# "estimated" remains in the type for forward-compatible stored rows, but
# v0.2.0 gateway paths only write actual|unavailable (no invented estimates).
AccountingStatus = Literal["actual", "estimated", "unavailable"]

# Re-export for `from app.db.ledger import estimate_cost`
__all__ = ["AccountingStatus", "UsageLedger", "estimate_cost"]


class UsageLedger:
    """
    SQLite-backed usage store. Satisfies the UsageStore port in app.db.storage.

    accounting_status:
      - actual: tokens observed from upstream usage payload
      - unavailable: tokens unknown (e.g. stream without a usage-bearing chunk);
        cost is not invented in this case
      - estimated: reserved / unused by v0.2.0 gateway writers
    """

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
        accounting_status: AccountingStatus | str = "actual",
    ) -> dict[str, Any]:
        quote = await self.db.get_price(model)
        if quote and accounting_status != "unavailable":
            cost = estimate_cost(
                prompt_tokens,
                completion_tokens,
                quote.input_per_1m_usd,
                quote.output_per_1m_usd,
            )
            price_version = quote.version
            in_price: float | None = quote.input_per_1m_usd
            out_price: float | None = quote.output_per_1m_usd
        elif quote and accounting_status == "unavailable":
            cost = 0.0
            price_version = quote.version
            in_price = quote.input_per_1m_usd
            out_price = quote.output_per_1m_usd
        else:
            cost = 0.0
            price_version = None
            in_price = None
            out_price = None

        if accounting_status == "unavailable":
            prompt_tokens = 0
            completion_tokens = 0

        total = int(prompt_tokens) + int(completion_tokens)
        rid = request_id or f"req_{uuid.uuid4().hex[:16]}"
        cur = await self.db.execute(
            """
            INSERT INTO usage_events(
                api_key_id, provider_id, model,
                prompt_tokens, completion_tokens, total_tokens,
                cost_usd, latency_ms, status, request_id,
                accounting_status, price_version,
                input_price_per_1m_usd, output_price_per_1m_usd,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                str(accounting_status),
                price_version,
                in_price,
                out_price,
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
            "accounting_status": str(accounting_status),
            "price_version": price_version,
            "input_price_per_1m_usd": in_price,
            "output_price_per_1m_usd": out_price,
        }

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT id, api_key_id, provider_id, model, prompt_tokens, completion_tokens,
                   total_tokens, cost_usd, latency_ms, status, request_id,
                   accounting_status, price_version,
                   input_price_per_1m_usd, output_price_per_1m_usd, created_at
            FROM usage_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]

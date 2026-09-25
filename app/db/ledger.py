"""Usage event writer + cost estimation with price-version snapshots."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from typing import Any, Literal

from app.db.cost import estimate_cost
from app.db.database import Database, utc_now_iso

# "estimated" remains in the type for forward-compatible stored rows, but
# v0.2.0 gateway paths only write actual|unavailable (no invented estimates).
AccountingStatus = Literal["actual", "estimated", "unavailable"]

logger = logging.getLogger("llm_router.ledger")

# Bounded write retry for SQLite lock/busy contention (PRAGMAs owned by Database).
_WRITE_RETRY_ATTEMPTS = 5
_WRITE_RETRY_BASE_DELAY_S = 0.02
_RETRYABLE_FRAGMENTS = ("database is locked", "database is busy", "busy")

# Re-export for `from app.db.ledger import estimate_cost`
__all__ = ["AccountingStatus", "UsageLedger", "estimate_cost", "is_retryable_db_error"]


def is_retryable_db_error(exc: BaseException) -> bool:
    """True for SQLite lock/busy errors that warrant a bounded write retry."""
    # aiosqlite.OperationalError subclasses sqlite3.OperationalError.
    if not isinstance(exc, sqlite3.OperationalError) and type(exc).__name__ != "OperationalError":
        return False
    msg = str(exc).lower()
    return any(fragment in msg for fragment in _RETRYABLE_FRAGMENTS)


class UsageLedger:
    """
    SQLite-backed usage store. Satisfies the UsageStore port in app.db.storage.

    accounting_status:
      - actual: tokens observed from upstream usage payload
      - unavailable: tokens unknown (e.g. stream without a usage-bearing chunk);
        cost is not invented in this case
      - estimated: reserved / unused by v0.2.0 gateway writers

    Write path uses bounded exponential backoff on lock/busy OperationalError.
    SQLite WAL / busy_timeout PRAGMAs are applied by the Database owner (or
    ensured at app startup); this class does not own connection configuration.
    """

    def __init__(
        self,
        db: Database,
        *,
        write_retries: int = _WRITE_RETRY_ATTEMPTS,
        retry_base_delay_s: float = _WRITE_RETRY_BASE_DELAY_S,
    ) -> None:
        self.db = db
        self.write_retries = max(1, int(write_retries))
        self.retry_base_delay_s = max(0.0, float(retry_base_delay_s))
        self._ttfb_column_ready: bool | None = None

    async def _has_ttfb_column(self) -> bool:
        """
        Detect usage_events.ttfb_ms from Database._migrate_columns / schema.sql.

        Migration ownership stays on Database; the ledger only consumes the
        column when present (no ALTER here).
        """
        if self._ttfb_column_ready is not None:
            return self._ttfb_column_ready
        try:
            rows = await self.db.fetchall("PRAGMA table_info(usage_events)")
            cols = {str(r["name"]) for r in rows}
            self._ttfb_column_ready = "ttfb_ms" in cols
            if not self._ttfb_column_ready:
                logger.warning(
                    "usage_events.ttfb_ms missing — stream TTFB will not be persisted "
                    "(Database migration should add it)"
                )
            return self._ttfb_column_ready
        except Exception as exc:  # noqa: BLE001 — closed handle / non-SQLite
            logger.warning("ttfb_ms column detect skipped: %s", exc)
            self._ttfb_column_ready = False
            return False

    async def _execute_with_retry(self, sql: str, params: tuple[Any, ...] | list[Any]) -> Any:
        last: BaseException | None = None
        for attempt in range(self.write_retries):
            try:
                return await self.db.execute(sql, params)
            except Exception as exc:
                last = exc
                if not is_retryable_db_error(exc) or attempt >= self.write_retries - 1:
                    raise
                delay = self.retry_base_delay_s * (2**attempt)
                logger.debug(
                    "usage write retry %s/%s after %s (sleep %.3fs)",
                    attempt + 1,
                    self.write_retries,
                    exc,
                    delay,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
        assert last is not None
        raise last

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
        ttfb_ms: int | None = None,
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
        ttfb_value = int(ttfb_ms) if ttfb_ms is not None else 0
        has_ttfb = await self._has_ttfb_column()

        if has_ttfb:
            cur = await self._execute_with_retry(
                """
                INSERT INTO usage_events(
                    api_key_id, provider_id, model,
                    prompt_tokens, completion_tokens, total_tokens,
                    cost_usd, latency_ms, status, request_id,
                    accounting_status, price_version,
                    input_price_per_1m_usd, output_price_per_1m_usd,
                    created_at, ttfb_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    ttfb_value,
                ),
            )
        else:
            cur = await self._execute_with_retry(
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
            "ttfb_ms": ttfb_value if has_ttfb else None,
            "status": status,
            "request_id": rid,
            "accounting_status": str(accounting_status),
            "price_version": price_version,
            "input_price_per_1m_usd": in_price,
            "output_price_per_1m_usd": out_price,
        }

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        has_ttfb = await self._has_ttfb_column()
        ttfb_select = ", ttfb_ms" if has_ttfb else ""
        rows = await self.db.fetchall(
            f"""
            SELECT id, api_key_id, provider_id, model, prompt_tokens, completion_tokens,
                   total_tokens, cost_usd, latency_ms, status, request_id,
                   accounting_status, price_version,
                   input_price_per_1m_usd, output_price_per_1m_usd, created_at
                   {ttfb_select}
            FROM usage_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]

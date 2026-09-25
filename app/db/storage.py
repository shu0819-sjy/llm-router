"""
Narrow storage ports (seams) for API keys, usage ledger, and model prices.

SQLite remains the default production adapter. Redis/PostgreSQL are *not*
shipped here — only Protocol contracts + an in-memory fake for tests, plus
optional extension points documented on the Protocols.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

from app.db.cost import PriceQuote, estimate_cost


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# Re-export for callers that imported PriceQuote from app.db.storage
__all__ = [
    "ApiKeyStore",
    "InMemoryStorage",
    "PriceQuote",
    "PriceStore",
    "StorageBackend",
    "StorageBundle",
    "UsageStore",
]

StorageBackend = Literal["sqlite", "memory"]


@runtime_checkable
class ApiKeyStore(Protocol):
    """Port for API key persistence. Future Redis/PG adapters may implement this."""

    async def upsert_api_key(
        self,
        *,
        raw_key: str,
        name: str,
        provider_id: str | None = None,
        model_allowlist: str | None = None,
        rate_capacity: float | None = None,
        rate_refill_per_s: float | None = None,
        is_active: bool = True,
        source: str = "panel",
        update_active: bool = True,
    ) -> int: ...

    async def get_api_key_id_by_raw(self, raw_key: str) -> int | None: ...


@runtime_checkable
class PriceStore(Protocol):
    """Port for versioned model prices."""

    async def get_price(self, model: str, *, at: str | None = None) -> PriceQuote | None: ...

    async def set_price(
        self,
        model: str,
        input_per_1m_usd: float,
        output_per_1m_usd: float,
        *,
        version: str | None = None,
        effective_from: str | None = None,
    ) -> PriceQuote: ...

    async def list_model_ids(self) -> list[str]: ...


@runtime_checkable
class UsageStore(Protocol):
    """Port for usage / cost ledger rows."""

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
        accounting_status: str = "actual",
        ttfb_ms: int | None = None,
    ) -> dict[str, Any]: ...

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class StorageBundle:
    """
    Injectable storage ports consumed by routing / ledger / panel / health paths.

    ``backend`` is only ``sqlite`` (default production) or ``memory`` (tests).
    This package does **not** ship Redis or PostgreSQL adapters.
    """

    api_keys: ApiKeyStore
    prices: PriceStore
    usage: UsageStore
    backend: StorageBackend = "sqlite"

    @staticmethod
    def from_memory(store: InMemoryStorage | None = None) -> StorageBundle:
        mem = store or InMemoryStorage()
        return StorageBundle(api_keys=mem, prices=mem, usage=mem, backend="memory")


@dataclass
class InMemoryStorage:
    """
    Test double implementing ApiKeyStore + PriceStore + UsageStore.

    Not a production backend — proves the seam is swappable without SQLite.
    """

    _keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    _key_seq: int = 0
    _prices: dict[str, PriceQuote] = field(default_factory=dict)
    _price_history: dict[str, list[PriceQuote]] = field(default_factory=dict)
    _events: list[dict[str, Any]] = field(default_factory=list)
    _event_seq: int = 0

    async def upsert_api_key(
        self,
        *,
        raw_key: str,
        name: str,
        provider_id: str | None = None,
        model_allowlist: str | None = None,
        rate_capacity: float | None = None,
        rate_refill_per_s: float | None = None,
        is_active: bool = True,
        source: str = "panel",
        update_active: bool = True,
    ) -> int:
        existing = self._keys.get(raw_key)
        if existing:
            existing.update(
                {
                    "name": name,
                    "provider_id": provider_id,
                    "model_allowlist": model_allowlist,
                    "rate_capacity": rate_capacity,
                    "rate_refill_per_s": rate_refill_per_s,
                    "source": source,
                }
            )
            if update_active:
                existing["is_active"] = is_active
            return int(existing["id"])
        self._key_seq += 1
        self._keys[raw_key] = {
            "id": self._key_seq,
            "name": name,
            "provider_id": provider_id,
            "model_allowlist": model_allowlist,
            "rate_capacity": rate_capacity,
            "rate_refill_per_s": rate_refill_per_s,
            "is_active": is_active,
            "source": source,
        }
        return self._key_seq

    async def get_api_key_id_by_raw(self, raw_key: str) -> int | None:
        row = self._keys.get(raw_key)
        if not row or not row.get("is_active", True):
            return None
        return int(row["id"])

    async def get_price(self, model: str, *, at: str | None = None) -> PriceQuote | None:
        direct = self._price_at(model, at)
        if direct:
            return direct
        # longest-prefix fallback
        best: PriceQuote | None = None
        best_len = -1
        m = model.lower()
        for key in self._prices:
            quote = self._price_at(key, at)
            if quote is None:
                continue
            k = key.lower()
            if m.startswith(k) and len(k) > best_len:
                best = quote
                best_len = len(k)
        return best

    def _price_at(self, model: str, at: str | None) -> PriceQuote | None:
        """从内存历史中返回指定时刻生效的价格。"""
        if at is None:
            return self._prices.get(model)
        candidates = [
            quote for quote in self._price_history.get(model, []) if quote.effective_from <= at
        ]
        return max(candidates, key=lambda quote: quote.effective_from, default=None)

    async def set_price(
        self,
        model: str,
        input_per_1m_usd: float,
        output_per_1m_usd: float,
        *,
        version: str | None = None,
        effective_from: str | None = None,
    ) -> PriceQuote:
        existing = self._prices.get(model)
        if version is None:
            if existing and existing.version.startswith("v"):
                try:
                    n = int(existing.version[1:]) + 1
                    version = f"v{n}"
                except ValueError:
                    version = "v2"
            else:
                version = "v1"
        quote = PriceQuote(
            model=model,
            input_per_1m_usd=float(input_per_1m_usd),
            output_per_1m_usd=float(output_per_1m_usd),
            version=version,
            effective_from=effective_from or _utc_now_iso(),
        )
        self._price_history.setdefault(model, []).append(quote)
        current = self._prices.get(model)
        if current is None or quote.effective_from >= current.effective_from:
            self._prices[model] = quote
        return quote

    async def list_model_ids(self) -> list[str]:
        return sorted(self._prices.keys())

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
        accounting_status: str = "actual",
        ttfb_ms: int | None = None,
    ) -> dict[str, Any]:
        quote = await self.get_price(model)
        if quote:
            cost = estimate_cost(
                prompt_tokens,
                completion_tokens,
                quote.input_per_1m_usd,
                quote.output_per_1m_usd,
            )
            price_version = quote.version
            in_price = quote.input_per_1m_usd
            out_price = quote.output_per_1m_usd
        else:
            cost = 0.0
            price_version = None
            in_price = None
            out_price = None
        # Unavailable accounting: do not invent token-based cost
        if accounting_status == "unavailable":
            cost = 0.0
        total = int(prompt_tokens) + int(completion_tokens)
        rid = request_id or f"req_{uuid.uuid4().hex[:16]}"
        self._event_seq += 1
        row = {
            "id": self._event_seq,
            "api_key_id": api_key_id,
            "provider_id": provider_id,
            "model": model,
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "total_tokens": total,
            "cost_usd": float(cost),
            "latency_ms": int(latency_ms),
            "ttfb_ms": int(ttfb_ms) if ttfb_ms is not None else None,
            "status": status,
            "request_id": rid,
            "accounting_status": accounting_status,
            "price_version": price_version,
            "input_price_per_1m_usd": in_price,
            "output_price_per_1m_usd": out_price,
            "created_at": _utc_now_iso(),
        }
        self._events.append(row)
        return dict(row)

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(reversed(self._events[-limit:]))

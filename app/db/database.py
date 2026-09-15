"""Async SQLite engine (aiosqlite) + schema bootstrap."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Seed prices (USD per 1M tokens) — illustrative defaults for v0.1
_DEFAULT_PRICES: list[tuple[str, float, float]] = [
    ("gpt-4o-mini", 0.15, 0.60),
    ("gpt-4o", 2.50, 10.00),
    ("deepseek-chat", 0.14, 0.28),
    ("deepseek-reasoner", 0.55, 2.19),
    ("claude-3-5-sonnet-latest", 3.00, 15.00),
    ("claude-3-haiku-20240307", 0.25, 1.25),
    ("qwen-turbo", 0.30, 0.60),
    ("qwen-plus", 0.80, 2.00),
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self.init_schema()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def init_schema(self) -> None:
        assert self._conn is not None
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        await self._conn.executescript(sql)
        await self._conn.commit()
        await self.seed_prices()

    async def seed_prices(self) -> None:
        assert self._conn is not None
        for model, inp, out in _DEFAULT_PRICES:
            await self._conn.execute(
                """
                INSERT OR IGNORE INTO model_prices(model, input_per_1m_usd, output_per_1m_usd)
                VALUES (?, ?, ?)
                """,
                (model, inp, out),
            )
        await self._conn.commit()

    async def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> aiosqlite.Cursor:
        assert self._conn is not None
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cur

    async def fetchone(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> aiosqlite.Row | None:
        assert self._conn is not None
        cur = await self._conn.execute(sql, params)
        return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[aiosqlite.Row]:
        assert self._conn is not None
        cur = await self._conn.execute(sql, params)
        return await cur.fetchall()

    async def ping(self) -> bool:
        try:
            row = await self.fetchone("SELECT 1 AS ok")
            return row is not None and int(row["ok"]) == 1
        except Exception:
            return False

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
    ) -> int:
        key_hash = hash_api_key(raw_key)
        existing = await self.fetchone(
            "SELECT id FROM api_keys WHERE key_hash = ?", (key_hash,)
        )
        if existing:
            await self.execute(
                """
                UPDATE api_keys
                SET name = ?, provider_id = ?, model_allowlist = ?,
                    rate_capacity = ?, rate_refill_per_s = ?, is_active = ?
                WHERE id = ?
                """,
                (
                    name,
                    provider_id,
                    model_allowlist,
                    rate_capacity,
                    rate_refill_per_s,
                    1 if is_active else 0,
                    int(existing["id"]),
                ),
            )
            return int(existing["id"])
        cur = await self.execute(
            """
            INSERT INTO api_keys(
                key_hash, name, provider_id, model_allowlist,
                rate_capacity, rate_refill_per_s, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key_hash,
                name,
                provider_id,
                model_allowlist,
                rate_capacity,
                rate_refill_per_s,
                1 if is_active else 0,
                utc_now_iso(),
            ),
        )
        return int(cur.lastrowid)

    async def get_api_key_id_by_raw(self, raw_key: str) -> int | None:
        row = await self.fetchone(
            "SELECT id FROM api_keys WHERE key_hash = ? AND is_active = 1",
            (hash_api_key(raw_key),),
        )
        return int(row["id"]) if row else None

    async def get_model_price(self, model: str) -> tuple[float, float] | None:
        row = await self.fetchone(
            "SELECT input_per_1m_usd, output_per_1m_usd FROM model_prices WHERE model = ?",
            (model,),
        )
        if not row:
            # prefix fallback: longest matching seeded prefix
            rows = await self.fetchall("SELECT model, input_per_1m_usd, output_per_1m_usd FROM model_prices")
            best: tuple[float, float] | None = None
            best_len = -1
            m = model.lower()
            for r in rows:
                key = str(r["model"]).lower()
                if m.startswith(key) and len(key) > best_len:
                    best = (float(r["input_per_1m_usd"]), float(r["output_per_1m_usd"]))
                    best_len = len(key)
            return best
        return float(row["input_per_1m_usd"]), float(row["output_per_1m_usd"])


# Optional process-global (tests inject via app.state.db)
_db: Database | None = None


def get_db() -> Database | None:
    return _db


def set_db(db: Database | None) -> None:
    global _db
    _db = db

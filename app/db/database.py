"""Async SQLite engine (aiosqlite) + schema bootstrap."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from app.db.cost import PriceQuote

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# SQLite reliability defaults applied on every connect().
# Bounded write retries for lock/busy live on UsageLedger (ledger consumer).
SQLITE_BUSY_TIMEOUT_MS = 5000

# Seed prices (USD per 1M tokens) — illustrative defaults
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
    """
    SQLite adapter implementing the storage ports used by the app.

    Redis / PostgreSQL are not implemented; swap via app.state injection of
    objects satisfying ApiKeyStore / PriceStore / UsageStore (see app.db.storage).
    """

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
        await self._apply_reliability_pragmas()
        await self.init_schema()

    async def _apply_reliability_pragmas(self) -> None:
        """
        WAL + busy_timeout + NORMAL synchronous for concurrent write reliability.

        Idempotent; safe to re-run from app startup helpers. Write-path retries
        for transient ``database is locked`` remain on UsageLedger.
        """
        assert self._conn is not None
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        await self._conn.execute("PRAGMA synchronous=NORMAL")

    async def close(self) -> None:
        if self._conn is not None:
            # Best-effort WAL checkpoint so -wal/-shm settle before close.
            try:
                await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            await self._conn.close()
            self._conn = None

    async def init_schema(self) -> None:
        assert self._conn is not None
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        await self._conn.executescript(sql)
        await self._conn.commit()
        await self._migrate_columns()
        await self.seed_prices()

    async def _table_columns(self, table: str) -> set[str]:
        assert self._conn is not None
        cur = await self._conn.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        return {str(r[1]) for r in rows}

    async def _migrate_columns(self) -> None:
        """Additive migrations for DBs created under older schemas."""
        assert self._conn is not None
        usage_cols = await self._table_columns("usage_events")
        for col, ddl in (
            ("accounting_status", "TEXT NOT NULL DEFAULT 'actual'"),
            ("price_version", "TEXT"),
            ("input_price_per_1m_usd", "REAL"),
            ("output_price_per_1m_usd", "REAL"),
            # Stream time-to-first-byte; additive for v0.2 → v0.3 databases.
            ("ttfb_ms", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in usage_cols:
                await self._conn.execute(
                    f"ALTER TABLE usage_events ADD COLUMN {col} {ddl}"
                )

        price_cols = await self._table_columns("model_prices")
        for col, ddl in (
            ("version", "TEXT NOT NULL DEFAULT 'v1'"),
            ("effective_from", "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'"),
        ):
            if col not in price_cols:
                await self._conn.execute(
                    f"ALTER TABLE model_prices ADD COLUMN {col} {ddl}"
                )

        key_cols = await self._table_columns("api_keys")
        if "source" not in key_cols:
            # Existing rows default to panel-managed; hydrate/sync retags env keys.
            await self._conn.execute(
                "ALTER TABLE api_keys ADD COLUMN source TEXT NOT NULL DEFAULT 'panel'"
            )
        await self._conn.commit()

    async def seed_prices(self) -> None:
        assert self._conn is not None
        now = utc_now_iso()
        for model, inp, out in _DEFAULT_PRICES:
            await self._conn.execute(
                """
                INSERT OR IGNORE INTO model_prices(
                    model, input_per_1m_usd, output_per_1m_usd, version, effective_from
                ) VALUES (?, ?, ?, 'v1', ?)
                """,
                (model, inp, out, now),
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
        source: str | None = None,
        update_active: bool = False,
    ) -> int:
        """
        Insert or update an API key by hash.

        ``source`` is ``'env'`` (LLM_ROUTER_API_KEYS) or ``'panel'`` (admin UI).
        On update, ``is_active`` is left unchanged unless ``update_active=True``
        so environment re-assertion cannot silently undo a panel revocation.
        """
        key_hash = hash_api_key(raw_key)
        existing = await self.fetchone(
            "SELECT id, source FROM api_keys WHERE key_hash = ?", (key_hash,)
        )
        if existing:
            key_id = int(existing["id"])
            if update_active:
                await self.execute(
                    """
                    UPDATE api_keys
                    SET name = ?, provider_id = ?, model_allowlist = ?,
                        rate_capacity = ?, rate_refill_per_s = ?, is_active = ?,
                        source = COALESCE(?, source)
                    WHERE id = ?
                    """,
                    (
                        name,
                        provider_id,
                        model_allowlist,
                        rate_capacity,
                        rate_refill_per_s,
                        1 if is_active else 0,
                        source,
                        key_id,
                    ),
                )
            else:
                await self.execute(
                    """
                    UPDATE api_keys
                    SET name = ?, provider_id = ?, model_allowlist = ?,
                        rate_capacity = ?, rate_refill_per_s = ?,
                        source = COALESCE(?, source)
                    WHERE id = ?
                    """,
                    (
                        name,
                        provider_id,
                        model_allowlist,
                        rate_capacity,
                        rate_refill_per_s,
                        source,
                        key_id,
                    ),
                )
            return key_id
        src = source if source is not None else "panel"
        cur = await self.execute(
            """
            INSERT INTO api_keys(
                key_hash, name, provider_id, model_allowlist,
                rate_capacity, rate_refill_per_s, is_active, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key_hash,
                name,
                provider_id,
                model_allowlist,
                rate_capacity,
                rate_refill_per_s,
                1 if is_active else 0,
                src,
                utc_now_iso(),
            ),
        )
        return int(cur.lastrowid)

    async def sync_environment_keys(self, items: list[dict[str, Any]]) -> dict[str, int]:
        """
        Upsert keys from ``LLM_ROUTER_API_KEYS`` with ``source='env'`` and
        deactivate environment-managed keys whose hashes are no longer present.

        Panel-managed keys are never deactivated by this method.
        Present env keys are refreshed but not force-reactivated (``update_active``
        stays false) so panel revocation remains durable against env re-assertion.
        """
        present_hashes: set[str] = set()
        upserted = 0
        for item in items:
            raw = str(item["key"])
            present_hashes.add(hash_api_key(raw))
            await self.upsert_api_key(
                raw_key=raw,
                name=str(item.get("name") or "env"),
                provider_id=item.get("provider_id"),  # type: ignore[arg-type]
                source="env",
                is_active=True,
                update_active=False,
            )
            upserted += 1
        deactivated = await self.deactivate_missing_env_keys(present_hashes)
        return {"upserted": upserted, "deactivated": deactivated}

    async def deactivate_missing_env_keys(self, present_hashes: set[str]) -> int:
        """Deactivate ``source='env'`` rows whose key_hash is not in ``present_hashes``."""
        rows = await self.fetchall(
            "SELECT id, key_hash FROM api_keys WHERE source = 'env' AND is_active = 1"
        )
        deactivated = 0
        for row in rows:
            if str(row["key_hash"]) not in present_hashes:
                await self.execute(
                    "UPDATE api_keys SET is_active = 0 WHERE id = ?",
                    (int(row["id"]),),
                )
                deactivated += 1
        return deactivated

    async def get_api_key_id_by_raw(self, raw_key: str) -> int | None:
        row = await self.fetchone(
            "SELECT id FROM api_keys WHERE key_hash = ? AND is_active = 1",
            (hash_api_key(raw_key),),
        )
        return int(row["id"]) if row else None

    async def get_model_price(self, model: str) -> tuple[float, float] | None:
        quote = await self.get_price(model)
        if quote is None:
            return None
        return quote.input_per_1m_usd, quote.output_per_1m_usd

    async def get_price(self, model: str, *, at: str | None = None) -> PriceQuote | None:
        """Return the current price quote (at is reserved for future history lookups)."""
        _ = at  # extension point for effective-at queries
        row = await self.fetchone(
            """
            SELECT model, input_per_1m_usd, output_per_1m_usd, version, effective_from
            FROM model_prices WHERE model = ?
            """,
            (model,),
        )
        if not row:
            rows = await self.fetchall(
                """
                SELECT model, input_per_1m_usd, output_per_1m_usd, version, effective_from
                FROM model_prices
                """
            )
            best: PriceQuote | None = None
            best_len = -1
            m = model.lower()
            for r in rows:
                key = str(r["model"]).lower()
                if m.startswith(key) and len(key) > best_len:
                    best = PriceQuote(
                        model=str(r["model"]),
                        input_per_1m_usd=float(r["input_per_1m_usd"]),
                        output_per_1m_usd=float(r["output_per_1m_usd"]),
                        version=str(r["version"] or "v1"),
                        effective_from=str(r["effective_from"] or "1970-01-01T00:00:00+00:00"),
                    )
                    best_len = len(key)
            return best
        return PriceQuote(
            model=str(row["model"]),
            input_per_1m_usd=float(row["input_per_1m_usd"]),
            output_per_1m_usd=float(row["output_per_1m_usd"]),
            version=str(row["version"] or "v1"),
            effective_from=str(row["effective_from"] or "1970-01-01T00:00:00+00:00"),
        )

    async def set_price(
        self,
        model: str,
        input_per_1m_usd: float,
        output_per_1m_usd: float,
        *,
        version: str | None = None,
        effective_from: str | None = None,
    ) -> PriceQuote:
        existing = await self.get_price(model)
        if version is None:
            if existing and str(existing.version).startswith("v"):
                try:
                    n = int(str(existing.version)[1:]) + 1
                    version = f"v{n}"
                except ValueError:
                    version = "v2"
            else:
                version = "v1"
        eff = effective_from or utc_now_iso()
        await self.execute(
            """
            INSERT INTO model_prices(model, input_per_1m_usd, output_per_1m_usd, version, effective_from)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(model) DO UPDATE SET
                input_per_1m_usd = excluded.input_per_1m_usd,
                output_per_1m_usd = excluded.output_per_1m_usd,
                version = excluded.version,
                effective_from = excluded.effective_from
            """,
            (model, float(input_per_1m_usd), float(output_per_1m_usd), version, eff),
        )
        return PriceQuote(
            model=model,
            input_per_1m_usd=float(input_per_1m_usd),
            output_per_1m_usd=float(output_per_1m_usd),
            version=version,
            effective_from=eff,
        )

    async def list_model_ids(self) -> list[str]:
        rows = await self.fetchall("SELECT model FROM model_prices ORDER BY model")
        return [str(r["model"]) for r in rows]


# Optional process-global (tests inject via app.state.db)
_db: Database | None = None


def get_db() -> Database | None:
    return _db


def set_db(db: Database | None) -> None:
    global _db
    _db = db

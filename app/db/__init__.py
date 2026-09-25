"""SQLite persistence for keys, providers, usage/cost + storage ports."""

from app.db.cost import PriceQuote, estimate_cost
from app.db.database import Database, get_db
from app.db.ledger import UsageLedger
from app.db.storage import (
    ApiKeyStore,
    InMemoryStorage,
    PriceStore,
    StorageBackend,
    StorageBundle,
    UsageStore,
)


def build_sqlite_storage_bundle(
    db: Database, *, ledger: UsageLedger | None = None
) -> StorageBundle:
    """SQLite-backed StorageBundle (the only shipped production adapter)."""
    usage = ledger if ledger is not None else UsageLedger(db)
    return StorageBundle(api_keys=db, prices=db, usage=usage, backend="sqlite")


__all__ = [
    "ApiKeyStore",
    "Database",
    "InMemoryStorage",
    "PriceQuote",
    "PriceStore",
    "StorageBackend",
    "StorageBundle",
    "UsageLedger",
    "UsageStore",
    "build_sqlite_storage_bundle",
    "estimate_cost",
    "get_db",
]

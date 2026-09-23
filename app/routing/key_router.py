"""API-key lookup + model-prefix candidate selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from app.config import Settings, get_settings
from app.db.database import hash_api_key
from app.models import ApiKeyRecord
from app.providers.base import Provider
from app.providers.registry import ProviderRegistry

if TYPE_CHECKING:
    from app.db.database import Database


@dataclass
class RouteDecision:
    api_key: ApiKeyRecord
    primary: Provider | None
    candidates: list[Provider] = field(default_factory=list)
    reason: str = ""


def capabilities_for_tools_request(
    *,
    has_tools: bool = False,
    has_tool_choice: bool = False,
    has_response_format: bool = False,
) -> frozenset[str] | None:
    """
    Capability set required when a chat request carries tools / structured output.

    Returns None when no special capabilities are needed (plain chat).
    """
    needed: set[str] = set()
    if has_tools:
        needed.add("tools")
    if has_tool_choice:
        needed.add("tool_choice")
    if has_response_format:
        needed.add("structured_output")
    return frozenset(needed) if needed else None


def _parse_allowlist(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    return parts or None


def _model_allowlisted(api_key: ApiKeyRecord, model: str) -> bool:
    if not api_key.model_allowlist:
        return True
    m = model.lower()
    for pattern in api_key.model_allowlist:
        p = pattern.strip().lower()
        if p.endswith("*") and m.startswith(p[:-1]):
            return True
        if m == p or m.startswith(p):
            return True
    return False


def _record_from_db_row(row: Any, *, raw_key: str) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=int(row["id"]),
        name=str(row["name"]),
        key=raw_key,
        provider_id=row["provider_id"],
        model_allowlist=_parse_allowlist(row["model_allowlist"]),
        rate_capacity=float(row["rate_capacity"]) if row["rate_capacity"] is not None else None,
        rate_refill_per_s=(
            float(row["rate_refill_per_s"]) if row["rate_refill_per_s"] is not None else None
        ),
        is_active=bool(row["is_active"]),
    )


class KeyRouter:
    """
    Resolves bearer API keys via:
      1) env / hot in-memory map (fast path)
      2) SHA-256(bearer) → active api_keys.key_hash (SQLite)
    and builds ordered failover candidates.
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        settings: Settings | None = None,
        keys: list[ApiKeyRecord] | None = None,
        db: Database | None = None,
    ) -> None:
        self.registry = registry
        self.settings = settings or get_settings()
        self._db: Database | None = db
        self._keys_by_value: dict[str, ApiKeyRecord] = {}
        if keys is not None:
            for k in keys:
                self._keys_by_value[k.key] = k
        else:
            self.reload_from_settings()

    def bind_db(self, db: Database | None) -> None:
        """Attach / detach SQLite handle used for hash lookups."""
        self._db = db

    def reload_from_settings(self) -> None:
        """Env-key hot path — raw keys from LLM_ROUTER_API_KEYS."""
        self._keys_by_value.clear()
        for item in self.settings.parsed_api_keys():
            rec = ApiKeyRecord(
                name=str(item["name"]),
                key=str(item["key"]),
                provider_id=item.get("provider_id"),  # type: ignore[arg-type]
            )
            self._keys_by_value[rec.key] = rec

    def register_hot_key(self, record: ApiKeyRecord) -> None:
        """Process-local cache (e.g. right after panel create)."""
        if record.key and record.is_active:
            self._keys_by_value[record.key] = record

    def drop_hot_key(self, raw_key: str) -> None:
        self._keys_by_value.pop(raw_key, None)

    def authenticate(self, bearer_token: str | None) -> ApiKeyRecord | None:
        """
        Sync env/hot-path only. Prefer authenticate_async when a DB is bound
        so panel-persisted keys resolve across process restarts.
        """
        token = self._normalize_bearer(bearer_token)
        if not token:
            return None
        rec = self._keys_by_value.get(token)
        if rec is None or not rec.is_active:
            return None
        return rec

    async def authenticate_async(self, bearer_token: str | None) -> ApiKeyRecord | None:
        """Env hot path, then hash lookup against api_keys.key_hash."""
        token = self._normalize_bearer(bearer_token)
        if not token:
            return None

        # 1) Env / hot in-memory keys
        rec = self._keys_by_value.get(token)
        if rec is not None and rec.is_active:
            return rec

        # 2) SQLite hash lookup (survives restart; raw key never stored)
        if self._db is None or not self._db.connected:
            return None
        key_hash = hash_api_key(token)
        row = await self._db.fetchone(
            """
            SELECT id, key_hash, name, provider_id, model_allowlist,
                   rate_capacity, rate_refill_per_s, is_active, created_at
            FROM api_keys
            WHERE key_hash = ? AND is_active = 1
            """,
            (key_hash,),
        )
        if row is None:
            return None
        record = _record_from_db_row(row, raw_key=token)
        # Optional soft-cache for this process (still no persistence of raw key to disk)
        self._keys_by_value[token] = record
        return record

    async def hydrate_from_db(self) -> int:
        """
        Startup hook: tag env hot keys with source='env', apply DB overrides,
        and deactivate environment-managed keys that were removed from settings.

        Panel-managed keys (source='panel') are never deactivated here.
        Cannot restore arbitrary panel raw keys (only hashes are stored);
        those resolve lazily via authenticate_async hash lookup.
        Returns number of env keys linked to active DB rows.
        """
        if self._db is None or not self._db.connected:
            return 0

        present_hashes = {hash_api_key(raw) for raw in self._keys_by_value}
        linked = 0

        for raw, rec in list(self._keys_by_value.items()):
            row = await self._db.fetchone(
                """
                SELECT id, key_hash, name, provider_id, model_allowlist,
                       rate_capacity, rate_refill_per_s, is_active, created_at
                FROM api_keys
                WHERE key_hash = ?
                """,
                (hash_api_key(raw),),
            )
            if row is None:
                continue

            # Mark as environment-managed (additive source metadata).
            await self._db.execute(
                "UPDATE api_keys SET source = 'env' WHERE id = ?",
                (int(row["id"]),),
            )

            # DB revocation wins over env re-assertion: drop inactive from hot cache.
            if not bool(row["is_active"]):
                self._keys_by_value.pop(raw, None)
                continue

            # Apply DB overrides onto env hot record
            rec.id = int(row["id"])
            if row["provider_id"]:
                rec.provider_id = row["provider_id"]
            allow = _parse_allowlist(row["model_allowlist"])
            if allow is not None:
                rec.model_allowlist = allow
            if row["rate_capacity"] is not None:
                rec.rate_capacity = float(row["rate_capacity"])
            if row["rate_refill_per_s"] is not None:
                rec.rate_refill_per_s = float(row["rate_refill_per_s"])
            linked += 1

        # Revoke environment-managed keys removed from LLM_ROUTER_API_KEYS.
        await self._db.deactivate_missing_env_keys(present_hashes)

        return linked

    @staticmethod
    def _normalize_bearer(bearer_token: str | None) -> str | None:
        if not bearer_token:
            return None
        token = bearer_token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token or None

    def resolve(
        self,
        api_key: ApiKeyRecord,
        model: str,
        *,
        require_capabilities: Iterable[str] | None = None,
    ) -> RouteDecision:
        """
        Build failover candidates for ``model``.

        Candidates include only enabled providers whose ``supported_prefixes``
        match the model (longest-prefix first, then ``provider_order`` among
        the matching set). Unrelated providers are never appended.

        When ``require_capabilities`` is set (e.g. tools / structured output),
        only providers declaring every required capability remain.
        """
        order = self.settings.provider_order_list()
        needed = frozenset(require_capabilities) if require_capabilities else None

        # Allowlist applies to both forced and auto-routed keys.
        if api_key.model_allowlist and not _model_allowlisted(api_key, model):
            return RouteDecision(
                api_key=api_key,
                primary=None,
                candidates=[],
                reason="model_not_allowlisted",
            )

        # Key forces a single provider
        if api_key.provider_id:
            forced = self.registry.get(api_key.provider_id)
            if forced is None or not forced.enabled:
                return RouteDecision(
                    api_key=api_key,
                    primary=None,
                    candidates=[],
                    reason=f"forced provider {api_key.provider_id} unavailable",
                )
            if not forced.supports_model(model):
                return RouteDecision(
                    api_key=api_key,
                    primary=None,
                    candidates=[],
                    reason="forced_provider_model_mismatch",
                )
            if needed and not forced.supports_capabilities(needed):
                return RouteDecision(
                    api_key=api_key,
                    primary=None,
                    candidates=[],
                    reason="forced_provider_capability_mismatch",
                )
            return RouteDecision(
                api_key=api_key,
                primary=forced,
                candidates=[forced],
                reason="key_forced_provider",
            )

        matched = self.registry.compatible(model, require_capabilities=needed)

        candidates: list[Provider] = []
        seen: set[str] = set()
        matched_by_id = {p.id: p for p in matched}

        def _add(p: Provider | None) -> None:
            if p is None or not p.enabled or p.id in seen:
                return
            if p.id not in matched_by_id:
                return
            seen.add(p.id)
            candidates.append(p)

        # Longest-prefix primary first, then configured provider_order, then rest.
        _add(matched[0] if matched else None)
        for pid in order:
            _add(matched_by_id.get(pid) or self.registry.get(pid))
        for p in matched:
            _add(p)

        if not candidates:
            return RouteDecision(
                api_key=api_key,
                primary=None,
                candidates=[],
                reason="model_not_supported",
            )

        return RouteDecision(
            api_key=api_key,
            primary=candidates[0],
            candidates=candidates,
            reason="model_prefix",
        )

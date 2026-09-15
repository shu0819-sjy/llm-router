"""Panel static UI + JSON admin APIs."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from app.db.database import hash_api_key, utc_now_iso
from app.models import ApiKeyRecord
from app.panel.auth import require_admin

router = APIRouter(prefix="/panel", tags=["panel"])

_STATIC_DIR = Path(__file__).resolve().parent / "static"


class CreateKeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    provider_id: str | None = None
    model_allowlist: str | None = None
    rate_capacity: float | None = None
    rate_refill_per_s: float | None = None


class PatchProviderBody(BaseModel):
    enabled: bool | None = None
    base_url: str | None = None


@router.get("/")
async def panel_index() -> Any:
    index = _STATIC_DIR / "index.html"
    if not index.is_file():
        return HTMLResponse("<h1>Panel assets missing</h1>", status_code=500)
    return FileResponse(index, media_type="text/html")


@router.get("/static/{asset_path:path}")
async def panel_static(asset_path: str) -> FileResponse:
    target = (_STATIC_DIR / asset_path).resolve()
    if not str(target).startswith(str(_STATIC_DIR.resolve())) or not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    media = "application/javascript" if target.suffix == ".js" else "text/css"
    if target.suffix == ".html":
        media = "text/html"
    return FileResponse(target, media_type=media)


@router.get("/api/keys")
async def list_keys(
    request: Request,
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    db = request.app.state.db
    rows = await db.fetchall(
        """
        SELECT id, key_hash, name, provider_id, model_allowlist,
               rate_capacity, rate_refill_per_s, is_active, created_at
        FROM api_keys
        ORDER BY id DESC
        """
    )
    keys = []
    for r in rows:
        h = str(r["key_hash"])
        keys.append(
            {
                "id": int(r["id"]),
                "name": r["name"],
                "key_prefix": h[:8] + "…",
                "provider_id": r["provider_id"],
                "model_allowlist": r["model_allowlist"],
                "rate_capacity": r["rate_capacity"],
                "rate_refill_per_s": r["rate_refill_per_s"],
                "is_active": bool(r["is_active"]),
                "created_at": r["created_at"],
            }
        )
    return {"keys": keys}


@router.post("/api/keys")
async def create_key(
    body: CreateKeyBody,
    request: Request,
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    db = request.app.state.db
    raw = "sk-" + secrets.token_urlsafe(24)
    key_id = await db.upsert_api_key(
        raw_key=raw,
        name=body.name,
        provider_id=body.provider_id,
        model_allowlist=body.model_allowlist,
        rate_capacity=body.rate_capacity,
        rate_refill_per_s=body.rate_refill_per_s,
        is_active=True,
    )
    # Hot-register into in-memory router (never expose upstream provider secrets)
    router_keys = request.app.state.key_router
    router_keys.register_hot_key(
        ApiKeyRecord(
            id=key_id,
            name=body.name,
            key=raw,
            provider_id=body.provider_id,
            model_allowlist=(
                [p.strip() for p in body.model_allowlist.split(",") if p.strip()]
                if body.model_allowlist
                else None
            ),
            rate_capacity=body.rate_capacity,
            rate_refill_per_s=body.rate_refill_per_s,
            is_active=True,
        )
    )
    return {
        "id": key_id,
        "name": body.name,
        "key": raw,  # shown once
        "key_prefix": hash_api_key(raw)[:8] + "…",
        "provider_id": body.provider_id,
        "warning": "Store this key now; it will not be shown again.",
    }


@router.delete("/api/keys/{key_id}")
async def delete_key(
    key_id: int,
    request: Request,
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    db = request.app.state.db
    row = await db.fetchone("SELECT id, key_hash FROM api_keys WHERE id = ?", (key_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Key not found")
    await db.execute(
        "UPDATE api_keys SET is_active = 0 WHERE id = ?",
        (key_id,),
    )
    # Remove matching in-memory keys by id
    kr = request.app.state.key_router
    for raw, rec in list(kr._keys_by_value.items()):  # noqa: SLF001
        if rec.id == key_id:
            rec.is_active = False
            del kr._keys_by_value[raw]
    return {"ok": True, "id": key_id, "deactivated": True}


@router.get("/api/providers")
async def list_providers(
    request: Request,
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    registry = request.app.state.registry
    breakers = request.app.state.breakers
    out = []
    for p in registry.all():
        # Never include upstream API keys / secrets in panel responses
        base_url = getattr(p, "base_url", "") or ""
        out.append(
            {
                "id": p.id,
                "enabled": p.enabled,
                "base_url": base_url,
                "circuit": breakers.get(p.id).state.value,
                "supported_prefixes": list(p.supported_prefixes),
            }
        )
    return {"providers": out}


@router.patch("/api/providers/{provider_id}")
async def patch_provider(
    provider_id: str,
    body: PatchProviderBody,
    request: Request,
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    registry = request.app.state.registry
    provider = registry.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    if body.enabled is not None:
        provider.enabled = body.enabled
    if body.base_url is not None and hasattr(provider, "base_url"):
        provider.base_url = body.base_url.rstrip("/")
    # Persist lightweight row (no secrets)
    db = request.app.state.db
    await db.execute(
        """
        INSERT INTO providers(id, base_url, api_key_enc, enabled, weight, updated_at)
        VALUES (?, ?, NULL, ?, 100, ?)
        ON CONFLICT(id) DO UPDATE SET
            base_url = excluded.base_url,
            enabled = excluded.enabled,
            updated_at = excluded.updated_at
        """,
        (
            provider_id,
            getattr(provider, "base_url", "") or "",
            1 if provider.enabled else 0,
            utc_now_iso(),
        ),
    )
    breakers = request.app.state.breakers
    return {
        "id": provider_id,
        "enabled": provider.enabled,
        "base_url": getattr(provider, "base_url", ""),
        "circuit": breakers.get(provider_id).state.value,
    }


@router.get("/api/usage")
async def usage_summary(
    request: Request,
    _: str = Depends(require_admin),
    api_key_id: int | None = Query(default=None),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
) -> dict[str, Any]:
    db = request.app.state.db
    clauses = ["1=1"]
    params: list[Any] = []
    if api_key_id is not None:
        clauses.append("api_key_id = ?")
        params.append(api_key_id)
    if from_ts:
        clauses.append("created_at >= ?")
        params.append(from_ts)
    if to_ts:
        clauses.append("created_at <= ?")
        params.append(to_ts)
    where = " AND ".join(clauses)
    row = await db.fetchone(
        f"""
        SELECT
            COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COALESCE(SUM(cost_usd), 0) AS cost_usd,
            COUNT(*) AS requests
        FROM usage_events
        WHERE {where}
        """,
        params,
    )
    by_provider = await db.fetchall(
        f"""
        SELECT provider_id,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COALESCE(SUM(cost_usd), 0) AS cost_usd,
               COUNT(*) AS requests
        FROM usage_events
        WHERE {where}
        GROUP BY provider_id
        ORDER BY cost_usd DESC
        """,
        params,
    )
    return {
        "summary": {
            "prompt_tokens": int(row["prompt_tokens"]) if row else 0,
            "completion_tokens": int(row["completion_tokens"]) if row else 0,
            "total_tokens": int(row["total_tokens"]) if row else 0,
            "cost_usd": float(row["cost_usd"]) if row else 0.0,
            "requests": int(row["requests"]) if row else 0,
        },
        "by_provider": [dict(r) for r in by_provider],
    }


@router.get("/api/usage/recent")
async def usage_recent(
    request: Request,
    _: str = Depends(require_admin),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    ledger = request.app.state.ledger
    rows = await ledger.recent(limit=limit)
    # Strip nothing sensitive — usage rows have no upstream secrets
    return {"events": rows}

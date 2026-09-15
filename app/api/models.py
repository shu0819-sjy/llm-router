"""GET /v1/models — OpenAI-compatible model list (chat models only)."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request

from app.auth import require_api_key
from app.models import ApiKeyRecord, ModelCard, ModelListResponse

# Static catalog of chat models known to the router. Embeddings / images / audio
# are intentionally absent — this project does not advertise unsupported APIs.
_FALLBACK_MODELS: tuple[str, ...] = (
    "gpt-4o-mini",
    "gpt-4o",
    "deepseek-chat",
    "deepseek-reasoner",
    "claude-3-5-sonnet-latest",
    "claude-3-haiku-20240307",
    "qwen-turbo",
    "qwen-plus",
)


def _owned_by(model_id: str) -> str:
    lower = model_id.lower()
    if lower.startswith("gpt-") or lower.startswith("o1") or lower.startswith("o3"):
        return "openai"
    if lower.startswith("deepseek"):
        return "deepseek"
    if lower.startswith("claude"):
        return "anthropic"
    if lower.startswith("qwen"):
        return "qwen"
    return "llm-router"


async def list_model_ids(request: Request) -> list[str]:
    """Prefer priced models from storage; fall back to the static chat catalog."""
    db = getattr(request.app.state, "db", None)
    ids: list[str] = []
    if db is not None and hasattr(db, "list_model_ids"):
        try:
            ids = list(await db.list_model_ids())
        except Exception:
            ids = []
    if not ids:
        if db is not None and getattr(db, "connected", False):
            try:
                rows = await db.fetchall(
                    "SELECT DISTINCT model FROM model_prices ORDER BY model"
                )
                ids = [str(r["model"]) for r in rows]
            except Exception:
                ids = []
    if not ids:
        ids = list(_FALLBACK_MODELS)
    return ids


async def list_models(
    request: Request,
    _api_key: ApiKeyRecord = Depends(require_api_key),
) -> dict[str, Any]:
    """OpenAI-shaped model list. Mounted on the /v1 chat router."""
    ids = await list_model_ids(request)
    data = [
        ModelCard(id=mid, created=0, owned_by=_owned_by(mid)).model_dump()
        for mid in ids
    ]
    return {"object": "list", "data": data}


# Re-export for OpenAPI typing convenience
__all__ = ["list_models", "list_model_ids", "ModelListResponse", "_FALLBACK_MODELS"]

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


def _model_visible_to_key(router: Any, api_key: ApiKeyRecord, model: str) -> bool:
    """
    Authorization-aware catalog visibility.

    Unlike chat routing, listing does not require the provider to be currently
    enabled (upstream API key present). Fresh installs can still see catalog
    models while forced-provider / allowlist constraints still hide the rest.
    """
    decision = router.resolve(api_key, model)
    if decision.reason == "model_not_allowlisted":
        return False
    if decision.reason == "forced_provider_model_mismatch":
        return False
    if decision.reason.startswith("forced provider") and "unavailable" in decision.reason:
        return False
    if decision.candidates:
        return True

    registry = getattr(router, "registry", None)
    if registry is None:
        return False
    if api_key.provider_id:
        forced = registry.get(api_key.provider_id)
        return forced is not None and forced.supports_model(model)
    return any(p.supports_model(model) for p in registry.all())


async def list_model_ids(
    request: Request,
    api_key: ApiKeyRecord | None = None,
) -> list[str]:
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
                rows = await db.fetchall("SELECT DISTINCT model FROM model_prices ORDER BY model")
                ids = [str(r["model"]) for r in rows]
            except Exception:
                ids = []
    if not ids:
        ids = list(_FALLBACK_MODELS)
    router = getattr(request.app.state, "key_router", None)
    if router is None or api_key is None:
        return ids
    return [mid for mid in ids if _model_visible_to_key(router, api_key, mid)]


async def list_models(
    request: Request,
    _api_key: ApiKeyRecord = Depends(require_api_key),
) -> dict[str, Any]:
    """OpenAI-shaped model list. Mounted on the /v1 chat router."""
    ids = await list_model_ids(request, _api_key)
    data = [ModelCard(id=mid, created=0, owned_by=_owned_by(mid)).model_dump() for mid in ids]
    return {"object": "list", "data": data}


__all__ = ["list_models", "list_model_ids", "ModelListResponse", "_FALLBACK_MODELS"]

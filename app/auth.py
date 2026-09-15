"""Bearer API-key extraction helpers."""

from __future__ import annotations

from fastapi import Header, HTTPException, Request

from app.models import ApiKeyRecord
from app.routing.key_router import KeyRouter


def extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return authorization.strip() or None
    return parts[1].strip() or None


async def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
) -> ApiKeyRecord:
    router: KeyRouter = request.app.state.key_router
    record = await router.authenticate_async(authorization)
    if record is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "Invalid or missing API key",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        )
    return record

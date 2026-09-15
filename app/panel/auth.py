"""Admin bearer token check for panel mutating/read APIs."""

from __future__ import annotations

from fastapi import Header, HTTPException, Request


async def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    settings = request.app.state.settings
    expected = (settings.admin_token or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": "Admin token not configured (LLM_ROUTER_ADMIN_TOKEN)",
                    "type": "api_error",
                    "code": "admin_not_configured",
                }
            },
        )
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or token != expected:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "Invalid admin token",
                    "type": "invalid_request_error",
                    "code": "invalid_admin_token",
                }
            },
        )
    return token

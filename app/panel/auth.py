"""Admin bearer token check for panel mutating/read APIs."""

from __future__ import annotations

from fastapi import Header, HTTPException, Request

from app.auth import (
    DEFAULT_ADMIN_TOKEN_MIN_LENGTH,
    admin_token_quality_issues,
    assert_secure_admin_token,
    constant_time_token_equals,
    extract_bearer,
    is_development_mode,
)


async def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    settings = request.app.state.settings

    # Outside development, refuse weak/placeholder admin configuration entirely.
    if not is_development_mode(settings):
        min_length = int(
            getattr(settings, "admin_token_min_length", DEFAULT_ADMIN_TOKEN_MIN_LENGTH)
            or DEFAULT_ADMIN_TOKEN_MIN_LENGTH
        )
        if admin_token_quality_issues(settings.admin_token, min_length=min_length):
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "message": (
                            "Admin token missing or insecure. Set a strong "
                            "LLM_ROUTER_ADMIN_TOKEN or use LLM_ROUTER_ENV=development "
                            "for local development only."
                        ),
                        "type": "api_error",
                        "code": "admin_insecure_config",
                    }
                },
            )

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

    token = extract_bearer(authorization) or ""
    if not constant_time_token_equals(token, expected):
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


def validate_settings_admin_token(settings) -> None:
    """Startup helper re-exported for main.create_app."""
    assert_secure_admin_token(settings)

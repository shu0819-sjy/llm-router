"""Bearer API-key extraction helpers + admin token security primitives."""

from __future__ import annotations

import hmac
import secrets
from typing import TYPE_CHECKING

from fastapi import Header, HTTPException, Request

from app.models import ApiKeyRecord
from app.routing.key_router import KeyRouter

if TYPE_CHECKING:
    from app.config import Settings

# Well-known insecure placeholders — rejected outside development mode.
INSECURE_ADMIN_TOKENS = frozenset(
    {
        "",
        "change-me",
        "change-me-admin-token",
        "admin",
        "admin-token",
        "password",
        "secret",
        "token",
        "llm-router-admin",
    }
)


def extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return authorization.strip() or None
    return parts[1].strip() or None


def is_development_mode(settings: Settings) -> bool:
    env = (getattr(settings, "env", None) or "production").strip().lower()
    return env in {"development", "dev", "test"}


def normalize_admin_token(value: str | None) -> str:
    return (value or "").strip()


def is_insecure_admin_token(token: str | None) -> bool:
    normalized = normalize_admin_token(token)
    if not normalized:
        return True
    return normalized.lower() in INSECURE_ADMIN_TOKENS


def assert_secure_admin_token(settings: Settings) -> None:
    """
    Hard-fail outside development when admin token is missing or a known placeholder.
    Call from application startup.
    """
    if is_development_mode(settings):
        return
    if is_insecure_admin_token(settings.admin_token):
        raise RuntimeError(
            "LLM_ROUTER_ADMIN_TOKEN is missing or uses an insecure placeholder. "
            "Set a strong unique token, or set LLM_ROUTER_ENV=development for local use only."
        )


def constant_time_token_equals(provided: str | None, expected: str | None) -> bool:
    """Constant-time comparison for admin bearer tokens."""
    a = normalize_admin_token(provided).encode("utf-8")
    b = normalize_admin_token(expected).encode("utf-8")
    if not a or not b:
        # Still run compare_digest on equal-length dummy buffers to reduce
        # trivial short-circuit timing differences on empty inputs.
        dummy = secrets.token_bytes(32)
        hmac.compare_digest(dummy, dummy)
        return False
    # hmac.compare_digest requires equal length; hash both sides to fixed length.
    ha = hmac.new(b"llm-router-admin-cmp", a, digestmod="sha256").digest()
    hb = hmac.new(b"llm-router-admin-cmp", b, digestmod="sha256").digest()
    return hmac.compare_digest(ha, hb)


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

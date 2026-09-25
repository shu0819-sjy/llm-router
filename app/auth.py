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


# ---------------------------------------------------------------------------
# Production admin-token quality policy (v0.3)
#
# A production admin token (LLM_ROUTER_ADMIN_TOKEN) must satisfy ALL of:
#   1. Present and not a well-known placeholder (INSECURE_ADMIN_TOKENS).
#   2. At least ``min_length`` characters (default 24; configurable via
#      LLM_ROUTER_ADMIN_TOKEN_MIN_LENGTH, floor 8).
#   3. No embedded whitespace.
#   4. At least 3 of the 4 character classes: lowercase, uppercase, digits,
#      symbols — so trivially guessable / single-class strings are rejected.
#   5. Not a single repeated character.
#
# Development mode (LLM_ROUTER_ENV=development|dev|test) remains an explicit
# escape hatch: the policy is not enforced there so local uvicorn works with
# placeholder tokens, but the deployment is then explicitly non-production.
# ---------------------------------------------------------------------------

DEFAULT_ADMIN_TOKEN_MIN_LENGTH = 24


def admin_token_quality_issues(
    token: str | None,
    *,
    min_length: int = DEFAULT_ADMIN_TOKEN_MIN_LENGTH,
) -> list[str]:
    """Return the list of policy violations for an admin token (empty = compliant)."""
    normalized = normalize_admin_token(token)
    if not normalized:
        return ["token is empty (set LLM_ROUTER_ADMIN_TOKEN)"]
    if is_insecure_admin_token(normalized):
        return ["token is a well-known insecure placeholder"]
    issues: list[str] = []
    if any(ch.isspace() for ch in normalized):
        issues.append("token contains whitespace")
    if len(normalized) < min_length:
        issues.append(f"token is shorter than the required minimum of {min_length} characters")
    classes = (
        any(c.islower() for c in normalized),
        any(c.isupper() for c in normalized),
        any(c.isdigit() for c in normalized),
        any(not c.isalnum() for c in normalized),
    )
    if sum(classes) < 3:
        issues.append("token must mix at least 3 of: lowercase, uppercase, digits, symbols")
    if len(set(normalized)) == 1:
        issues.append("token is a single repeated character")
    return issues


def assert_secure_admin_token(settings: Settings) -> None:
    """
    Hard-fail outside development when the admin token violates the quality
    policy above. Call from application startup.
    """
    if is_development_mode(settings):
        return
    min_length = int(
        getattr(settings, "admin_token_min_length", DEFAULT_ADMIN_TOKEN_MIN_LENGTH)
        or DEFAULT_ADMIN_TOKEN_MIN_LENGTH
    )
    issues = admin_token_quality_issues(settings.admin_token, min_length=min_length)
    if issues:
        raise RuntimeError(
            "LLM_ROUTER_ADMIN_TOKEN fails the production admin-token policy: "
            + "; ".join(issues)
            + ". Set a strong unique token (>= "
            + str(min_length)
            + " characters, mixed character classes) or set "
            "LLM_ROUTER_ENV=development for local use only."
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

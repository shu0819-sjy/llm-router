"""Non-reversible identities for rate limiting (never use raw bearer keys)."""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import ApiKeyRecord


def _hmac_hex(secret: str, raw_key: str) -> str:
    key = (secret or "").encode("utf-8")
    if not key:
        # Deterministic process pepper when no secret configured (still not reversible
        # to the raw key without brute force; prefer configuring a real secret).
        key = b"llm-router-rate-limit-default-pepper"
    return hmac.new(key, raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


def rate_limit_key_id(
    api_key: ApiKeyRecord | str,
    *,
    secret: str = "",
) -> str:
    """
    Stable rate-limiter bucket identity.

    Prefer opaque DB key id when present; otherwise HMAC-SHA256 of the raw key
    with a server-side secret. Never return or store the raw bearer string.
    """
    if isinstance(api_key, str):
        return f"hk:{_hmac_hex(secret, api_key)}"
    if getattr(api_key, "id", None) is not None:
        return f"kid:{int(api_key.id)}"
    return f"hk:{_hmac_hex(secret, api_key.key)}"


def looks_like_raw_api_key(value: str) -> bool:
    """Heuristic used in tests/guards — digest ids never look like sk- keys."""
    v = (value or "").strip()
    return v.startswith("sk-") or (
        len(v) > 8 and ":" not in v[:4] and not v.startswith("hk:") and not v.startswith("kid:")
    )

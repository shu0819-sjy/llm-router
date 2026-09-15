"""Runtime configuration from environment / .env (no secrets committed).

Security boundary knobs (v0.3 hardening):
- ``LLM_ROUTER_ADMIN_TOKEN_MIN_LENGTH`` — production admin-token quality floor.
- ``LLM_ROUTER_MAX_BODY_BYTES`` / ``MAX_MESSAGES`` / ``MAX_TOOLS`` — request
  size and chat-schema bounds enforced at the ASGI boundary.
- ``LLM_ROUTER_MAX_CONCURRENT_CHAT_REQUESTS`` — in-flight chat request cap.
- ``LLM_ROUTER_ALLOW_PRIVATE_PROVIDER_URLS`` — development-only override that
  permits loopback/private provider base URLs (refused in production).
- ``LLM_ROUTER_DIAGNOSTICS_AUTH`` — auth policy for /metrics and
  /health/providers ("auto" | "admin" | "public").
"""

from __future__ import annotations

import ipaddress
import socket
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Provider base-URL SSRF policy
# ---------------------------------------------------------------------------

_SAFE_URL_SCHEMES = frozenset({"http", "https"})
_LOCAL_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})
_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal")


class UnsafeProviderUrlError(ValueError):
    """A provider base URL violates the SSRF-safe target policy."""


def _unsafe_ip_reason(ip: Any) -> str | None:
    """Return a short reason string when an IP targets a forbidden range."""
    mapped = getattr(ip, "ipv4_mapped", None)
    for candidate in (ip, mapped):
        if candidate is None:
            continue
        if candidate.is_loopback:
            return "loopback address"
        if candidate.is_link_local:
            return "link-local address"
        if candidate.is_private:
            return "private address"
        if candidate.is_unspecified:
            return "unspecified address"
        if candidate.is_multicast:
            return "multicast address"
        if candidate.is_reserved:
            return "reserved address"
    return None


def validate_provider_base_url(
    url: str,
    *,
    allow_private: bool = False,
    resolve: bool = True,
) -> str:
    """
    Validate a provider base URL against the SSRF-safe target policy.

    Rejected targets: non-http(s) schemes, missing hosts, embedded credentials,
    fragments, out-of-range ports, ``localhost``-style names, and loopback /
    link-local / private / unspecified / reserved / multicast IP literals.
    With ``resolve=True`` the hostname is also resolved via DNS and every
    resulting address must be public (defense against hostnames that point at
    internal networks; rebinding-to-localhost is a known residual risk).

    Returns the normalized URL (trailing slash stripped).

    ``allow_private=True`` is the explicit development override: it skips the
    host-range checks entirely (scheme/credential checks still apply).
    """
    raw = (url or "").strip()
    if not raw:
        raise UnsafeProviderUrlError("provider base URL is empty")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise UnsafeProviderUrlError(f"unparsable provider base URL ({exc})") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in _SAFE_URL_SCHEMES:
        raise UnsafeProviderUrlError(
            f"scheme '{scheme or '(missing)'}' is not allowed; use http or https"
        )
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UnsafeProviderUrlError("provider base URL has no host")
    if parts.username or parts.password:
        raise UnsafeProviderUrlError("provider base URL must not embed credentials")
    if parts.fragment:
        raise UnsafeProviderUrlError("provider base URL must not contain a fragment")
    if port is not None and not (1 <= port <= 65535):
        raise UnsafeProviderUrlError("provider base URL port is out of range")

    normalized = raw.rstrip("/")
    if allow_private:
        return normalized

    if host in _LOCAL_HOSTNAMES or host.endswith(_LOCAL_HOST_SUFFIXES):
        raise UnsafeProviderUrlError(
            f"host '{host}' is a local hostname; private provider targets are disabled"
        )
    try:
        literal: Any = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        reason = _unsafe_ip_reason(literal)
        if reason:
            raise UnsafeProviderUrlError(
                f"provider base URL points at a {reason} ({host})"
            )
        return normalized

    if not resolve:
        return normalized
    # DNS-aware check for hostnames (used by runtime wiring, e.g. panel PATCH).
    lookup_port = port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, lookup_port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeProviderUrlError(
            f"provider host '{host}' could not be resolved ({exc.__class__.__name__})"
        ) from exc
    for info in infos:
        sockaddr = info[4]
        addr_text = str(sockaddr[0]).split("%", 1)[0]  # drop IPv6 zone index
        try:
            addr: Any = ipaddress.ip_address(addr_text)
        except ValueError:
            continue
        reason = _unsafe_ip_reason(addr)
        if reason:
            raise UnsafeProviderUrlError(
                f"provider host '{host}' resolves to a {reason} ({addr_text})"
            )
    return normalized


def _is_development_env(env: str | None) -> bool:
    return (env or "").strip().lower() in {"development", "dev", "test"}


_DIAGNOSTICS_AUTH_MODES = frozenset({"auto", "admin", "public"})


class Settings(BaseSettings):
    """All knobs for llm-router v0.1."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Allow constructing Settings(env=..., admin_token=...) by field name
        # in addition to the LLM_ROUTER_* aliases (used heavily by tests).
        populate_by_name=True,
    )

    host: str = Field(default="0.0.0.0", alias="LLM_ROUTER_HOST")
    port: int = Field(default=8000, alias="LLM_ROUTER_PORT")
    db_path: str = Field(default="./data/llm_router.db", alias="LLM_ROUTER_DB_PATH")
    admin_token: str = Field(default="", alias="LLM_ROUTER_ADMIN_TOKEN")
    # production | development | test — insecure admin tokens only allowed in development/test
    env: str = Field(default="production", alias="LLM_ROUTER_ENV")
    # Optional HMAC secret for rate-limit bucket identities (falls back to admin token)
    rate_limit_secret: str = Field(default="", alias="LLM_ROUTER_RATE_LIMIT_SECRET")

    default_timeout_ms: int = Field(default=500, alias="LLM_ROUTER_DEFAULT_TIMEOUT_MS")
    failover_budget_ms: int = Field(default=500, alias="LLM_ROUTER_FAILOVER_BUDGET_MS")
    cb_failure_threshold: int = Field(default=3, alias="LLM_ROUTER_CB_FAILURE_THRESHOLD")
    cb_recovery_timeout_s: float = Field(default=30.0, alias="LLM_ROUTER_CB_RECOVERY_TIMEOUT_S")
    cb_half_open_max: int = Field(default=1, alias="LLM_ROUTER_CB_HALF_OPEN_MAX")

    rate_capacity: float = Field(default=60.0, alias="LLM_ROUTER_RATE_CAPACITY")
    rate_refill_per_s: float = Field(default=1.0, alias="LLM_ROUTER_RATE_REFILL_PER_S")
    rate_cost_per_req: float = Field(default=1.0, alias="LLM_ROUTER_RATE_COST_PER_REQ")

    enable_prometheus: bool = Field(default=False, alias="LLM_ROUTER_ENABLE_PROMETHEUS")
    log_level: str = Field(default="INFO", alias="LLM_ROUTER_LOG_LEVEL")
    provider_order: str = Field(
        default="deepseek,openai,anthropic,qwen",
        alias="LLM_ROUTER_PROVIDER_ORDER",
    )
    # name:key[:provider_id],...
    api_keys: str = Field(default="demo:sk-demo-key", alias="LLM_ROUTER_API_KEYS")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_base_url: str = Field(default="https://api.anthropic.com", alias="ANTHROPIC_BASE_URL")
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    qwen_api_key: str = Field(default="", alias="QWEN_API_KEY")
    qwen_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="QWEN_BASE_URL",
    )

    # --- v0.3 production boundary hardening ---------------------------------
    # Production admin-token quality floor (see app.auth for the full policy).
    admin_token_min_length: int = Field(
        default=24, ge=8, le=1024, alias="LLM_ROUTER_ADMIN_TOKEN_MIN_LENGTH"
    )
    # Request boundary limits (enforced by app.api.errors.RequestBoundaryMiddleware).
    max_body_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1024, alias="LLM_ROUTER_MAX_BODY_BYTES"
    )
    max_messages: int = Field(default=512, ge=1, alias="LLM_ROUTER_MAX_MESSAGES")
    max_tools: int = Field(default=64, ge=1, alias="LLM_ROUTER_MAX_TOOLS")
    max_concurrent_chat_requests: int = Field(
        default=64, ge=1, alias="LLM_ROUTER_MAX_CONCURRENT_CHAT_REQUESTS"
    )
    # Development-only override for the provider URL SSRF policy.
    allow_private_provider_urls: bool = Field(
        default=False, alias="LLM_ROUTER_ALLOW_PRIVATE_PROVIDER_URLS"
    )
    # Auth policy for /metrics + /health/providers: auto (admin outside
    # development, open in development) | admin | public.
    diagnostics_auth: str = Field(default="auto", alias="LLM_ROUTER_DIAGNOSTICS_AUTH")

    @field_validator("provider_order", mode="before")
    @classmethod
    def _strip_order(cls, v: Any) -> str:
        return str(v or "deepseek,openai,anthropic,qwen").strip()

    @field_validator("diagnostics_auth", mode="before")
    @classmethod
    def _validate_diagnostics_auth(cls, v: Any) -> str:
        mode = str(v or "auto").strip().lower()
        if mode not in _DIAGNOSTICS_AUTH_MODES:
            raise ValueError(
                "LLM_ROUTER_DIAGNOSTICS_AUTH must be one of: "
                + ", ".join(sorted(_DIAGNOSTICS_AUTH_MODES))
            )
        return mode

    @model_validator(mode="after")
    def _enforce_provider_url_policy(self) -> Settings:
        """
        Offline enforcement of the provider URL SSRF policy at configuration time.

        DNS-resolving checks (a hostname that resolves into a private network)
        run at runtime wiring (see validate_provider_base_url with resolve=True);
        here we reject structurally unsafe URLs without touching the network.
        """
        if self.allow_private_provider_urls and not _is_development_env(self.env):
            raise ValueError(
                "LLM_ROUTER_ALLOW_PRIVATE_PROVIDER_URLS=true is a development-only "
                "override and is refused when LLM_ROUTER_ENV=production"
            )
        for env_name, url in self.provider_base_urls().items():
            if not (url or "").strip():
                continue
            try:
                validate_provider_base_url(
                    url,
                    allow_private=self.allow_private_provider_urls,
                    resolve=False,
                )
            except UnsafeProviderUrlError as exc:
                raise ValueError(
                    f"{env_name} rejected by the provider URL policy: {exc}"
                ) from exc
        return self

    def provider_base_urls(self) -> dict[str, str]:
        """The four upstream base URLs with their configuration names."""
        return {
            "OPENAI_BASE_URL": self.openai_base_url,
            "ANTHROPIC_BASE_URL": self.anthropic_base_url,
            "DEEPSEEK_BASE_URL": self.deepseek_base_url,
            "QWEN_BASE_URL": self.qwen_base_url,
        }

    def provider_order_list(self) -> list[str]:
        return [p.strip() for p in self.provider_order.split(",") if p.strip()]

    def parsed_api_keys(self) -> list[dict[str, str | None]]:
        """Parse LLM_ROUTER_API_KEYS into [{name, key, provider_id}]."""
        out: list[dict[str, str | None]] = []
        raw = (self.api_keys or "").strip()
        if not raw:
            return out
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            bits = part.split(":")
            if len(bits) < 2:
                continue
            name, key = bits[0].strip(), bits[1].strip()
            provider_id = bits[2].strip() if len(bits) >= 3 and bits[2].strip() else None
            if name and key:
                out.append({"name": name, "key": key, "provider_id": provider_id})
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()

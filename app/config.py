"""Runtime configuration from environment / .env (no secrets committed)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All knobs for llm-router v0.1."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    host: str = Field(default="0.0.0.0", alias="LLM_ROUTER_HOST")
    port: int = Field(default=8000, alias="LLM_ROUTER_PORT")
    db_path: str = Field(default="./data/llm_router.db", alias="LLM_ROUTER_DB_PATH")
    admin_token: str = Field(default="", alias="LLM_ROUTER_ADMIN_TOKEN")

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

    @field_validator("provider_order", mode="before")
    @classmethod
    def _strip_order(cls, v: Any) -> str:
        return str(v or "deepseek,openai,anthropic,qwen").strip()

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

"""Opt-in live/network provider smoke tests.

These tests are excluded from the default pytest run via:

    addopts = -m "not integration"

They additionally require an explicit environment flag so accidental
selection (`pytest -m integration`) still cannot hit live APIs:

    LLM_ROUTER_RUN_INTEGRATION=1
"""

from __future__ import annotations

import os

import httpx
import pytest

RUN_INTEGRATION = os.getenv("LLM_ROUTER_RUN_INTEGRATION", "").strip() == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not RUN_INTEGRATION,
        reason="Set LLM_ROUTER_RUN_INTEGRATION=1 to enable live provider tests",
    ),
]


@pytest.mark.asyncio
async def test_public_openai_models_endpoint_reachable() -> None:
    """Cheap connectivity smoke against a public HTTPS API (no auth required).

    This does not call paid chat completions. It only verifies that the
    integration lane can perform outbound HTTPS when explicitly enabled.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get("https://api.openai.com/v1/models")
    # Without a key OpenAI returns 401; that still proves network + TLS work.
    assert response.status_code in {200, 401}


def test_integration_env_flag_is_explicit() -> None:
    """When this module runs, the opt-in flag must be exactly enabled."""
    assert RUN_INTEGRATION is True

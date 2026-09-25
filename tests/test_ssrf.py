"""SSRF-safe provider base URL policy.

Provider base URLs must reject unsafe schemes, embedded credentials,
loopback / link-local / private / reserved targets, and hostnames that
resolve into private networks — unless the explicit development override
(LLM_ROUTER_ALLOW_PRIVATE_PROVIDER_URLS=true, refused in production) is set.
"""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.config as config_module
from app.config import (
    Settings,
    UnsafeProviderUrlError,
    validate_provider_base_url,
)
from app.main import create_app
from app.providers.registry import ProviderRegistry
from tests.conftest import FakeProvider

STRONG_ADMIN = "RouterAdmin-2024-Xk9qz-PLWmn-741"


# ---------------------------------------------------------------------------
# validate_provider_base_url — unit level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url",
        "http://",
        "ftp://example.com/v1",
        "file:///etc/passwd",
        "gopher://example.com",
        "ws://example.com/v1",
        "http://user:secret@example.com/v1",
        "https://user@example.com/v1",
        "http://example.com/#fragment",
        "http://example.com:99999/",
        # loopback / local names
        "http://127.0.0.1",
        "http://127.0.0.1:8080/v1",
        "https://localhost",
        "https://localhost:11434/v1",
        "https://api.localhost/v1",
        "http://my-proxy.local/v1",
        "http://gateway.internal/v1",
        "http://[::1]/v1",
        "http://[::ffff:127.0.0.1]/v1",
        # link-local / cloud metadata
        "http://169.254.169.254/latest/meta-data/",
        "http://[fe80::1]/v1",
        # private ranges
        "http://10.0.0.5/v1",
        "http://172.16.0.1/v1",
        "http://172.31.255.255/v1",
        "http://192.168.1.10/v1",
        "http://[fc00::1]/v1",
        "http://[fd12:3456:789a::1]/v1",
        # unspecified / reserved
        "http://0.0.0.0/v1",
        "http://240.0.0.1/v1",
        # multicast
        "http://224.0.0.1/v1",
    ],
)
def test_unsafe_provider_urls_rejected(url: str) -> None:
    with pytest.raises(UnsafeProviderUrlError):
        validate_provider_base_url(url)


def test_safe_provider_urls_accepted() -> None:
    # Public literal IPs need no DNS.
    assert validate_provider_base_url("https://8.8.8.8/v1") == "https://8.8.8.8/v1"
    # Hostnames pass the offline (resolve=False) checks.
    assert (
        validate_provider_base_url("https://api.openai.com/v1", resolve=False)
        == "https://api.openai.com/v1"
    )
    # Trailing slash is normalized away.
    assert validate_provider_base_url("https://8.8.8.8/") == "https://8.8.8.8"


def test_explicit_development_override_allows_private_targets() -> None:
    assert (
        validate_provider_base_url("http://127.0.0.1:9000/", allow_private=True)
        == "http://127.0.0.1:9000"
    )
    # Scheme/credential checks still apply even with the override.
    with pytest.raises(UnsafeProviderUrlError):
        validate_provider_base_url("ftp://127.0.0.1:9000", allow_private=True)
    with pytest.raises(UnsafeProviderUrlError):
        validate_provider_base_url("http://user:pass@127.0.0.1:9000", allow_private=True)


def test_dns_resolution_to_private_range_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_private(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]

    monkeypatch.setattr(config_module.socket, "getaddrinfo", fake_private)
    with pytest.raises(UnsafeProviderUrlError, match="private"):
        validate_provider_base_url("https://internal.example/v1")

    def fake_public(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(config_module.socket, "getaddrinfo", fake_public)
    assert (
        validate_provider_base_url("https://internal.example/v1") == "https://internal.example/v1"
    )


def test_unresolvable_hostname_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fail(host, port, *args, **kwargs):
        raise OSError("no dns")

    monkeypatch.setattr(config_module.socket, "getaddrinfo", fake_fail)
    with pytest.raises(UnsafeProviderUrlError, match="could not be resolved"):
        validate_provider_base_url("https://nope.example")


def test_ipv6_zone_index_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_link_local(host, port, *args, **kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%eth0", port, 0, 0))]

    monkeypatch.setattr(config_module.socket, "getaddrinfo", fake_link_local)
    with pytest.raises(UnsafeProviderUrlError, match="link-local"):
        validate_provider_base_url("https://host6.example/v1")


# ---------------------------------------------------------------------------
# Settings-level enforcement (offline, no DNS)
# ---------------------------------------------------------------------------


def test_production_settings_reject_metadata_base_url(tmp_path) -> None:
    with pytest.raises(ValidationError, match="169.254.169.254"):
        Settings(
            env="production",
            admin_token=STRONG_ADMIN,
            db_path=str(tmp_path / "s.db"),
            openai_base_url="http://169.254.169.254/latest/meta-data/",
        )


def test_production_settings_reject_loopback_and_private_base_urls(tmp_path) -> None:
    for url in ("http://127.0.0.1:9000", "http://10.0.0.5/v1", "https://localhost/v1"):
        with pytest.raises(ValidationError):
            Settings(
                env="production",
                admin_token=STRONG_ADMIN,
                db_path=str(tmp_path / "s.db"),
                deepseek_base_url=url,
            )


def test_production_settings_reject_unsafe_scheme(tmp_path) -> None:
    with pytest.raises(ValidationError, match="OPENAI_BASE_URL"):
        Settings(
            env="production",
            admin_token=STRONG_ADMIN,
            db_path=str(tmp_path / "s.db"),
            openai_base_url="ftp://files.example.com",
        )


def test_production_settings_accept_public_urls(tmp_path) -> None:
    settings = Settings(
        env="production",
        admin_token=STRONG_ADMIN,
        db_path=str(tmp_path / "s.db"),
        openai_base_url="https://8.8.8.8/v1",
        anthropic_base_url="https://1.1.1.1",
        deepseek_base_url="https://8.8.4.4",
        qwen_base_url="https://9.9.9.9/compatible-mode/v1",
    )
    assert settings.openai_base_url == "https://8.8.8.8/v1"


def test_private_url_override_refused_in_production(tmp_path) -> None:
    with pytest.raises(ValidationError, match="development-only"):
        Settings(
            env="production",
            admin_token=STRONG_ADMIN,
            db_path=str(tmp_path / "s.db"),
            allow_private_provider_urls=True,
        )


def test_private_url_override_allows_loopback_in_development(tmp_path) -> None:
    settings = Settings(
        env="development",
        db_path=str(tmp_path / "s.db"),
        allow_private_provider_urls=True,
        deepseek_base_url="http://127.0.0.1:9000/v1",
    )
    assert settings.deepseek_base_url == "http://127.0.0.1:9000/v1"


def test_development_without_override_still_rejects_loopback(tmp_path) -> None:
    with pytest.raises(ValidationError):
        Settings(
            env="development",
            db_path=str(tmp_path / "s.db"),
            deepseek_base_url="http://127.0.0.1:9000/v1",
        )


# ---------------------------------------------------------------------------
# Panel runtime enforcement (PATCH /panel/api/providers/{id})
# ---------------------------------------------------------------------------


def _panel_app(test_settings: Settings, *, allow_private: bool = False):
    test_settings.admin_token = STRONG_ADMIN
    test_settings.allow_private_provider_urls = allow_private
    reg = ProviderRegistry([FakeProvider("deepseek", ["deepseek-"])])
    for p in reg.all():
        p.base_url = "https://upstream.example.test/v1"  # type: ignore[attr-defined]
    return create_app(test_settings, registry=reg)


def test_panel_patch_rejects_unsafe_base_url(test_settings: Settings) -> None:
    app = _panel_app(test_settings, allow_private=False)
    headers = {"Authorization": f"Bearer {STRONG_ADMIN}"}
    with TestClient(app) as client:
        r = client.patch(
            "/panel/api/providers/deepseek",
            headers=headers,
            json={"base_url": "http://127.0.0.1:9999"},
        )
        assert r.status_code == 400
        err = r.json()["error"]
        assert err["code"] == "unsafe_provider_url"
        # Original URL untouched.
        assert (
            app.state.registry.get("deepseek").base_url  # type: ignore[attr-defined]
            == "https://upstream.example.test/v1"
        )

        r2 = client.patch(
            "/panel/api/providers/deepseek",
            headers=headers,
            json={"base_url": "ftp://files.example.com"},
        )
        assert r2.status_code == 400
        assert r2.json()["error"]["code"] == "unsafe_provider_url"


def test_panel_patch_accepts_loopback_with_development_override(
    test_settings: Settings,
) -> None:
    app = _panel_app(test_settings, allow_private=True)
    headers = {"Authorization": f"Bearer {STRONG_ADMIN}"}
    with TestClient(app) as client:
        r = client.patch(
            "/panel/api/providers/deepseek",
            headers=headers,
            json={"base_url": "http://127.0.0.1:9999/"},
        )
        assert r.status_code == 200
        assert r.json()["base_url"] == "http://127.0.0.1:9999"
        assert (
            app.state.registry.get("deepseek").base_url  # type: ignore[attr-defined]
            == "http://127.0.0.1:9999"
        )


def test_panel_patch_rejects_dns_rebinding_hostname(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_private(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.44", port))]

    monkeypatch.setattr(config_module.socket, "getaddrinfo", fake_private)
    app = _panel_app(test_settings, allow_private=False)
    headers = {"Authorization": f"Bearer {STRONG_ADMIN}"}
    with TestClient(app) as client:
        r = client.patch(
            "/panel/api/providers/deepseek",
            headers=headers,
            json={"base_url": "https://rebind.example.test/v1"},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "unsafe_provider_url"

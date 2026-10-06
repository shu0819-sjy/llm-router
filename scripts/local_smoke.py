"""Run a dependency-light configuration and secret-redaction smoke check."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.errors import sanitize_client_error_message  # noqa: E402
from app.auth import admin_token_quality_issues  # noqa: E402
from app.config import Settings  # noqa: E402


def main() -> None:
    """Validate safe development defaults without network or provider calls."""
    settings = Settings(env="development", admin_token="change-me-admin-token")
    if settings.env != "development":
        raise RuntimeError("development configuration did not load")

    leaked = "upstream rejected Bearer sk-local-smoke-secret"
    sanitized = sanitize_client_error_message(leaked)
    if "sk-local-smoke-secret" in sanitized or "[redacted]" not in sanitized:
        raise RuntimeError("client-facing error redaction failed")
    if not admin_token_quality_issues(settings.admin_token):
        raise RuntimeError("placeholder admin token was unexpectedly accepted")

    print("local smoke passed: development config and secret redaction")


if __name__ == "__main__":
    main()

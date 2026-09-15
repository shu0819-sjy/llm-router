"""Docker restart + SQLite migration smoke test for llm-router.

Opt-in quality tooling. Boots the built image against a **clean temporary
Docker volume** and checks two release-critical behaviors:

1. Migration — a database created with the v0.2.0 schema (plus representative
   rows) boots on the current image; additive migrations apply and the
   pre-existing rows survive.
2. Restart persistence — an API key created via the admin panel still
   authenticates after ``docker restart``, with usage history intact.

This is a smoke test of packaged defaults, **not** a production validation
claim. It requires a working Docker daemon.

Usage (from the repository root):

    python scripts/restart_migration_smoke.py                  # build llm-router:smoke if missing
    python scripts/restart_migration_smoke.py --image llm-router:ci
    python scripts/restart_migration_smoke.py --build --port 18000 --keep

Exit code 0 = all checks passed; non-zero = first failed check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# The container runs as root for this smoke so the `docker cp`-ed seed database
# stays writable. The image's default USER is unchanged for real deployments.
CONTAINER_USER = "root"

SEED_KEY_RAW = "sk-smoke-seed-key"
ENV_KEY_RAW = "sk-smoke-env-key"
SEED_MODEL = "smoke-legacy-model"
SEED_USAGE_REQUEST_ID = "smoke-seed-usage-req"
DB_PATH_IN_CONTAINER = "/app/data/llm_router.db"

# Schema as shipped in v0.2.0 (before the v0.3 additive migrations).
V02_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    provider_id TEXT,
    model_allowlist TEXT,
    rate_capacity REAL,
    rate_refill_per_s REAL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    base_url TEXT NOT NULL,
    api_key_enc TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    weight INTEGER NOT NULL DEFAULT 100,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_id INTEGER,
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    request_id TEXT,
    accounting_status TEXT NOT NULL DEFAULT 'actual',
    price_version TEXT,
    input_price_per_1m_usd REAL,
    output_price_per_1m_usd REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
);

CREATE TABLE IF NOT EXISTS model_prices (
    model TEXT PRIMARY KEY,
    input_per_1m_usd REAL NOT NULL DEFAULT 0,
    output_per_1m_usd REAL NOT NULL DEFAULT 0,
    version TEXT NOT NULL DEFAULT 'v1',
    effective_from TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'
);

CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_key ON usage_events(api_key_id);
CREATE INDEX IF NOT EXISTS idx_keys_hash ON api_keys(key_hash);
"""

# Columns that must survive the v0.2.0 -> current migration unchanged.
V02_API_KEY_COLUMNS = {
    "id",
    "key_hash",
    "name",
    "provider_id",
    "model_allowlist",
    "rate_capacity",
    "rate_refill_per_s",
    "is_active",
    "created_at",
}
V02_USAGE_COLUMNS = {
    "id",
    "api_key_id",
    "provider_id",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cost_usd",
    "latency_ms",
    "status",
    "request_id",
    "accounting_status",
    "price_version",
    "input_price_per_1m_usd",
    "output_price_per_1m_usd",
    "created_at",
}
# Known additive column introduced after v0.2.0 (api_keys.source).
KNOWN_ADDED_API_KEY_COLUMNS = {"source"}

# No proxy for localhost HTTP probes.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(step: str, message: str) -> None:
    print(f"[{step}] {message}", flush=True)


def fail(step: str, message: str) -> None:
    print(f"[{step}] FAIL: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        print(
            f"docker {' '.join(args)} failed (exit {proc.returncode}):\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}",
            file=sys.stderr,
        )
        raise SystemExit(proc.returncode or 1)
    return proc


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout_s: float = 15.0,
) -> tuple[int, Any]:
    data = None
    hdrs = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with _OPENER.open(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, (json.loads(body) if body else None)
        except json.JSONDecodeError:
            return exc.code, body


def wait_healthy(port: int, timeout_s: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/health/ready"
    last_error = "unknown"
    while time.monotonic() < deadline:
        try:
            status, _ = http_json("GET", url, timeout_s=5.0)
            if status == 200:
                return
            last_error = f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001 — retry loop
            last_error = str(exc)
        time.sleep(2.0)
    fail("health", f"container did not become ready within {timeout_s:.0f}s ({last_error})")


def table_columns(container: str, table: str) -> set[str]:
    code = (
        "import sqlite3;"
        f"con = sqlite3.connect('{DB_PATH_IN_CONTAINER}');"
        f"print(','.join(r[1] for r in con.execute('PRAGMA table_info({table})')))"
    )
    proc = docker("exec", container, "python", "-c", code)
    return {c.strip() for c in proc.stdout.strip().split(",") if c.strip()}


def build_seed_db(path: Path) -> None:
    """Create a v0.2.0-schema database with representative rows."""
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    key_hash = hashlib.sha256(SEED_KEY_RAW.encode("utf-8")).hexdigest()
    con = sqlite3.connect(path)
    try:
        con.executescript(V02_SCHEMA_DDL)
        con.execute(
            """
            INSERT INTO api_keys(
                key_hash, name, provider_id, model_allowlist,
                rate_capacity, rate_refill_per_s, is_active, created_at
            ) VALUES (?, 'smoke-seed', NULL, NULL, NULL, NULL, 1, ?)
            """,
            (key_hash, now),
        )
        con.execute(
            """
            INSERT INTO model_prices(
                model, input_per_1m_usd, output_per_1m_usd, version, effective_from
            ) VALUES (?, 1.0, 2.0, 'v1', '2026-01-01T00:00:00+00:00')
            """,
            (SEED_MODEL,),
        )
        con.execute(
            """
            INSERT INTO usage_events(
                api_key_id, provider_id, model,
                prompt_tokens, completion_tokens, total_tokens,
                cost_usd, latency_ms, status, request_id,
                accounting_status, created_at
            ) VALUES (1, 'deepseek', ?, 10, 20, 30, 0.00006, 123, 'ok', ?, 'actual', ?)
            """,
            (SEED_MODEL, SEED_USAGE_REQUEST_ID, now),
        )
        con.commit()
    finally:
        con.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", default="llm-router:smoke", help="image to test")
    parser.add_argument("--build", action="store_true", help="force a rebuild of the image")
    parser.add_argument("--port", type=int, default=18000, help="host port for the container")
    parser.add_argument("--keep", action="store_true", help="keep container and volume on exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    suffix = secrets.token_hex(4)
    container = f"llm-router-smoke-{suffix}"
    volume = f"llm-router-smoke-vol-{suffix}"
    admin_token = secrets.token_urlsafe(32)
    base_url = f"http://127.0.0.1:{args.port}"
    seed_db = Path(f".smoke-seed-{suffix}.db")

    if args.build or docker("image", "inspect", args.image, check=False).returncode != 0:
        log("build", f"building {args.image} from {REPO_ROOT}")
        build = subprocess.run(
            ["docker", "build", "-t", args.image, str(REPO_ROOT)],
            check=False,
        )
        if build.returncode != 0:
            fail("build", "docker build failed")

    try:
        # --- Phase A: boot on a v0.2.0 database (migration) ---
        build_seed_db(seed_db)
        docker("volume", "create", volume)
        docker(
            "create",
            "--name",
            container,
            "--user",
            CONTAINER_USER,
            "-p",
            f"{args.port}:8000",
            "-e",
            "LLM_ROUTER_ENV=development",
            "-e",
            f"LLM_ROUTER_ADMIN_TOKEN={admin_token}",
            "-e",
            f"LLM_ROUTER_API_KEYS=smoke:{ENV_KEY_RAW}",
            "-e",
            "LLM_ROUTER_DB_PATH=/app/data/llm_router.db",
            "-v",
            f"{volume}:/app/data",
            args.image,
        )
        docker("cp", str(seed_db), f"{container}:{DB_PATH_IN_CONTAINER}")
        docker("start", container)
        log("start", f"container {container} starting on port {args.port}")
        wait_healthy(args.port)

        key_columns = table_columns(container, "api_keys")
        usage_columns = table_columns(container, "usage_events")
        missing_keys = V02_API_KEY_COLUMNS - key_columns
        missing_usage = V02_USAGE_COLUMNS - usage_columns
        if missing_keys or missing_usage:
            fail(
                "migration",
                f"legacy columns dropped: api_keys={sorted(missing_keys)} "
                f"usage_events={sorted(missing_usage)}",
            )
        added = key_columns - V02_API_KEY_COLUMNS
        if not (added & KNOWN_ADDED_API_KEY_COLUMNS):
            fail("migration", f"expected additive column(s) {KNOWN_ADDED_API_KEY_COLUMNS}, got {added}")
        log("migration", f"schema OK (api_keys added columns: {sorted(added)})")

        admin_auth = {"Authorization": f"Bearer {admin_token}"}
        status, models = http_json(
            "GET", f"{base_url}/v1/models", headers={"Authorization": f"Bearer {SEED_KEY_RAW}"}
        )
        if status != 200:
            fail("migration", f"seeded v0.2.0 key no longer authenticates (HTTP {status})")
        model_ids = {m.get("id") for m in (models or {}).get("data", [])}
        if SEED_MODEL not in model_ids:
            fail("migration", f"seeded model price row missing from /v1/models: {sorted(model_ids)}")
        log("migration", "seeded key + model price survived")

        status, usage = http_json("GET", f"{base_url}/panel/api/usage", headers=admin_auth)
        if status != 200 or int((usage or {}).get("summary", {}).get("requests", 0)) < 1:
            fail("migration", f"seeded usage row not readable via panel (HTTP {status})")
        log("migration", "seeded usage row survived")

        status, _ = http_json(
            "GET", f"{base_url}/v1/models", headers={"Authorization": f"Bearer {ENV_KEY_RAW}"}
        )
        if status != 200:
            fail("migration", f"env-configured key does not authenticate (HTTP {status})")

        # --- Phase B: restart persistence ---
        status, created = http_json(
            "POST",
            f"{base_url}/panel/api/keys",
            headers=admin_auth,
            json_body={"name": "smoke-restart"},
        )
        if status != 200 or not (created or {}).get("key"):
            fail("restart", f"panel key creation failed (HTTP {status})")
        panel_key = str(created["key"])
        log("restart", "panel key created")

        status, _ = http_json(
            "GET", f"{base_url}/v1/models", headers={"Authorization": f"Bearer {panel_key}"}
        )
        if status != 200:
            fail("restart", f"new panel key does not authenticate before restart (HTTP {status})")

        docker("restart", container)
        wait_healthy(args.port)
        log("restart", "container restarted")

        status, _ = http_json(
            "GET", f"{base_url}/v1/models", headers={"Authorization": f"Bearer {panel_key}"}
        )
        if status != 200:
            fail("restart", f"panel key lost after restart (HTTP {status})")
        status, _ = http_json(
            "GET", f"{base_url}/v1/models", headers={"Authorization": f"Bearer {SEED_KEY_RAW}"}
        )
        if status != 200:
            fail("restart", f"seeded key lost after restart (HTTP {status})")
        status, usage = http_json("GET", f"{base_url}/panel/api/usage", headers=admin_auth)
        if status != 200 or int((usage or {}).get("summary", {}).get("requests", 0)) < 1:
            fail("restart", f"usage history lost after restart (HTTP {status})")
        status, _ = http_json(
            "GET", f"{base_url}/v1/models", headers={"Authorization": "Bearer sk-smoke-wrong-key"}
        )
        if status != 401:
            fail("restart", f"invalid key must be rejected with 401 (got HTTP {status})")
        log("restart", "panel key, seeded key, usage history all survived restart")

        log("result", "SMOKE PASSED: migration + restart persistence OK")
        log(
            "note",
            "this is a packaged-defaults smoke test, not a production validation claim",
        )
    finally:
        if args.keep:
            log(
                "cleanup",
                f"--keep set: container={container} volume={volume} "
                f"(remove with: docker rm -f {container} && docker volume rm {volume})",
            )
        else:
            docker("rm", "-f", container, check=False)
            docker("volume", "rm", volume, check=False)
        if seed_db.exists():
            seed_db.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())

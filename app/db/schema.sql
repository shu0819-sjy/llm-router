-- llm-router v0.1 SQLite schema

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
    created_at TEXT NOT NULL,
    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
);

CREATE TABLE IF NOT EXISTS model_prices (
    model TEXT PRIMARY KEY,
    input_per_1m_usd REAL NOT NULL DEFAULT 0,
    output_per_1m_usd REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_key ON usage_events(api_key_id);
CREATE INDEX IF NOT EXISTS idx_keys_hash ON api_keys(key_hash);

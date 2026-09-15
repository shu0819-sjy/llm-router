-- llm-router v0.2 SQLite schema

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    provider_id TEXT,
    model_allowlist TEXT,
    rate_capacity REAL,
    rate_refill_per_s REAL,
    is_active INTEGER NOT NULL DEFAULT 1,
    -- 'env' = managed via LLM_ROUTER_API_KEYS; 'panel' = created in admin panel
    source TEXT NOT NULL DEFAULT 'panel',
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
    -- Time-to-first-byte for streaming requests (0 when not applicable / unknown)
    ttfb_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    request_id TEXT,
    accounting_status TEXT NOT NULL DEFAULT 'actual',
    price_version TEXT,
    input_price_per_1m_usd REAL,
    output_price_per_1m_usd REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
);

-- Current price per model (versioned). Historical costs rely on snapshots on usage_events.
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

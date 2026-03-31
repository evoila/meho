-- meho.db initial schema
-- Stores connector metadata, operations, and sessions
-- Credentials are NOT in SQLite -- they are Fernet-encrypted files

CREATE TABLE IF NOT EXISTS connectors (
    id TEXT PRIMARY KEY,                  -- UUID as text (SQLite has no UUID type)
    name TEXT NOT NULL UNIQUE,            -- Human-readable name (e.g., "k8s-prod")
    connector_type TEXT NOT NULL,         -- "rest", "kubernetes", "vmware", etc.
    description TEXT,
    base_url TEXT,                        -- Connection target URL/host
    config_path TEXT NOT NULL,            -- Path to YAML config file
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_connectors_type ON connectors(connector_type);
CREATE INDEX IF NOT EXISTS idx_connectors_active ON connectors(is_active);

CREATE TABLE IF NOT EXISTS schema_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_info (key, value) VALUES ('created_at', datetime('now'));

PRAGMA user_version = 1;

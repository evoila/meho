-- Operations table: universal operation model for all connector types
-- FTS5 external content table with porter+unicode61 tokenizer for BM25 search
-- Sync triggers keep FTS5 in sync with operations table

CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connector_name TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    trust_tier TEXT NOT NULL DEFAULT 'READ',
    http_method TEXT,
    url_template TEXT,
    input_schema TEXT DEFAULT '{}',
    output_schema TEXT DEFAULT '{}',
    tags TEXT DEFAULT '',
    example_params TEXT DEFAULT '{}',
    related_operations TEXT DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(connector_name, operation_id)
);

CREATE INDEX IF NOT EXISTS idx_ops_connector ON operations(connector_name);
CREATE INDEX IF NOT EXISTS idx_ops_trust ON operations(trust_tier);

-- FTS5 external content table synced via triggers
CREATE VIRTUAL TABLE IF NOT EXISTS operations_fts USING fts5(
    operation_id,
    display_name,
    description,
    tags,
    content='operations',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Trigger: after INSERT, add row to FTS5
CREATE TRIGGER IF NOT EXISTS operations_ai AFTER INSERT ON operations BEGIN
    INSERT INTO operations_fts(rowid, operation_id, display_name, description, tags)
    VALUES (new.id, new.operation_id, new.display_name, new.description, new.tags);
END;

-- Trigger: after DELETE, remove row from FTS5
CREATE TRIGGER IF NOT EXISTS operations_ad AFTER DELETE ON operations BEGIN
    INSERT INTO operations_fts(operations_fts, rowid, operation_id, display_name, description, tags)
    VALUES ('delete', old.id, old.operation_id, old.display_name, old.description, old.tags);
END;

-- Trigger: after UPDATE, update row in FTS5 (delete old + insert new)
CREATE TRIGGER IF NOT EXISTS operations_au AFTER UPDATE ON operations BEGIN
    INSERT INTO operations_fts(operations_fts, rowid, operation_id, display_name, description, tags)
    VALUES ('delete', old.id, old.operation_id, old.display_name, old.description, old.tags);
    INSERT INTO operations_fts(rowid, operation_id, display_name, description, tags)
    VALUES (new.id, new.operation_id, new.display_name, new.description, new.tags);
END;

PRAGMA user_version = 2;

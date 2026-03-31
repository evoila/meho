-- Knowledge sources: tracks ingested files
-- Knowledge chunks: individual searchable pieces with FTS5 for BM25 search
-- Dual-write pattern: SQLite is source of truth, ChromaDB is search cache

CREATE TABLE IF NOT EXISTS knowledge_sources (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    connector_name TEXT,           -- NULL = global knowledge
    file_hash TEXT NOT NULL,       -- SHA-256 of original file for dedup
    chunk_count INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ks_connector ON knowledge_sources(connector_name);
-- Use COALESCE to handle NULL connector_name for unique constraint
CREATE UNIQUE INDEX IF NOT EXISTS idx_ks_file_connector
    ON knowledge_sources(filename, COALESCE(connector_name, '__global__'));

-- Knowledge chunks: individual searchable pieces
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    heading TEXT NOT NULL DEFAULT '',
    token_estimate INTEGER NOT NULL DEFAULT 0,
    connector_name TEXT,           -- Denormalized for search efficiency
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_kc_source ON knowledge_chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_kc_connector ON knowledge_chunks(connector_name);

-- FTS5 for BM25 search over knowledge chunks
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(
    content,
    heading,
    content='knowledge_chunks',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

-- Sync triggers (same pattern as operations_fts)
CREATE TRIGGER IF NOT EXISTS kc_ai AFTER INSERT ON knowledge_chunks BEGIN
    INSERT INTO knowledge_chunks_fts(rowid, content, heading)
    VALUES (new.rowid, new.content, new.heading);
END;

CREATE TRIGGER IF NOT EXISTS kc_ad AFTER DELETE ON knowledge_chunks BEGIN
    INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts, rowid, content, heading)
    VALUES ('delete', old.rowid, old.content, old.heading);
END;

CREATE TRIGGER IF NOT EXISTS kc_au AFTER UPDATE ON knowledge_chunks BEGIN
    INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts, rowid, content, heading)
    VALUES ('delete', old.rowid, old.content, old.heading);
    INSERT INTO knowledge_chunks_fts(rowid, content, heading)
    VALUES (new.rowid, new.content, new.heading);
END;

PRAGMA user_version = 3;

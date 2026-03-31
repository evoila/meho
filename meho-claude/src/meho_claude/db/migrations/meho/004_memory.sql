-- Memories: connector-scoped or global knowledge snippets
-- Manual-only creation via meho memory store
-- FTS5 for BM25 search, ChromaDB for semantic search

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    connector_name TEXT,           -- NULL = global memory
    tags TEXT NOT NULL DEFAULT '',  -- Comma-separated tags
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_mem_connector ON memories(connector_name);

-- FTS5 for BM25 search over memories
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    tags,
    content='memories',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, tags)
    VALUES (new.rowid, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
    VALUES ('delete', old.rowid, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
    VALUES ('delete', old.rowid, old.content, old.tags);
    INSERT INTO memories_fts(rowid, content, tags)
    VALUES (new.rowid, new.content, new.tags);
END;

PRAGMA user_version = 4;

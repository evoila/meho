-- topology.db migration 002: FTS5 virtual table for topology entity search
-- Pattern: mirrors meho.db 002_operations.sql exactly
-- Adds embedding_hash column for hash-based change detection

-- Add embedding_hash column for change detection
ALTER TABLE topology_entities ADD COLUMN embedding_hash TEXT DEFAULT '';

-- FTS5 external content table synced via triggers
-- Columns: name, entity_type, description (searchable fields)
-- Uses implicit rowid from topology_entities (SQLite auto-provides for non-WITHOUT ROWID tables)
CREATE VIRTUAL TABLE IF NOT EXISTS topology_entities_fts USING fts5(
    name,
    entity_type,
    description,
    content='topology_entities',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

-- Trigger: after INSERT, add row to FTS5
CREATE TRIGGER IF NOT EXISTS topo_entities_ai AFTER INSERT ON topology_entities BEGIN
    INSERT INTO topology_entities_fts(rowid, name, entity_type, description)
    VALUES (new.rowid, new.name, new.entity_type, new.description);
END;

-- Trigger: after DELETE, remove row from FTS5
CREATE TRIGGER IF NOT EXISTS topo_entities_ad AFTER DELETE ON topology_entities BEGIN
    INSERT INTO topology_entities_fts(topology_entities_fts, rowid, name, entity_type, description)
    VALUES ('delete', old.rowid, old.name, old.entity_type, old.description);
END;

-- Trigger: after UPDATE, update row in FTS5 (delete old + insert new)
CREATE TRIGGER IF NOT EXISTS topo_entities_au AFTER UPDATE ON topology_entities BEGIN
    INSERT INTO topology_entities_fts(topology_entities_fts, rowid, name, entity_type, description)
    VALUES ('delete', old.rowid, old.name, old.entity_type, old.description);
    INSERT INTO topology_entities_fts(rowid, name, entity_type, description)
    VALUES (new.rowid, new.name, new.entity_type, new.description);
END;

PRAGMA user_version = 2;

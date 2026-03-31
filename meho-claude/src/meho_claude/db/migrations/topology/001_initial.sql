-- topology.db initial schema
-- Stores discovered infrastructure entities and their relationships

CREATE TABLE IF NOT EXISTS topology_entities (
    id TEXT PRIMARY KEY,                  -- UUID as text
    name TEXT NOT NULL,
    connector_id TEXT,                    -- References connectors.id in meho.db
    connector_name TEXT,                  -- Cached for display
    entity_type TEXT NOT NULL,            -- "Pod", "VM", "Namespace", "Host"
    connector_type TEXT NOT NULL,         -- "kubernetes", "vmware", "gcp"
    scope_json TEXT DEFAULT '{}',         -- JSON: {"namespace": "prod"}
    canonical_id TEXT NOT NULL,           -- Scoped identity: "prod/nginx"
    description TEXT NOT NULL DEFAULT '',
    raw_attributes_json TEXT DEFAULT '{}',
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_verified_at TEXT,
    stale_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_topo_entity_type ON topology_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_topo_entity_connector ON topology_entities(connector_id);
CREATE INDEX IF NOT EXISTS idx_topo_entity_connector_type ON topology_entities(connector_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_topo_entity_identity
    ON topology_entities(connector_id, entity_type, canonical_id);

CREATE TABLE IF NOT EXISTS topology_relationships (
    id TEXT PRIMARY KEY,
    from_entity_id TEXT NOT NULL REFERENCES topology_entities(id) ON DELETE CASCADE,
    to_entity_id TEXT NOT NULL REFERENCES topology_entities(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,      -- "runs_on", "routes_to", "uses_storage", "member_of"
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_verified_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_topo_rel_from ON topology_relationships(from_entity_id);
CREATE INDEX IF NOT EXISTS idx_topo_rel_to ON topology_relationships(to_entity_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_topo_rel_unique
    ON topology_relationships(from_entity_id, to_entity_id, relationship_type);

CREATE TABLE IF NOT EXISTS topology_correlations (
    id TEXT PRIMARY KEY,
    entity_a_id TEXT NOT NULL REFERENCES topology_entities(id) ON DELETE CASCADE,
    entity_b_id TEXT NOT NULL REFERENCES topology_entities(id) ON DELETE CASCADE,
    match_type TEXT NOT NULL,             -- "ip_match", "hostname_match", "provider_id"
    confidence REAL NOT NULL DEFAULT 0.0, -- 0.0 to 1.0
    match_details TEXT,                   -- Human-readable match evidence
    status TEXT NOT NULL DEFAULT 'pending', -- "pending", "confirmed", "rejected"
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    resolved_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_topo_corr_a ON topology_correlations(entity_a_id);
CREATE INDEX IF NOT EXISTS idx_topo_corr_b ON topology_correlations(entity_b_id);
CREATE INDEX IF NOT EXISTS idx_topo_corr_status ON topology_correlations(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_topo_corr_unique
    ON topology_correlations(entity_a_id, entity_b_id);

PRAGMA user_version = 1;

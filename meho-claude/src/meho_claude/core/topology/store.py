"""TopologyStore: CRUD and query interface for topology.db.

Handles entity/relationship upserts, graph queries, and orchestrates
correlation via CorrelationEngine on every entity insert.
"""

import json
import uuid
from pathlib import Path

import structlog

from meho_claude.core.database import get_connection
from meho_claude.core.topology.models import (
    ExtractionResult,
    TopologyEntity,
    TopologyRelationship,
    compute_embedding_hash,
)

logger = structlog.get_logger()


class TopologyStore:
    """CRUD and query interface for topology.db."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.conn = get_connection(state_dir / "topology.db")

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()

    def upsert_entity(self, entity: TopologyEntity) -> bool:
        """Upsert an entity. Returns True if entity was inserted or embedding-relevant fields changed.

        Matching is done by (connector_id, entity_type, canonical_id).
        The embedding hash is computed from (entity_type, name, description) and compared
        to detect whether re-embedding is needed.
        """
        new_hash = compute_embedding_hash(entity.entity_type, entity.name, entity.description)

        # Check existing by unique identity
        existing = self.conn.execute(
            """SELECT id, embedding_hash FROM topology_entities
               WHERE connector_id = ? AND entity_type = ? AND canonical_id = ?""",
            (entity.connector_id, entity.entity_type, entity.canonical_id),
        ).fetchone()

        if existing:
            # Update existing entity
            self.conn.execute(
                """UPDATE topology_entities
                   SET name = ?, description = ?, raw_attributes_json = ?,
                       scope_json = ?, last_verified_at = datetime('now'),
                       embedding_hash = ?
                   WHERE id = ?""",
                (
                    entity.name,
                    entity.description,
                    json.dumps(entity.raw_attributes),
                    json.dumps(entity.scope),
                    new_hash,
                    existing["id"],
                ),
            )
            self.conn.commit()
            entity.id = existing["id"]
            return existing["embedding_hash"] != new_hash
        else:
            # Insert new entity
            entity.id = entity.id or str(uuid.uuid4())
            self.conn.execute(
                """INSERT INTO topology_entities
                   (id, name, connector_id, connector_name, entity_type, connector_type,
                    scope_json, canonical_id, description, raw_attributes_json, embedding_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entity.id,
                    entity.name,
                    entity.connector_id,
                    entity.connector_name,
                    entity.entity_type,
                    entity.connector_type,
                    json.dumps(entity.scope),
                    entity.canonical_id,
                    entity.description,
                    json.dumps(entity.raw_attributes),
                    new_hash,
                ),
            )
            self.conn.commit()
            return True  # New entity always needs embedding

    def upsert_relationship(self, rel: TopologyRelationship) -> None:
        """Upsert a relationship. Idempotent: updates last_verified_at if exists.

        Uses INSERT OR REPLACE on the unique index (from_entity_id, to_entity_id, relationship_type).
        """
        # Check if relationship already exists
        existing = self.conn.execute(
            """SELECT id FROM topology_relationships
               WHERE from_entity_id = ? AND to_entity_id = ? AND relationship_type = ?""",
            (rel.from_entity_id, rel.to_entity_id, rel.relationship_type),
        ).fetchone()

        if existing:
            # Update last_verified_at
            self.conn.execute(
                """UPDATE topology_relationships
                   SET last_verified_at = datetime('now')
                   WHERE id = ?""",
                (existing["id"],),
            )
        else:
            # Insert new relationship
            rel_id = rel.id or str(uuid.uuid4())
            self.conn.execute(
                """INSERT INTO topology_relationships
                   (id, from_entity_id, to_entity_id, relationship_type)
                   VALUES (?, ?, ?, ?)""",
                (rel_id, rel.from_entity_id, rel.to_entity_id, rel.relationship_type),
            )
        self.conn.commit()

    def ingest(self, result: ExtractionResult) -> list[TopologyEntity]:
        """Ingest an extraction result: upsert entities, relationships, and run correlations.

        Returns list of entities that need embedding (new or changed embedding-relevant fields).
        """
        from meho_claude.core.topology.correlator import CorrelationEngine

        correlator = CorrelationEngine(self.conn)
        entities_needing_embedding: list[TopologyEntity] = []

        for entity in result.entities:
            needs_embed = self.upsert_entity(entity)
            if needs_embed:
                entities_needing_embedding.append(entity)
            # Eager correlation after every entity upsert
            correlator.correlate_entity(entity)

        for rel in result.relationships:
            self.upsert_relationship(rel)

        return entities_needing_embedding

    def get_entity_by_id(self, entity_id: str) -> TopologyEntity | None:
        """Get an entity by its UUID id. Returns None if not found."""
        row = self.conn.execute(
            "SELECT * FROM topology_entities WHERE id = ?",
            (entity_id,),
        ).fetchone()

        if row is None:
            return None

        return _row_to_entity(row)

    def get_entity_by_canonical(
        self, connector_id: str, entity_type: str, canonical_id: str
    ) -> TopologyEntity | None:
        """Get an entity by its unique identity (connector_id, entity_type, canonical_id)."""
        row = self.conn.execute(
            """SELECT * FROM topology_entities
               WHERE connector_id = ? AND entity_type = ? AND canonical_id = ?""",
            (connector_id, entity_type, canonical_id),
        ).fetchone()

        if row is None:
            return None

        return _row_to_entity(row)

    def get_relationships(self, entity_id: str) -> list[dict]:
        """Get all relationships for an entity (as from or to).

        Returns list of dicts with relationship info and related entity details.
        """
        rows = self.conn.execute(
            """SELECT r.id, r.from_entity_id, r.to_entity_id, r.relationship_type,
                      r.discovered_at, r.last_verified_at,
                      e.name as related_name, e.entity_type as related_type,
                      e.connector_name as related_connector, e.connector_type as related_connector_type
               FROM topology_relationships r
               JOIN topology_entities e ON (
                   CASE WHEN r.from_entity_id = ? THEN r.to_entity_id
                        ELSE r.from_entity_id END = e.id
               )
               WHERE r.from_entity_id = ? OR r.to_entity_id = ?""",
            (entity_id, entity_id, entity_id),
        ).fetchall()

        return [dict(row) for row in rows]

    def get_correlations(self, entity_id: str, status: str | None = None) -> list[dict]:
        """Get correlations for an entity, optionally filtered by status.

        Returns list of dicts with correlation info and both entity details.
        """
        sql = """
            SELECT c.id, c.entity_a_id, c.entity_b_id, c.match_type,
                   c.confidence, c.match_details, c.status,
                   c.discovered_at, c.resolved_at, c.resolved_by,
                   ea.name as entity_a_name, ea.entity_type as entity_a_type,
                   ea.connector_name as entity_a_connector,
                   eb.name as entity_b_name, eb.entity_type as entity_b_type,
                   eb.connector_name as entity_b_connector
            FROM topology_correlations c
            JOIN topology_entities ea ON c.entity_a_id = ea.id
            JOIN topology_entities eb ON c.entity_b_id = eb.id
            WHERE (c.entity_a_id = ? OR c.entity_b_id = ?)
        """
        params: list = [entity_id, entity_id]

        if status is not None:
            sql += " AND c.status = ?"
            params.append(status)

        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_pending_correlations(self) -> list[dict]:
        """Get all pending correlations across all entities.

        Returns list of dicts with correlation info and both entity details.
        """
        rows = self.conn.execute(
            """SELECT c.id, c.entity_a_id, c.entity_b_id, c.match_type,
                      c.confidence, c.match_details, c.status,
                      c.discovered_at, c.resolved_at, c.resolved_by,
                      ea.name as entity_a_name, ea.entity_type as entity_a_type,
                      ea.connector_name as entity_a_connector,
                      eb.name as entity_b_name, eb.entity_type as entity_b_type,
                      eb.connector_name as entity_b_connector
               FROM topology_correlations c
               JOIN topology_entities ea ON c.entity_a_id = ea.id
               JOIN topology_entities eb ON c.entity_b_id = eb.id
               WHERE c.status = 'pending'"""
        ).fetchall()
        return [dict(row) for row in rows]


def _row_to_entity(row: dict) -> TopologyEntity:
    """Convert a sqlite3.Row to a TopologyEntity model."""
    return TopologyEntity(
        id=row["id"],
        name=row["name"],
        connector_id=row["connector_id"],
        connector_name=row["connector_name"],
        entity_type=row["entity_type"],
        connector_type=row["connector_type"],
        scope=json.loads(row["scope_json"] or "{}"),
        canonical_id=row["canonical_id"],
        description=row["description"],
        raw_attributes=json.loads(row["raw_attributes_json"] or "{}"),
        embedding_hash=row["embedding_hash"] or "",
    )

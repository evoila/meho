"""Tests for TopologyStore CRUD and graph query operations."""

import json
import uuid

import pytest

from meho_claude.core.topology.models import (
    ExtractionResult,
    TopologyEntity,
    TopologyRelationship,
)


def _make_entity(
    name="nginx",
    entity_type="Pod",
    connector_type="kubernetes",
    connector_id="k8s-prod",
    connector_name="prod-cluster",
    canonical_id="default/nginx",
    description="",
    raw_attributes=None,
    scope=None,
):
    """Helper to create a TopologyEntity with sensible defaults."""
    return TopologyEntity(
        name=name,
        entity_type=entity_type,
        connector_type=connector_type,
        connector_id=connector_id,
        connector_name=connector_name,
        canonical_id=canonical_id,
        description=description,
        raw_attributes=raw_attributes or {},
        scope=scope or {},
    )


class TestUpsertEntity:
    """TopologyStore.upsert_entity tests."""

    def test_insert_new_entity(self, topology_db, tmp_state_dir):
        """Inserting a new entity should return True (needs embedding)."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)
        entity = _make_entity()
        result = store.upsert_entity(entity)
        assert result is True
        assert entity.id is not None
        store.close()

    def test_insert_assigns_uuid(self, topology_db, tmp_state_dir):
        """New entity should get a UUID id stored in DB."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)
        entity = _make_entity()
        store.upsert_entity(entity)

        row = topology_db.execute(
            "SELECT id, name FROM topology_entities WHERE canonical_id = ?",
            ("default/nginx",),
        ).fetchone()
        assert row is not None
        assert row["name"] == "nginx"
        store.close()

    def test_update_existing_entity_changed_hash(self, topology_db, tmp_state_dir):
        """Updating entity with changed name returns True (needs re-embedding)."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)
        entity = _make_entity(description="old desc")
        store.upsert_entity(entity)

        # Update description (changes embedding hash)
        entity2 = _make_entity(description="new desc")
        result = store.upsert_entity(entity2)
        assert result is True
        store.close()

    def test_update_existing_entity_unchanged_hash(self, topology_db, tmp_state_dir):
        """Updating entity with same name/type/description returns False."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)
        entity = _make_entity(description="same desc")
        store.upsert_entity(entity)

        # Same entity, same embedding-relevant fields
        entity2 = _make_entity(description="same desc")
        result = store.upsert_entity(entity2)
        assert result is False
        store.close()

    def test_update_preserves_id(self, topology_db, tmp_state_dir):
        """Updating an entity should keep the same id."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)
        entity = _make_entity()
        store.upsert_entity(entity)
        original_id = entity.id

        entity2 = _make_entity(description="updated")
        store.upsert_entity(entity2)
        assert entity2.id == original_id
        store.close()

    def test_upsert_stores_scope_and_attributes_as_json(self, topology_db, tmp_state_dir):
        """Scope and raw_attributes should be stored as JSON in DB."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)
        entity = _make_entity(
            scope={"namespace": "prod"},
            raw_attributes={"ip_address": "10.0.1.5"},
        )
        store.upsert_entity(entity)

        row = topology_db.execute(
            "SELECT scope_json, raw_attributes_json FROM topology_entities WHERE canonical_id = ?",
            ("default/nginx",),
        ).fetchone()
        assert json.loads(row["scope_json"]) == {"namespace": "prod"}
        assert json.loads(row["raw_attributes_json"]) == {"ip_address": "10.0.1.5"}
        store.close()


class TestUpsertRelationship:
    """TopologyStore.upsert_relationship tests."""

    def test_insert_new_relationship(self, topology_db, tmp_state_dir):
        """Inserting a new relationship should succeed."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)

        # Create two entities first
        e1 = _make_entity(name="pod-a", canonical_id="default/pod-a")
        e2 = _make_entity(name="node-1", entity_type="Node", canonical_id="node-1")
        store.upsert_entity(e1)
        store.upsert_entity(e2)

        rel = TopologyRelationship(
            from_entity_id=e1.id,
            to_entity_id=e2.id,
            relationship_type="runs_on",
        )
        store.upsert_relationship(rel)

        row = topology_db.execute(
            "SELECT * FROM topology_relationships WHERE from_entity_id = ?",
            (e1.id,),
        ).fetchone()
        assert row is not None
        assert row["relationship_type"] == "runs_on"
        store.close()

    def test_upsert_relationship_idempotent(self, topology_db, tmp_state_dir):
        """Upserting same relationship twice should update last_verified_at."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)

        e1 = _make_entity(name="pod-a", canonical_id="default/pod-a")
        e2 = _make_entity(name="node-1", entity_type="Node", canonical_id="node-1")
        store.upsert_entity(e1)
        store.upsert_entity(e2)

        rel = TopologyRelationship(
            from_entity_id=e1.id,
            to_entity_id=e2.id,
            relationship_type="runs_on",
        )
        store.upsert_relationship(rel)

        # Count should remain 1 after second upsert
        rel2 = TopologyRelationship(
            from_entity_id=e1.id,
            to_entity_id=e2.id,
            relationship_type="runs_on",
        )
        store.upsert_relationship(rel2)

        count = topology_db.execute(
            "SELECT COUNT(*) as c FROM topology_relationships WHERE from_entity_id = ? AND to_entity_id = ?",
            (e1.id, e2.id),
        ).fetchone()["c"]
        assert count == 1
        store.close()


class TestGetEntity:
    """TopologyStore get_entity_* tests."""

    def test_get_entity_by_id(self, topology_db, tmp_state_dir):
        """Should return entity by its UUID id."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)
        entity = _make_entity()
        store.upsert_entity(entity)

        found = store.get_entity_by_id(entity.id)
        assert found is not None
        assert found.name == "nginx"
        assert found.entity_type == "Pod"
        store.close()

    def test_get_entity_by_id_not_found(self, topology_db, tmp_state_dir):
        """Should return None for non-existent id."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)
        found = store.get_entity_by_id("non-existent-id")
        assert found is None
        store.close()

    def test_get_entity_by_canonical(self, topology_db, tmp_state_dir):
        """Should return entity by connector_id + entity_type + canonical_id."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)
        entity = _make_entity()
        store.upsert_entity(entity)

        found = store.get_entity_by_canonical("k8s-prod", "Pod", "default/nginx")
        assert found is not None
        assert found.name == "nginx"
        store.close()

    def test_get_entity_by_canonical_not_found(self, topology_db, tmp_state_dir):
        """Should return None when canonical not found."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)
        found = store.get_entity_by_canonical("fake", "Pod", "fake/pod")
        assert found is None
        store.close()


class TestGetRelationships:
    """TopologyStore.get_relationships tests."""

    def test_get_relationships(self, topology_db, tmp_state_dir):
        """Should return relationships for an entity."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)

        e1 = _make_entity(name="pod-a", canonical_id="default/pod-a")
        e2 = _make_entity(name="node-1", entity_type="Node", canonical_id="node-1")
        store.upsert_entity(e1)
        store.upsert_entity(e2)

        rel = TopologyRelationship(
            from_entity_id=e1.id,
            to_entity_id=e2.id,
            relationship_type="runs_on",
        )
        store.upsert_relationship(rel)

        rels = store.get_relationships(e1.id)
        assert len(rels) >= 1
        assert any(r["relationship_type"] == "runs_on" for r in rels)
        store.close()

    def test_get_relationships_bidirectional(self, topology_db, tmp_state_dir):
        """Should return relationships whether entity is from or to."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)

        e1 = _make_entity(name="pod-a", canonical_id="default/pod-a")
        e2 = _make_entity(name="node-1", entity_type="Node", canonical_id="node-1")
        store.upsert_entity(e1)
        store.upsert_entity(e2)

        rel = TopologyRelationship(
            from_entity_id=e1.id,
            to_entity_id=e2.id,
            relationship_type="runs_on",
        )
        store.upsert_relationship(rel)

        # Query from the to_entity_id side
        rels = store.get_relationships(e2.id)
        assert len(rels) >= 1
        store.close()


class TestGetCorrelations:
    """TopologyStore.get_correlations and get_pending_correlations tests."""

    def test_get_correlations(self, topology_db, tmp_state_dir):
        """Should return correlations for an entity."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)

        # Create two entities from different connectors
        e1 = _make_entity(
            name="server-1", connector_id="k8s-prod", connector_name="prod-cluster",
            canonical_id="default/server-1", raw_attributes={"ip_address": "10.0.1.5"},
        )
        e2 = _make_entity(
            name="vm-1", entity_type="VM", connector_type="vmware",
            connector_id="vc-prod", connector_name="vcenter",
            canonical_id="dc1/vm-1", raw_attributes={"ip_address": "10.0.1.5"},
        )
        store.upsert_entity(e1)
        store.upsert_entity(e2)

        # Insert a correlation manually
        topology_db.execute(
            """INSERT INTO topology_correlations
               (id, entity_a_id, entity_b_id, match_type, confidence, match_details, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), e1.id, e2.id, "ip_match", 0.8, "{}", "pending"),
        )
        topology_db.commit()

        corrs = store.get_correlations(e1.id)
        assert len(corrs) >= 1
        store.close()

    def test_get_correlations_with_status_filter(self, topology_db, tmp_state_dir):
        """Should filter correlations by status."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)

        e1 = _make_entity(name="s1", canonical_id="s1")
        e2 = _make_entity(
            name="s2", entity_type="VM", connector_type="vmware",
            connector_id="vc", connector_name="vcenter", canonical_id="s2",
        )
        store.upsert_entity(e1)
        store.upsert_entity(e2)

        topology_db.execute(
            """INSERT INTO topology_correlations
               (id, entity_a_id, entity_b_id, match_type, confidence, match_details, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), e1.id, e2.id, "ip_match", 0.8, "{}", "confirmed"),
        )
        topology_db.commit()

        # Filter by confirmed
        corrs = store.get_correlations(e1.id, status="confirmed")
        assert len(corrs) >= 1

        # Filter by pending -- should be empty
        corrs = store.get_correlations(e1.id, status="pending")
        assert len(corrs) == 0
        store.close()

    def test_get_pending_correlations(self, topology_db, tmp_state_dir):
        """Should return all pending correlations."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)

        e1 = _make_entity(name="s1", canonical_id="s1")
        e2 = _make_entity(
            name="s2", entity_type="VM", connector_type="vmware",
            connector_id="vc", connector_name="vcenter", canonical_id="s2",
        )
        store.upsert_entity(e1)
        store.upsert_entity(e2)

        topology_db.execute(
            """INSERT INTO topology_correlations
               (id, entity_a_id, entity_b_id, match_type, confidence, match_details, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), e1.id, e2.id, "ip_match", 0.8, "{}", "pending"),
        )
        topology_db.commit()

        pending = store.get_pending_correlations()
        assert len(pending) >= 1
        assert all(p["status"] == "pending" for p in pending)
        store.close()


class TestIngest:
    """TopologyStore.ingest integration tests."""

    def test_ingest_entities_and_relationships(self, topology_db, tmp_state_dir):
        """Ingest should upsert entities then relationships."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)

        e1 = _make_entity(name="pod-a", canonical_id="default/pod-a")
        e2 = _make_entity(name="node-1", entity_type="Node", canonical_id="node-1")

        result = ExtractionResult(
            entities=[e1, e2],
            relationships=[
                TopologyRelationship(
                    from_entity_id=e1.id,
                    to_entity_id=e2.id,
                    relationship_type="runs_on",
                ),
            ],
            source_connector="prod-cluster",
            source_operation="list-pods",
        )

        entities_needing_embed = store.ingest(result)
        assert len(entities_needing_embed) == 2  # Both new entities need embedding

        # Verify entities exist
        row = topology_db.execute("SELECT COUNT(*) as c FROM topology_entities").fetchone()
        assert row["c"] == 2

        # Verify relationship exists
        row = topology_db.execute("SELECT COUNT(*) as c FROM topology_relationships").fetchone()
        assert row["c"] == 1
        store.close()

    def test_ingest_returns_entities_needing_embedding(self, topology_db, tmp_state_dir):
        """Ingest should return list of entities that need embedding."""
        from meho_claude.core.topology.store import TopologyStore

        store = TopologyStore(tmp_state_dir)
        entity = _make_entity()
        result = ExtractionResult(
            entities=[entity], relationships=[],
            source_connector="test", source_operation="test",
        )

        needing_embed = store.ingest(result)
        assert len(needing_embed) == 1

        # Second ingest with same entity -- no change
        entity2 = _make_entity()
        result2 = ExtractionResult(
            entities=[entity2], relationships=[],
            source_connector="test", source_operation="test",
        )
        needing_embed2 = store.ingest(result2)
        assert len(needing_embed2) == 0
        store.close()

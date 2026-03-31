"""Tests for topology Pydantic models and FTS5 migration."""

import hashlib

import pytest


class TestTopologyEntity:
    """TopologyEntity model validation."""

    def test_minimal_entity(self):
        """Entity with only required fields should validate."""
        from meho_claude.core.topology.models import TopologyEntity

        entity = TopologyEntity(
            name="nginx-pod",
            entity_type="Pod",
            connector_type="kubernetes",
            canonical_id="default/nginx-pod",
        )
        assert entity.name == "nginx-pod"
        assert entity.entity_type == "Pod"
        assert entity.connector_type == "kubernetes"
        assert entity.canonical_id == "default/nginx-pod"
        # Defaults
        assert entity.scope == {}
        assert entity.raw_attributes == {}
        assert entity.description == ""
        assert entity.embedding_hash == ""
        assert entity.connector_id is None
        assert entity.connector_name is None

    def test_entity_auto_generates_id(self):
        """Entity id should be auto-generated UUID if not provided."""
        from meho_claude.core.topology.models import TopologyEntity

        entity = TopologyEntity(
            name="nginx",
            entity_type="Pod",
            connector_type="kubernetes",
            canonical_id="default/nginx",
        )
        assert entity.id is not None
        assert len(entity.id) == 36  # UUID format

    def test_entity_with_all_fields(self):
        """Entity with all fields should validate."""
        from meho_claude.core.topology.models import TopologyEntity

        entity = TopologyEntity(
            id="custom-id",
            name="nginx-pod",
            connector_id="k8s-prod",
            connector_name="prod-cluster",
            entity_type="Pod",
            connector_type="kubernetes",
            scope={"namespace": "prod"},
            canonical_id="prod/nginx-pod",
            description="Nginx web server pod",
            raw_attributes={"ip_address": "10.0.1.5"},
            embedding_hash="abc123",
        )
        assert entity.id == "custom-id"
        assert entity.connector_id == "k8s-prod"
        assert entity.connector_name == "prod-cluster"
        assert entity.scope == {"namespace": "prod"}
        assert entity.description == "Nginx web server pod"
        assert entity.raw_attributes == {"ip_address": "10.0.1.5"}
        assert entity.embedding_hash == "abc123"

    def test_entity_missing_required_fields(self):
        """Entity without required fields should fail validation."""
        from meho_claude.core.topology.models import TopologyEntity

        with pytest.raises(Exception):
            TopologyEntity()

    def test_entity_missing_name(self):
        """Entity without name should fail."""
        from meho_claude.core.topology.models import TopologyEntity

        with pytest.raises(Exception):
            TopologyEntity(
                entity_type="Pod",
                connector_type="kubernetes",
                canonical_id="default/nginx",
            )


class TestTopologyRelationship:
    """TopologyRelationship model validation."""

    def test_valid_relationship(self):
        """Relationship with valid type should validate."""
        from meho_claude.core.topology.models import TopologyRelationship

        rel = TopologyRelationship(
            from_entity_id="entity-a",
            to_entity_id="entity-b",
            relationship_type="runs_on",
        )
        assert rel.from_entity_id == "entity-a"
        assert rel.to_entity_id == "entity-b"
        assert rel.relationship_type == "runs_on"
        assert rel.id is not None

    def test_all_valid_relationship_types(self):
        """All defined relationship types should validate."""
        from meho_claude.core.topology.models import TopologyRelationship

        valid_types = ["runs_on", "routes_to", "uses_storage", "member_of", "contains", "connects_to"]
        for rel_type in valid_types:
            rel = TopologyRelationship(
                from_entity_id="a",
                to_entity_id="b",
                relationship_type=rel_type,
            )
            assert rel.relationship_type == rel_type

    def test_invalid_relationship_type(self):
        """Invalid relationship type should fail validation."""
        from meho_claude.core.topology.models import TopologyRelationship

        with pytest.raises(Exception):
            TopologyRelationship(
                from_entity_id="a",
                to_entity_id="b",
                relationship_type="invalid_type",
            )

    def test_relationship_missing_fields(self):
        """Relationship without required fields should fail."""
        from meho_claude.core.topology.models import TopologyRelationship

        with pytest.raises(Exception):
            TopologyRelationship(from_entity_id="a")


class TestTopologyCorrelation:
    """TopologyCorrelation model validation."""

    def test_minimal_correlation(self):
        """Correlation with required fields should validate with defaults."""
        from meho_claude.core.topology.models import TopologyCorrelation

        corr = TopologyCorrelation(
            entity_a_id="entity-a",
            entity_b_id="entity-b",
            match_type="ip_match",
        )
        assert corr.entity_a_id == "entity-a"
        assert corr.entity_b_id == "entity-b"
        assert corr.match_type == "ip_match"
        assert corr.confidence == 0.0
        assert corr.match_details == {}
        assert corr.status == "pending"

    def test_correlation_confidence_bounds(self):
        """Confidence must be between 0.0 and 1.0."""
        from meho_claude.core.topology.models import TopologyCorrelation

        # Valid bounds
        corr = TopologyCorrelation(
            entity_a_id="a", entity_b_id="b", match_type="ip_match",
            confidence=0.0,
        )
        assert corr.confidence == 0.0

        corr = TopologyCorrelation(
            entity_a_id="a", entity_b_id="b", match_type="ip_match",
            confidence=1.0,
        )
        assert corr.confidence == 1.0

    def test_correlation_confidence_out_of_bounds(self):
        """Confidence outside 0.0-1.0 should fail."""
        from meho_claude.core.topology.models import TopologyCorrelation

        with pytest.raises(Exception):
            TopologyCorrelation(
                entity_a_id="a", entity_b_id="b", match_type="test",
                confidence=1.5,
            )

        with pytest.raises(Exception):
            TopologyCorrelation(
                entity_a_id="a", entity_b_id="b", match_type="test",
                confidence=-0.1,
            )

    def test_correlation_valid_statuses(self):
        """All valid status values should work."""
        from meho_claude.core.topology.models import TopologyCorrelation

        for status in ["pending", "confirmed", "rejected"]:
            corr = TopologyCorrelation(
                entity_a_id="a", entity_b_id="b", match_type="test",
                status=status,
            )
            assert corr.status == status

    def test_correlation_invalid_status(self):
        """Invalid status should fail validation."""
        from meho_claude.core.topology.models import TopologyCorrelation

        with pytest.raises(Exception):
            TopologyCorrelation(
                entity_a_id="a", entity_b_id="b", match_type="test",
                status="invalid",
            )

    def test_correlation_with_match_details(self):
        """Correlation with match evidence should validate."""
        from meho_claude.core.topology.models import TopologyCorrelation

        evidence = {
            "match_field": "ip_address",
            "entity_a_value": "10.0.1.5",
            "entity_b_value": "10.0.1.5",
            "match_type": "exact",
        }
        corr = TopologyCorrelation(
            entity_a_id="a", entity_b_id="b",
            match_type="ip_match",
            confidence=0.8,
            match_details=evidence,
            status="pending",
        )
        assert corr.match_details == evidence


class TestExtractionResult:
    """ExtractionResult model validation."""

    def test_extraction_result(self):
        """ExtractionResult should hold entities and relationships."""
        from meho_claude.core.topology.models import (
            ExtractionResult,
            TopologyEntity,
            TopologyRelationship,
        )

        entity = TopologyEntity(
            name="nginx", entity_type="Pod",
            connector_type="kubernetes", canonical_id="default/nginx",
        )
        rel = TopologyRelationship(
            from_entity_id="a", to_entity_id="b",
            relationship_type="runs_on",
        )
        result = ExtractionResult(
            entities=[entity],
            relationships=[rel],
            source_connector="prod-cluster",
            source_operation="list-pods",
        )
        assert len(result.entities) == 1
        assert len(result.relationships) == 1
        assert result.source_connector == "prod-cluster"
        assert result.source_operation == "list-pods"

    def test_extraction_result_empty_lists(self):
        """ExtractionResult with empty lists should validate."""
        from meho_claude.core.topology.models import ExtractionResult

        result = ExtractionResult(
            entities=[], relationships=[],
            source_connector="test", source_operation="test-op",
        )
        assert result.entities == []
        assert result.relationships == []


class TestComputeEmbeddingHash:
    """compute_embedding_hash function tests."""

    def test_returns_sha256_hex(self):
        """Should return SHA-256 hex digest."""
        from meho_claude.core.topology.models import compute_embedding_hash

        result = compute_embedding_hash("Pod", "nginx", "web server")
        expected = hashlib.sha256("Pod:nginx:web server".encode("utf-8")).hexdigest()
        assert result == expected

    def test_deterministic(self):
        """Same inputs should produce same output."""
        from meho_claude.core.topology.models import compute_embedding_hash

        h1 = compute_embedding_hash("Pod", "nginx", "web server")
        h2 = compute_embedding_hash("Pod", "nginx", "web server")
        assert h1 == h2

    def test_different_inputs_different_hash(self):
        """Different inputs should produce different hashes."""
        from meho_claude.core.topology.models import compute_embedding_hash

        h1 = compute_embedding_hash("Pod", "nginx", "web server")
        h2 = compute_embedding_hash("VM", "nginx", "web server")
        h3 = compute_embedding_hash("Pod", "apache", "web server")
        h4 = compute_embedding_hash("Pod", "nginx", "app server")
        assert len({h1, h2, h3, h4}) == 4  # All different

    def test_empty_description(self):
        """Should handle empty description."""
        from meho_claude.core.topology.models import compute_embedding_hash

        result = compute_embedding_hash("Pod", "nginx", "")
        expected = hashlib.sha256("Pod:nginx:".encode("utf-8")).hexdigest()
        assert result == expected


class TestFTS5Migration:
    """FTS5 migration 002 tests using topology_db fixture."""

    def test_embedding_hash_column_exists(self, topology_db):
        """After migration 002, topology_entities should have embedding_hash column."""
        row = topology_db.execute(
            "SELECT embedding_hash FROM topology_entities LIMIT 0"
        ).description
        # If column doesn't exist, this would raise OperationalError
        assert any(col[0] == "embedding_hash" for col in row)

    def test_fts5_virtual_table_exists(self, topology_db):
        """topology_entities_fts virtual table should exist after migration 002."""
        tables = topology_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='topology_entities_fts'"
        ).fetchall()
        assert len(tables) == 1

    def test_user_version_is_2(self, topology_db):
        """PRAGMA user_version should be 2 after migration 002."""
        version = topology_db.execute("PRAGMA user_version").fetchone()[0]
        assert version == 2

    def test_fts5_insert_trigger(self, topology_db):
        """Inserting into topology_entities should make entity searchable via FTS5."""
        import uuid

        entity_id = str(uuid.uuid4())
        topology_db.execute(
            """INSERT INTO topology_entities
               (id, name, entity_type, connector_type, canonical_id, description)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (entity_id, "nginx-deployment", "Deployment", "kubernetes",
             "prod/nginx", "Nginx web server deployment"),
        )
        topology_db.commit()

        results = topology_db.execute(
            "SELECT * FROM topology_entities_fts WHERE topology_entities_fts MATCH 'nginx'"
        ).fetchall()
        assert len(results) >= 1

    def test_fts5_update_trigger(self, topology_db):
        """Updating topology_entities should update FTS5 results."""
        import uuid

        entity_id = str(uuid.uuid4())
        topology_db.execute(
            """INSERT INTO topology_entities
               (id, name, entity_type, connector_type, canonical_id, description)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (entity_id, "old-name", "Pod", "kubernetes", "default/old", "old description"),
        )
        topology_db.commit()

        # Verify searchable under old name
        results = topology_db.execute(
            "SELECT * FROM topology_entities_fts WHERE topology_entities_fts MATCH 'old'"
        ).fetchall()
        assert len(results) >= 1

        # Update name and description
        topology_db.execute(
            "UPDATE topology_entities SET name = ?, description = ? WHERE id = ?",
            ("new-name", "new description", entity_id),
        )
        topology_db.commit()

        # Old name should not match
        results = topology_db.execute(
            "SELECT * FROM topology_entities_fts WHERE topology_entities_fts MATCH 'old'"
        ).fetchall()
        assert len(results) == 0

        # New name should match
        results = topology_db.execute(
            "SELECT * FROM topology_entities_fts WHERE topology_entities_fts MATCH 'new'"
        ).fetchall()
        assert len(results) >= 1

    def test_fts5_delete_trigger(self, topology_db):
        """Deleting from topology_entities should remove from FTS5."""
        import uuid

        entity_id = str(uuid.uuid4())
        topology_db.execute(
            """INSERT INTO topology_entities
               (id, name, entity_type, connector_type, canonical_id, description)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (entity_id, "deleteme-entity", "VM", "vmware", "dc1/deleteme", "to be deleted"),
        )
        topology_db.commit()

        # Verify searchable
        results = topology_db.execute(
            "SELECT * FROM topology_entities_fts WHERE topology_entities_fts MATCH 'deleteme'"
        ).fetchall()
        assert len(results) >= 1

        # Delete
        topology_db.execute("DELETE FROM topology_entities WHERE id = ?", (entity_id,))
        topology_db.commit()

        # Should no longer be found
        results = topology_db.execute(
            "SELECT * FROM topology_entities_fts WHERE topology_entities_fts MATCH 'deleteme'"
        ).fetchall()
        assert len(results) == 0

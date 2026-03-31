# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Contract tests for Topology Service API.

Verifies that TopologyService provides the API that consumers (Agent) expect.
These tests ensure the interface contract is maintained as the implementation evolves.
"""

import inspect


class TestTopologyServiceContract:
    """Test TopologyService API contract."""

    def test_topology_service_exists(self):
        """Verify TopologyService can be imported."""
        from meho_app.modules.topology import TopologyService

        assert TopologyService is not None

    def test_topology_service_has_store_discovery_method(self):
        """Verify store_discovery method exists."""
        from meho_app.modules.topology import TopologyService

        assert hasattr(TopologyService, "store_discovery")
        assert callable(TopologyService.store_discovery)

    def test_topology_service_has_lookup_method(self):
        """Verify lookup method exists."""
        from meho_app.modules.topology import TopologyService

        assert hasattr(TopologyService, "lookup")
        assert callable(TopologyService.lookup)

    def test_topology_service_has_invalidate_method(self):
        """Verify invalidate method exists."""
        from meho_app.modules.topology import TopologyService

        assert hasattr(TopologyService, "invalidate")
        assert callable(TopologyService.invalidate)

    def test_store_discovery_signature(self):
        """Verify store_discovery method signature."""
        from meho_app.modules.topology import TopologyService

        sig = inspect.signature(TopologyService.store_discovery)
        params = list(sig.parameters.keys())

        assert "self" in params
        assert "input" in params
        assert "tenant_id" in params

    def test_lookup_signature(self):
        """Verify lookup method signature."""
        from meho_app.modules.topology import TopologyService

        sig = inspect.signature(TopologyService.lookup)
        params = list(sig.parameters.keys())

        assert "self" in params
        assert "input" in params
        assert "tenant_id" in params

    def test_invalidate_signature(self):
        """Verify invalidate method signature."""
        from meho_app.modules.topology import TopologyService

        sig = inspect.signature(TopologyService.invalidate)
        params = list(sig.parameters.keys())

        assert "self" in params
        assert "input" in params
        assert "tenant_id" in params


class TestTopologyEntitySchemaContract:
    """Test TopologyEntity schema contract."""

    def test_entity_schema_exists(self):
        """Verify TopologyEntity schema can be imported."""
        from meho_app.modules.topology import TopologyEntity

        assert TopologyEntity is not None

    def test_entity_schema_has_required_fields(self):
        """Verify TopologyEntity has all required fields."""
        from meho_app.modules.topology import TopologyEntity

        fields = TopologyEntity.model_fields.keys()

        required_fields = [
            "id",
            "name",
            "entity_type",  # TASK-156: Entity classification
            "connector_type",  # TASK-156: Connector type
            "connector_id",
            "scope",  # TASK-156: Scoping context
            "canonical_id",  # TASK-156: Unique ID within connector+type
            "description",
            "raw_attributes",
            "discovered_at",
            "last_verified_at",
            "stale_at",
            "tenant_id",
        ]

        for field in required_fields:
            assert field in fields, f"TopologyEntity should have field: {field}"

    def test_entity_create_schema_has_required_fields(self):
        """Verify TopologyEntityCreate has all required fields."""
        from meho_app.modules.topology import TopologyEntityCreate

        fields = TopologyEntityCreate.model_fields.keys()

        required_fields = [
            "name",
            "entity_type",  # TASK-156: Entity classification
            "description",
        ]

        for field in required_fields:
            assert field in fields, f"TopologyEntityCreate should have field: {field}"


class TestTopologyRelationshipSchemaContract:
    """Test TopologyRelationship schema contract."""

    def test_relationship_schema_exists(self):
        """Verify TopologyRelationship schema can be imported."""
        from meho_app.modules.topology import TopologyRelationship

        assert TopologyRelationship is not None

    def test_relationship_schema_has_required_fields(self):
        """Verify TopologyRelationship has all required fields."""
        from meho_app.modules.topology import TopologyRelationship

        fields = TopologyRelationship.model_fields.keys()

        required_fields = [
            "id",
            "from_entity_id",
            "to_entity_id",
            "relationship_type",
            "discovered_at",
        ]

        for field in required_fields:
            assert field in fields, f"TopologyRelationship should have field: {field}"


class TestTopologySameAsSchemaContract:
    """Test TopologySameAs schema contract."""

    def test_same_as_schema_exists(self):
        """Verify TopologySameAs schema can be imported."""
        from meho_app.modules.topology import TopologySameAs

        assert TopologySameAs is not None

    def test_same_as_schema_has_required_fields(self):
        """Verify TopologySameAs has all required fields."""
        from meho_app.modules.topology import TopologySameAs

        fields = TopologySameAs.model_fields.keys()

        required_fields = [
            "id",
            "entity_a_id",
            "entity_b_id",
            "similarity_score",
            "verified_via",
            "discovered_at",
        ]

        for field in required_fields:
            assert field in fields, f"TopologySameAs should have field: {field}"

    def test_same_as_create_requires_verified_via(self):
        """
        Verify TopologySameAsCreate requires verified_via field.

        This is critical: SAME_AS relationships must be verified via API
        before storage. This contract ensures the verification requirement
        is enforced at the schema level.
        """
        from meho_app.modules.topology import TopologySameAsCreate

        fields = TopologySameAsCreate.model_fields

        assert "verified_via" in fields, (
            "TopologySameAsCreate must have verified_via field. "
            "SAME_AS relationships require verification evidence."
        )

        # Verify it's required (not optional)
        verified_via_field = fields["verified_via"]
        assert verified_via_field.is_required(), "verified_via should be required, not optional"


class TestStoreDiscoveryInputContract:
    """Test StoreDiscoveryInput schema contract."""

    def test_store_discovery_input_exists(self):
        """Verify StoreDiscoveryInput schema can be imported."""
        from meho_app.modules.topology import StoreDiscoveryInput

        assert StoreDiscoveryInput is not None

    def test_store_discovery_input_has_required_fields(self):
        """Verify StoreDiscoveryInput has all required fields."""
        from meho_app.modules.topology import StoreDiscoveryInput

        fields = StoreDiscoveryInput.model_fields.keys()

        required_fields = [
            "entities",
            "relationships",
            "same_as",
        ]

        for field in required_fields:
            assert field in fields, f"StoreDiscoveryInput should have field: {field}"


class TestLookupTopologyInputContract:
    """Test LookupTopologyInput schema contract."""

    def test_lookup_input_exists(self):
        """Verify LookupTopologyInput schema can be imported."""
        from meho_app.modules.topology import LookupTopologyInput

        assert LookupTopologyInput is not None

    def test_lookup_input_has_required_fields(self):
        """Verify LookupTopologyInput has all required fields."""
        from meho_app.modules.topology import LookupTopologyInput

        fields = LookupTopologyInput.model_fields.keys()

        required_fields = [
            "query",
            "traverse_depth",
            "cross_connectors",
        ]

        for field in required_fields:
            assert field in fields, f"LookupTopologyInput should have field: {field}"

    def test_lookup_input_defaults(self):
        """Verify LookupTopologyInput has sensible defaults."""
        from meho_app.modules.topology import LookupTopologyInput

        # Should be able to create with just query
        input = LookupTopologyInput(query="test-entity")

        # Defaults should be applied
        assert input.traverse_depth == 10
        assert input.cross_connectors is True


class TestLookupTopologyResultContract:
    """Test LookupTopologyResult schema contract."""

    def test_lookup_result_exists(self):
        """Verify LookupTopologyResult schema can be imported."""
        from meho_app.modules.topology import LookupTopologyResult

        assert LookupTopologyResult is not None

    def test_lookup_result_has_required_fields(self):
        """Verify LookupTopologyResult has all required fields."""
        from meho_app.modules.topology import LookupTopologyResult

        fields = LookupTopologyResult.model_fields.keys()

        required_fields = [
            "found",
            "entity",
            "topology_chain",
            "connectors_traversed",
            "possibly_related",
            "suggestions",
        ]

        for field in required_fields:
            assert field in fields, f"LookupTopologyResult should have field: {field}"

    def test_lookup_result_not_found_case(self):
        """Verify LookupTopologyResult can represent 'not found' case."""
        from meho_app.modules.topology import LookupTopologyResult

        # Should be able to create a "not found" result
        result = LookupTopologyResult(
            found=False,
            suggestions=["Try searching K8s ingresses"],
        )

        assert result.found is False
        assert result.entity is None
        assert len(result.topology_chain) == 0
        assert len(result.suggestions) > 0


class TestTopologyChainItemContract:
    """Test TopologyChainItem schema contract."""

    def test_chain_item_exists(self):
        """Verify TopologyChainItem schema can be imported."""
        from meho_app.modules.topology import TopologyChainItem

        assert TopologyChainItem is not None

    def test_chain_item_has_required_fields(self):
        """Verify TopologyChainItem has all required fields for visualization."""
        from meho_app.modules.topology import TopologyChainItem

        fields = TopologyChainItem.model_fields.keys()

        required_fields = [
            "depth",
            "entity",
            "connector",
            "connector_id",
            "relationship",
        ]

        for field in required_fields:
            assert field in fields, f"TopologyChainItem should have field: {field}"


class TestPossiblyRelatedEntityContract:
    """Test PossiblyRelatedEntity schema contract."""

    def test_possibly_related_exists(self):
        """Verify PossiblyRelatedEntity schema can be imported."""
        from meho_app.modules.topology import PossiblyRelatedEntity

        assert PossiblyRelatedEntity is not None

    def test_possibly_related_has_similarity_score(self):
        """
        Verify PossiblyRelatedEntity has similarity score.

        This is important for the agent to decide whether to
        investigate and create a SAME_AS relationship.
        """
        from meho_app.modules.topology import PossiblyRelatedEntity

        fields = PossiblyRelatedEntity.model_fields.keys()

        assert "similarity" in fields, (
            "PossiblyRelatedEntity must have similarity field "
            "so the agent can decide whether to verify SAME_AS"
        )


class TestRelationshipTypeContract:
    """Test RelationshipType enum contract."""

    def test_relationship_type_exists(self):
        """Verify RelationshipType enum can be imported."""
        from meho_app.modules.topology import RelationshipType

        assert RelationshipType is not None

    def test_relationship_type_has_common_types(self):
        """Verify RelationshipType has common relationship types."""
        from meho_app.modules.topology import RelationshipType

        common_types = [
            "routes_to",
            "runs_on",
            "uses",
            "resolves_to",
            "depends_on",
        ]

        enum_values = [t.value for t in RelationshipType]

        for rel_type in common_types:
            assert rel_type in enum_values, (
                f"RelationshipType should include '{rel_type}' for common relationships"
            )


class TestTopologyRepositoryContract:
    """Test TopologyRepository API contract."""

    def test_repository_exists(self):
        """Verify TopologyRepository can be imported."""
        from meho_app.modules.topology.repository import TopologyRepository

        assert TopologyRepository is not None

    def test_repository_has_crud_methods(self):
        """Verify TopologyRepository has CRUD methods."""
        from meho_app.modules.topology.repository import TopologyRepository

        crud_methods = [
            "create_entity",
            "get_entity_by_id",
            "get_entity_by_name",
            "create_relationship",
            "create_same_as",
            "mark_entity_stale",
        ]

        for method in crud_methods:
            assert hasattr(TopologyRepository, method), (
                f"TopologyRepository should have method: {method}"
            )

    def test_repository_has_traversal_method(self):
        """Verify TopologyRepository has graph traversal method."""
        from meho_app.modules.topology.repository import TopologyRepository

        assert hasattr(TopologyRepository, "traverse_topology"), (
            "TopologyRepository should have traverse_topology method"
        )

    def test_repository_has_similarity_search(self):
        """Verify TopologyRepository has similarity search for SAME_AS discovery."""
        from meho_app.modules.topology.repository import TopologyRepository

        assert hasattr(TopologyRepository, "find_similar_entities"), (
            "TopologyRepository should have find_similar_entities for SAME_AS discovery"
        )


class TestTopologySemanticSearchContract:
    """Test TopologyService semantic search API contract."""

    def test_topology_service_has_semantic_search(self):
        """Verify TopologyService has _semantic_search method for fallback lookup."""
        from meho_app.modules.topology import TopologyService

        assert hasattr(TopologyService, "_semantic_search"), (
            "TopologyService should have _semantic_search method "
            "for semantic fallback when exact matching fails"
        )
        assert callable(TopologyService._semantic_search)

    def test_semantic_search_signature(self):
        """Verify _semantic_search method signature."""
        from meho_app.modules.topology import TopologyService

        sig = inspect.signature(TopologyService._semantic_search)
        params = list(sig.parameters.keys())

        assert "self" in params
        assert "query" in params
        assert "tenant_id" in params
        assert "limit" in params
        assert "min_similarity" in params

    def test_lookup_uses_two_stage_search(self):
        """
        Verify lookup method performs 2-stage search.

        The lookup method should:
        1. Try exact name match first (instant, no API call)
        2. Fall back to semantic search via embeddings (flexible)

        Pattern matching was removed as semantic search handles
        partial matches better (e.g., "namespace-service" vs "service").
        """
        import inspect

        from meho_app.modules.topology import TopologyService

        # Get the source code of the lookup method
        source = inspect.getsource(TopologyService.lookup)

        # Verify it mentions the 2-stage approach
        assert "get_entity_by_name" in source, "lookup should use exact name match first"
        assert "_semantic_search" in source, "lookup should fall back to semantic search"
        # Pattern matching should NOT be used (semantic is better for this)
        assert "find_entities_by_name_pattern" not in source, (
            "lookup should NOT use pattern matching - semantic search handles this better"
        )


class TestTopologyModelContract:
    """Test TopologyEntityModel database model contract."""

    def test_entity_model_exists(self):
        """Verify TopologyEntityModel can be imported."""
        from meho_app.modules.topology.models import TopologyEntityModel

        assert TopologyEntityModel is not None

    def test_entity_model_has_required_columns(self):
        """Verify TopologyEntityModel has required columns."""
        from meho_app.modules.topology.models import TopologyEntityModel

        # Check table name
        assert TopologyEntityModel.__tablename__ == "topology_entities"

        # Check required columns exist
        columns = [c.name for c in TopologyEntityModel.__table__.columns]

        required_columns = [
            "id",
            "name",
            "entity_type",  # TASK-156: Entity classification
            "connector_type",  # TASK-156: Connector type
            "connector_id",
            "scope",  # TASK-156: Scoping context
            "canonical_id",  # TASK-156: Unique ID within connector+type
            "description",
            "raw_attributes",
            "discovered_at",
            "last_verified_at",
            "stale_at",
            "tenant_id",
        ]

        for col in required_columns:
            assert col in columns, f"TopologyEntityModel should have column: {col}"

    def test_embedding_model_exists(self):
        """Verify TopologyEmbeddingModel can be imported."""
        from meho_app.modules.topology.models import TopologyEmbeddingModel

        assert TopologyEmbeddingModel is not None
        assert TopologyEmbeddingModel.__tablename__ == "topology_embeddings"

    def test_relationship_model_exists(self):
        """Verify TopologyRelationshipModel can be imported."""
        from meho_app.modules.topology.models import TopologyRelationshipModel

        assert TopologyRelationshipModel is not None
        assert TopologyRelationshipModel.__tablename__ == "topology_relationships"

    def test_same_as_model_exists(self):
        """Verify TopologySameAsModel can be imported."""
        from meho_app.modules.topology.models import TopologySameAsModel

        assert TopologySameAsModel is not None
        assert TopologySameAsModel.__tablename__ == "topology_same_as"


class TestTopologyCascadeDeleteContract:
    """Test cascade delete API contract for topology cleanup on connector deletion."""

    def test_topology_service_has_delete_entities_for_connector(self):
        """Verify TopologyService has method to delete entities by connector."""
        from meho_app.modules.topology import TopologyService

        assert hasattr(TopologyService, "delete_entities_for_connector"), (
            "TopologyService should have delete_entities_for_connector method "
            "for cleaning up topology when connectors are deleted"
        )
        assert callable(TopologyService.delete_entities_for_connector)

    def test_topology_service_has_cleanup_orphaned_entities(self):
        """Verify TopologyService has method to cleanup orphaned entities."""
        from meho_app.modules.topology import TopologyService

        assert hasattr(TopologyService, "cleanup_orphaned_entities"), (
            "TopologyService should have cleanup_orphaned_entities method "
            "for cleaning up entities whose connectors no longer exist"
        )
        assert callable(TopologyService.cleanup_orphaned_entities)

    def test_delete_entities_for_connector_signature(self):
        """Verify delete_entities_for_connector method signature."""
        from meho_app.modules.topology import TopologyService

        sig = inspect.signature(TopologyService.delete_entities_for_connector)
        params = list(sig.parameters.keys())

        assert "self" in params
        assert "connector_id" in params

    def test_cleanup_orphaned_entities_signature(self):
        """Verify cleanup_orphaned_entities method signature."""
        from meho_app.modules.topology import TopologyService

        sig = inspect.signature(TopologyService.cleanup_orphaned_entities)
        params = list(sig.parameters.keys())

        assert "self" in params
        assert "valid_connector_ids" in params

    def test_topology_repository_has_delete_by_connector(self):
        """Verify TopologyRepository has delete_entities_by_connector method."""
        from meho_app.modules.topology.repository import TopologyRepository

        assert hasattr(TopologyRepository, "delete_entities_by_connector"), (
            "TopologyRepository should have delete_entities_by_connector method"
        )
        assert callable(TopologyRepository.delete_entities_by_connector)

    def test_topology_repository_has_delete_orphaned(self):
        """Verify TopologyRepository has delete_orphaned_entities method."""
        from meho_app.modules.topology.repository import TopologyRepository

        assert hasattr(TopologyRepository, "delete_orphaned_entities"), (
            "TopologyRepository should have delete_orphaned_entities method"
        )
        assert callable(TopologyRepository.delete_orphaned_entities)

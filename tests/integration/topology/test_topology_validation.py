# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for topology schema validation.

Tests the validation of entity types and relationships against connector schemas
during store_discovery operations.

Test cases:
- Valid K8s entities accepted
- Invalid entity types rejected with clear error
- Valid K8s relationships accepted
- Invalid relationships rejected with clear error
- Cross-connector entities rejected (e.g., VMware VM in K8s connector)
- Deduplication by canonical ID
- Scoped canonical ID generation (different namespaces = different entities)
- Unknown connector types skip validation
- Validation errors in result with partial success
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from meho_app.modules.topology.schema import (
    get_topology_schema,
)
from meho_app.modules.topology.schemas import (
    StoreDiscoveryInput,
    TopologyEntityCreate,
    TopologyRelationshipCreate,
)
from meho_app.modules.topology.service import TopologyService


class TestTopologyValidationIntegration:
    """Integration tests for topology schema validation."""

    @pytest.fixture
    def mock_session(self):
        """Create mock database session."""
        session = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        session.flush = AsyncMock()
        session.refresh = AsyncMock()
        return session

    @pytest.fixture
    def mock_embedding_service(self):
        """Create mock embedding service."""
        service = MagicMock()
        service.generate_embedding = AsyncMock(return_value=[0.1] * 1536)
        return service

    @pytest.fixture
    def topology_service(self, mock_session, mock_embedding_service):
        """Create TopologyService with mocks."""
        service = TopologyService(mock_session, mock_embedding_service)
        return service

    # =========================================================================
    # Valid Entity Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_valid_k8s_entities_accepted(self, topology_service, mock_session):
        """Test that valid Kubernetes entity types are accepted."""
        # Setup mock repository to return new entities
        mock_entity = MagicMock()
        mock_entity.id = uuid4()
        mock_entity.entity_type = "Pod"

        topology_service.repository.upsert_entity = AsyncMock(return_value=(mock_entity, True))
        topology_service.repository.store_embedding = AsyncMock()

        input_data = StoreDiscoveryInput(
            connector_type="kubernetes",
            connector_id=uuid4(),
            entities=[
                TopologyEntityCreate(
                    name="nginx",
                    entity_type="Pod",
                    connector_type="kubernetes",
                    scope={"namespace": "prod"},
                    description="NGINX web server pod in production namespace",
                ),
                TopologyEntityCreate(
                    name="web-app",
                    entity_type="Deployment",
                    connector_type="kubernetes",
                    scope={"namespace": "prod"},
                    description="Web application deployment",
                ),
                TopologyEntityCreate(
                    name="web-service",
                    entity_type="Service",
                    connector_type="kubernetes",
                    scope={"namespace": "prod"},
                    description="Web application service",
                ),
            ],
        )

        result = await topology_service.store_discovery(input_data, tenant_id="test-tenant")

        assert result.stored is True
        assert result.entities_created == 3
        assert len(result.validation_errors) == 0

    @pytest.mark.asyncio
    async def test_valid_vmware_entities_accepted(self, topology_service, mock_session):
        """Test that valid VMware entity types are accepted."""
        mock_entity = MagicMock()
        mock_entity.id = uuid4()
        mock_entity.entity_type = "VM"

        topology_service.repository.upsert_entity = AsyncMock(return_value=(mock_entity, True))
        topology_service.repository.store_embedding = AsyncMock()

        input_data = StoreDiscoveryInput(
            connector_type="vmware",
            connector_id=uuid4(),
            entities=[
                TopologyEntityCreate(
                    name="web-server",
                    entity_type="VM",
                    connector_type="vmware",
                    scope={"moref": "vm-123"},
                    description="Web server virtual machine",
                ),
                TopologyEntityCreate(
                    name="esxi-01",
                    entity_type="Host",
                    connector_type="vmware",
                    scope={"cluster": "prod-cluster"},
                    description="ESXi host in production cluster",
                ),
            ],
        )

        result = await topology_service.store_discovery(input_data, tenant_id="test-tenant")

        assert result.stored is True
        assert result.entities_created == 2
        assert len(result.validation_errors) == 0

    # =========================================================================
    # Invalid Entity Type Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_invalid_entity_type_rejected(self, topology_service, mock_session):
        """Test that invalid entity types are rejected with clear error."""
        input_data = StoreDiscoveryInput(
            connector_type="kubernetes",
            connector_id=uuid4(),
            entities=[
                TopologyEntityCreate(
                    name="some-resource",
                    entity_type="InvalidType",  # Not a K8s entity type
                    connector_type="kubernetes",
                    description="Some unknown resource",
                ),
            ],
        )

        result = await topology_service.store_discovery(input_data, tenant_id="test-tenant")

        assert result.stored is True  # Partial success
        assert result.entities_created == 0
        assert len(result.validation_errors) == 1
        assert "Invalid entity type 'InvalidType'" in result.validation_errors[0]
        assert "kubernetes" in result.validation_errors[0]
        assert "Valid types:" in result.validation_errors[0]

    @pytest.mark.asyncio
    async def test_cross_connector_entity_type_rejected(self, topology_service, mock_session):
        """Test that VMware entity types are rejected in K8s connector."""
        input_data = StoreDiscoveryInput(
            connector_type="kubernetes",
            connector_id=uuid4(),
            entities=[
                TopologyEntityCreate(
                    name="web-server",
                    entity_type="VM",  # VMware type, not K8s
                    connector_type="kubernetes",
                    description="This should fail validation",
                ),
            ],
        )

        result = await topology_service.store_discovery(input_data, tenant_id="test-tenant")

        assert result.stored is True
        assert result.entities_created == 0
        assert len(result.validation_errors) == 1
        assert "Invalid entity type 'VM'" in result.validation_errors[0]

    # =========================================================================
    # Valid Relationship Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_valid_k8s_relationships_accepted(self, topology_service, mock_session):
        """Test that valid K8s relationships are accepted."""
        pod_id = uuid4()
        node_id = uuid4()

        # Mock entities in the batch
        pod_entity = MagicMock()
        pod_entity.id = pod_id
        pod_entity.entity_type = "Pod"

        node_entity = MagicMock()
        node_entity.id = node_id
        node_entity.entity_type = "Node"

        # Track which entity is being created
        entity_counter = [0]

        def mock_upsert_side_effect(entity, tenant_id, canonical_id):
            result = [pod_entity, node_entity][entity_counter[0] % 2]
            entity_counter[0] += 1
            return (result, True)

        topology_service.repository.upsert_entity = AsyncMock(side_effect=mock_upsert_side_effect)
        topology_service.repository.store_embedding = AsyncMock()
        topology_service.repository.get_relationship = AsyncMock(return_value=None)
        topology_service.repository.create_relationship = AsyncMock()

        input_data = StoreDiscoveryInput(
            connector_type="kubernetes",
            connector_id=uuid4(),
            entities=[
                TopologyEntityCreate(
                    name="nginx",
                    entity_type="Pod",
                    connector_type="kubernetes",
                    scope={"namespace": "prod"},
                    description="NGINX pod",
                ),
                TopologyEntityCreate(
                    name="worker-01",
                    entity_type="Node",
                    connector_type="kubernetes",
                    description="Worker node",
                ),
            ],
            relationships=[
                TopologyRelationshipCreate(
                    from_entity_name="nginx",
                    to_entity_name="worker-01",
                    relationship_type="runs_on",
                ),
            ],
        )

        result = await topology_service.store_discovery(input_data, tenant_id="test-tenant")

        assert result.stored is True
        assert result.entities_created == 2
        assert result.relationships_created == 1
        assert len(result.validation_errors) == 0

    # =========================================================================
    # Invalid Relationship Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_invalid_relationship_rejected(self, topology_service, mock_session):
        """Test that invalid relationships are rejected with clear error."""
        pod_id = uuid4()
        namespace_id = uuid4()

        pod_entity = MagicMock()
        pod_entity.id = pod_id
        pod_entity.entity_type = "Pod"

        namespace_entity = MagicMock()
        namespace_entity.id = namespace_id
        namespace_entity.entity_type = "Namespace"

        entity_counter = [0]

        def mock_upsert_side_effect(entity, tenant_id, canonical_id):
            result = [pod_entity, namespace_entity][entity_counter[0] % 2]
            entity_counter[0] += 1
            return (result, True)

        topology_service.repository.upsert_entity = AsyncMock(side_effect=mock_upsert_side_effect)
        topology_service.repository.store_embedding = AsyncMock()

        input_data = StoreDiscoveryInput(
            connector_type="kubernetes",
            connector_id=uuid4(),
            entities=[
                TopologyEntityCreate(
                    name="nginx",
                    entity_type="Pod",
                    connector_type="kubernetes",
                    scope={"namespace": "prod"},
                    description="NGINX pod",
                ),
                TopologyEntityCreate(
                    name="prod",
                    entity_type="Namespace",
                    connector_type="kubernetes",
                    description="Production namespace",
                ),
            ],
            relationships=[
                TopologyRelationshipCreate(
                    from_entity_name="nginx",
                    to_entity_name="prod",
                    relationship_type="runs_on",  # Invalid: Pod cannot runs_on Namespace
                ),
            ],
        )

        result = await topology_service.store_discovery(input_data, tenant_id="test-tenant")

        assert result.stored is True
        assert result.entities_created == 2
        assert result.relationships_created == 0
        assert len(result.validation_errors) == 1
        assert "Invalid relationship: Pod --runs_on--> Namespace" in result.validation_errors[0]

    # =========================================================================
    # Deduplication Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_deduplication_by_canonical_id(self, topology_service, mock_session):
        """Test that storing the same Pod twice results in only one entity."""
        pod_id = uuid4()

        pod_entity = MagicMock()
        pod_entity.id = pod_id
        pod_entity.entity_type = "Pod"

        # First call creates, second call updates (returns is_new=False)
        call_count = [0]

        def mock_upsert_side_effect(entity, tenant_id, canonical_id):
            call_count[0] += 1
            is_new = call_count[0] == 1
            return (pod_entity, is_new)

        topology_service.repository.upsert_entity = AsyncMock(side_effect=mock_upsert_side_effect)
        topology_service.repository.store_embedding = AsyncMock()

        input_data = StoreDiscoveryInput(
            connector_type="kubernetes",
            connector_id=uuid4(),
            entities=[
                TopologyEntityCreate(
                    name="nginx",
                    entity_type="Pod",
                    connector_type="kubernetes",
                    scope={"namespace": "prod"},
                    description="NGINX pod - first discovery",
                ),
                TopologyEntityCreate(
                    name="nginx",
                    entity_type="Pod",
                    connector_type="kubernetes",
                    scope={"namespace": "prod"},
                    description="NGINX pod - second discovery",
                ),
            ],
        )

        result = await topology_service.store_discovery(input_data, tenant_id="test-tenant")

        assert result.stored is True
        assert result.entities_created == 1  # Only one new entity
        assert len(result.validation_errors) == 0

    @pytest.mark.asyncio
    async def test_scoped_canonical_id_different_namespaces(self, topology_service, mock_session):
        """Test that same name in different namespaces creates different entities."""
        pod1_id = uuid4()
        pod2_id = uuid4()

        pod1_entity = MagicMock()
        pod1_entity.id = pod1_id
        pod1_entity.entity_type = "Pod"

        pod2_entity = MagicMock()
        pod2_entity.id = pod2_id
        pod2_entity.entity_type = "Pod"

        call_count = [0]

        def mock_upsert_side_effect(entity, tenant_id, canonical_id):
            call_count[0] += 1
            # Both are new because different canonical_id
            result = pod1_entity if call_count[0] == 1 else pod2_entity
            return (result, True)

        topology_service.repository.upsert_entity = AsyncMock(side_effect=mock_upsert_side_effect)
        topology_service.repository.store_embedding = AsyncMock()

        input_data = StoreDiscoveryInput(
            connector_type="kubernetes",
            connector_id=uuid4(),
            entities=[
                TopologyEntityCreate(
                    name="nginx",
                    entity_type="Pod",
                    connector_type="kubernetes",
                    scope={"namespace": "prod"},  # prod/nginx
                    description="NGINX pod in prod",
                ),
                TopologyEntityCreate(
                    name="nginx",
                    entity_type="Pod",
                    connector_type="kubernetes",
                    scope={"namespace": "dev"},  # dev/nginx - different canonical_id
                    description="NGINX pod in dev",
                ),
            ],
        )

        result = await topology_service.store_discovery(input_data, tenant_id="test-tenant")

        assert result.stored is True
        assert result.entities_created == 2  # Two different entities
        assert len(result.validation_errors) == 0

        # Verify both upsert calls were made
        assert topology_service.repository.upsert_entity.call_count == 2

    # =========================================================================
    # Unknown Connector Type Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_unknown_connector_type_skips_validation(self, topology_service, mock_session):
        """Test that unknown connector types (rest, soap) skip schema validation."""
        mock_entity = MagicMock()
        mock_entity.id = uuid4()
        mock_entity.entity_type = "APIEndpoint"

        topology_service.repository.upsert_entity = AsyncMock(return_value=(mock_entity, True))
        topology_service.repository.store_embedding = AsyncMock()

        input_data = StoreDiscoveryInput(
            connector_type="rest",  # No schema defined for REST
            connector_id=uuid4(),
            entities=[
                TopologyEntityCreate(
                    name="users-api",
                    entity_type="APIEndpoint",  # Custom type - should be allowed
                    connector_type="rest",
                    description="Users API endpoint",
                ),
            ],
        )

        result = await topology_service.store_discovery(input_data, tenant_id="test-tenant")

        assert result.stored is True
        assert result.entities_created == 1
        assert len(result.validation_errors) == 0

    @pytest.mark.asyncio
    async def test_soap_connector_allows_any_entity_type(self, topology_service, mock_session):
        """Test that SOAP connector allows any entity type."""
        mock_entity = MagicMock()
        mock_entity.id = uuid4()
        mock_entity.entity_type = "SOAPService"

        topology_service.repository.upsert_entity = AsyncMock(return_value=(mock_entity, True))
        topology_service.repository.store_embedding = AsyncMock()

        input_data = StoreDiscoveryInput(
            connector_type="soap",
            connector_id=uuid4(),
            entities=[
                TopologyEntityCreate(
                    name="payment-service",
                    entity_type="SOAPService",
                    connector_type="soap",
                    description="Payment processing SOAP service",
                ),
            ],
        )

        result = await topology_service.store_discovery(input_data, tenant_id="test-tenant")

        assert result.stored is True
        assert result.entities_created == 1
        assert len(result.validation_errors) == 0

    # =========================================================================
    # Partial Success / Mixed Validation Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_validation_errors_with_partial_success(self, topology_service, mock_session):
        """Test that valid entities are stored even when some fail validation."""
        pod_id = uuid4()

        pod_entity = MagicMock()
        pod_entity.id = pod_id
        pod_entity.entity_type = "Pod"

        topology_service.repository.upsert_entity = AsyncMock(return_value=(pod_entity, True))
        topology_service.repository.store_embedding = AsyncMock()

        input_data = StoreDiscoveryInput(
            connector_type="kubernetes",
            connector_id=uuid4(),
            entities=[
                TopologyEntityCreate(
                    name="nginx",
                    entity_type="Pod",  # Valid
                    connector_type="kubernetes",
                    scope={"namespace": "prod"},
                    description="Valid pod",
                ),
                TopologyEntityCreate(
                    name="my-vm",
                    entity_type="VM",  # Invalid for K8s
                    connector_type="kubernetes",
                    description="Invalid entity",
                ),
                TopologyEntityCreate(
                    name="weird-thing",
                    entity_type="WeirdType",  # Also invalid
                    connector_type="kubernetes",
                    description="Another invalid entity",
                ),
            ],
        )

        result = await topology_service.store_discovery(input_data, tenant_id="test-tenant")

        assert result.stored is True
        assert result.entities_created == 1  # Only the valid Pod
        assert len(result.validation_errors) == 2  # Two invalid entities
        assert "validation error" in result.message.lower()

    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid_relationships(self, topology_service, mock_session):
        """Test storing with mix of valid and invalid relationships."""
        pod_id = uuid4()
        node_id = uuid4()
        namespace_id = uuid4()

        pod_entity = MagicMock()
        pod_entity.id = pod_id
        pod_entity.entity_type = "Pod"

        node_entity = MagicMock()
        node_entity.id = node_id
        node_entity.entity_type = "Node"

        namespace_entity = MagicMock()
        namespace_entity.id = namespace_id
        namespace_entity.entity_type = "Namespace"

        entity_counter = [0]
        entities = [pod_entity, node_entity, namespace_entity]

        def mock_upsert_side_effect(entity, tenant_id, canonical_id):
            result = entities[entity_counter[0] % 3]
            entity_counter[0] += 1
            return (result, True)

        topology_service.repository.upsert_entity = AsyncMock(side_effect=mock_upsert_side_effect)
        topology_service.repository.store_embedding = AsyncMock()
        topology_service.repository.get_relationship = AsyncMock(return_value=None)
        topology_service.repository.create_relationship = AsyncMock()

        input_data = StoreDiscoveryInput(
            connector_type="kubernetes",
            connector_id=uuid4(),
            entities=[
                TopologyEntityCreate(
                    name="nginx",
                    entity_type="Pod",
                    connector_type="kubernetes",
                    scope={"namespace": "prod"},
                    description="NGINX pod",
                ),
                TopologyEntityCreate(
                    name="worker-01",
                    entity_type="Node",
                    connector_type="kubernetes",
                    description="Worker node",
                ),
                TopologyEntityCreate(
                    name="prod",
                    entity_type="Namespace",
                    connector_type="kubernetes",
                    description="Production namespace",
                ),
            ],
            relationships=[
                TopologyRelationshipCreate(
                    from_entity_name="nginx",
                    to_entity_name="worker-01",
                    relationship_type="runs_on",  # Valid: Pod runs_on Node
                ),
                TopologyRelationshipCreate(
                    from_entity_name="nginx",
                    to_entity_name="prod",
                    relationship_type="runs_on",  # Invalid: Pod cannot runs_on Namespace
                ),
            ],
        )

        result = await topology_service.store_discovery(input_data, tenant_id="test-tenant")

        assert result.stored is True
        assert result.entities_created == 3
        assert result.relationships_created == 1  # Only the valid one
        assert len(result.validation_errors) == 1


class TestCanonicalIdGeneration:
    """Tests for canonical ID generation based on schema."""

    def test_k8s_pod_canonical_id(self):
        """Test K8s Pod canonical ID includes namespace."""
        schema = get_topology_schema("kubernetes")
        canonical_id = schema.build_canonical_id("Pod", {"namespace": "production"}, "web-server")
        assert canonical_id == "production/web-server"

    def test_k8s_node_canonical_id(self):
        """Test K8s Node canonical ID is just the name (unscoped)."""
        schema = get_topology_schema("kubernetes")
        canonical_id = schema.build_canonical_id("Node", {}, "worker-01")
        assert canonical_id == "worker-01"

    def test_vmware_vm_canonical_id(self):
        """Test VMware VM canonical ID uses moref."""
        schema = get_topology_schema("vmware")
        canonical_id = schema.build_canonical_id("VM", {"moref": "vm-12345"}, "web-server")
        assert canonical_id == "vm-12345"

    def test_vmware_host_canonical_id(self):
        """Test VMware Host canonical ID includes cluster."""
        schema = get_topology_schema("vmware")
        canonical_id = schema.build_canonical_id("Host", {"cluster": "prod-cluster"}, "esxi-01")
        assert canonical_id == "prod-cluster/esxi-01"

    def test_proxmox_vm_canonical_id(self):
        """Test Proxmox VM canonical ID uses node/vmid."""
        schema = get_topology_schema("proxmox")
        canonical_id = schema.build_canonical_id(
            "VM", {"node": "pve1", "vmid": "100"}, "web-server"
        )
        assert canonical_id == "pve1/100"


class TestSchemaValidation:
    """Tests for schema validation utilities."""

    def test_k8s_schema_has_required_entity_types(self):
        """Test K8s schema has all required entity types."""
        schema = get_topology_schema("kubernetes")

        required_types = {"Pod", "Deployment", "Service", "Namespace", "Node"}
        for entity_type in required_types:
            assert schema.is_valid_entity_type(entity_type), f"Missing {entity_type}"

    def test_k8s_schema_has_required_relationships(self):
        """Test K8s schema has all required relationships."""
        schema = get_topology_schema("kubernetes")

        # Key relationships
        assert schema.is_valid_relationship("Pod", "runs_on", "Node")
        assert schema.is_valid_relationship("Pod", "member_of", "Namespace")
        assert schema.is_valid_relationship("Deployment", "manages", "ReplicaSet")
        assert schema.is_valid_relationship("ReplicaSet", "manages", "Pod")
        assert schema.is_valid_relationship("Service", "routes_to", "Pod")

    def test_k8s_schema_rejects_invalid_relationships(self):
        """Test K8s schema rejects invalid relationships."""
        schema = get_topology_schema("kubernetes")

        # Pod cannot runs_on Namespace
        assert not schema.is_valid_relationship("Pod", "runs_on", "Namespace")

        # Node cannot be member_of Pod
        assert not schema.is_valid_relationship("Node", "member_of", "Pod")

        # Namespace cannot routes_to Pod
        assert not schema.is_valid_relationship("Namespace", "routes_to", "Pod")

    def test_vmware_schema_has_required_entity_types(self):
        """Test VMware schema has all required entity types."""
        schema = get_topology_schema("vmware")

        required_types = {"VM", "Host", "Cluster", "Datacenter", "Datastore"}
        for entity_type in required_types:
            assert schema.is_valid_entity_type(entity_type), f"Missing {entity_type}"

    def test_unknown_connector_returns_none(self):
        """Test that unknown connector types return None schema."""
        assert get_topology_schema("rest") is None
        assert get_topology_schema("soap") is None
        assert get_topology_schema("unknown") is None

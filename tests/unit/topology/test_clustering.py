# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for ClusteringService.

Tests:
- Eligibility checking (Node+VM eligible, Pod+VM not eligible)
- Duplicate prevention (same pair not suggested twice)
- Symmetric eligibility (if A can match B, check both schemas)
- Discovery result handling
- Match details generation

TASK-160 Phase 2: Proactive SAME_AS discovery.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from meho_app.modules.topology.clustering import (
    ClusteringService,
    DiscoveryResult,
    get_clustering_service,
    run_same_as_discovery,
)
from meho_app.modules.topology.models import TopologyEntityModel

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_session():
    """Create a mock database session."""
    return AsyncMock()


@pytest.fixture
def clustering_service(mock_session):
    """Create a ClusteringService with mocked session."""
    return ClusteringService(mock_session)


def create_mock_entity(
    entity_type: str,
    connector_type: str,
    name: str = "test-entity",
    connector_name: str = "test-connector",
) -> MagicMock:
    """Create a mock TopologyEntityModel."""
    entity = MagicMock(spec=TopologyEntityModel)
    entity.id = uuid4()
    entity.name = name
    entity.entity_type = entity_type
    entity.connector_type = connector_type
    entity.connector_name = connector_name
    entity.tenant_id = "test-tenant"
    return entity


# =============================================================================
# Eligibility Tests
# =============================================================================


class TestEligibilityChecking:
    """Tests for _is_eligible_pair method."""

    def test_kubernetes_node_and_vmware_vm_are_eligible(self, clustering_service):
        """K8s Node and VMware VM should be eligible for SAME_AS."""
        k8s_node = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="worker-01",
        )
        vmware_vm = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="k8s-worker-01",
        )

        assert clustering_service._is_eligible_pair(k8s_node, vmware_vm) is True

    def test_kubernetes_node_and_gcp_instance_are_eligible(self, clustering_service):
        """K8s Node and GCP Instance should be eligible for SAME_AS."""
        k8s_node = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="gke-cluster-pool-abc123",
        )
        gcp_instance = create_mock_entity(
            entity_type="Instance",
            connector_type="gcp",
            name="gke-cluster-pool-abc123",
        )

        assert clustering_service._is_eligible_pair(k8s_node, gcp_instance) is True

    def test_kubernetes_node_and_proxmox_vm_are_eligible(self, clustering_service):
        """K8s Node and Proxmox VM should be eligible for SAME_AS."""
        k8s_node = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="node-01",
        )
        proxmox_vm = create_mock_entity(
            entity_type="VM",
            connector_type="proxmox",
            name="k8s-node-01",
        )

        assert clustering_service._is_eligible_pair(k8s_node, proxmox_vm) is True

    def test_kubernetes_pod_and_vmware_vm_are_not_eligible(self, clustering_service):
        """K8s Pod and VMware VM should NOT be eligible (Pod is ephemeral)."""
        k8s_pod = create_mock_entity(
            entity_type="Pod",
            connector_type="kubernetes",
            name="nginx-abc123",
        )
        vmware_vm = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="nginx",
        )

        assert clustering_service._is_eligible_pair(k8s_pod, vmware_vm) is False

    def test_kubernetes_service_and_vmware_vm_are_not_eligible(self, clustering_service):
        """K8s Service and VMware VM should NOT be eligible."""
        k8s_service = create_mock_entity(
            entity_type="Service",
            connector_type="kubernetes",
            name="frontend",
        )
        vmware_vm = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="frontend",
        )

        assert clustering_service._is_eligible_pair(k8s_service, vmware_vm) is False

    def test_vmware_datastore_and_gcp_disk_are_not_eligible(self, clustering_service):
        """Storage entities should NOT be eligible for cross-connector SAME_AS."""
        vmware_datastore = create_mock_entity(
            entity_type="Datastore",
            connector_type="vmware",
            name="storage-01",
        )
        gcp_disk = create_mock_entity(
            entity_type="Disk",
            connector_type="gcp",
            name="storage-01",
        )

        assert clustering_service._is_eligible_pair(vmware_datastore, gcp_disk) is False

    def test_unknown_connector_type_is_not_eligible(self, clustering_service):
        """Unknown connector types should NOT be eligible."""
        unknown_entity = create_mock_entity(
            entity_type="Widget",
            connector_type="unknown_connector",
            name="widget-01",
        )
        vmware_vm = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="widget-01",
        )

        assert clustering_service._is_eligible_pair(unknown_entity, vmware_vm) is False

    def test_unknown_entity_type_is_not_eligible(self, clustering_service):
        """Unknown entity types should NOT be eligible."""
        k8s_unknown = create_mock_entity(
            entity_type="UnknownResource",
            connector_type="kubernetes",
            name="resource-01",
        )
        vmware_vm = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="resource-01",
        )

        assert clustering_service._is_eligible_pair(k8s_unknown, vmware_vm) is False


class TestSymmetricEligibility:
    """Tests for symmetric eligibility checking."""

    def test_eligibility_is_symmetric_node_vm(self, clustering_service):
        """Eligibility should work in both directions for Node ↔ VM."""
        k8s_node = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="worker-01",
        )
        vmware_vm = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="worker-01",
        )

        # Both directions should be eligible
        assert clustering_service._is_eligible_pair(k8s_node, vmware_vm) is True
        assert clustering_service._is_eligible_pair(vmware_vm, k8s_node) is True

    def test_eligibility_is_symmetric_node_instance(self, clustering_service):
        """Eligibility should work in both directions for Node ↔ Instance."""
        k8s_node = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="gke-pool-123",
        )
        gcp_instance = create_mock_entity(
            entity_type="Instance",
            connector_type="gcp",
            name="gke-pool-123",
        )

        # Both directions should be eligible
        assert clustering_service._is_eligible_pair(k8s_node, gcp_instance) is True
        assert clustering_service._is_eligible_pair(gcp_instance, k8s_node) is True

    def test_vmware_vm_and_gcp_instance_are_eligible(self, clustering_service):
        """VMware VM and GCP Instance should be eligible (both can match Node)."""
        vmware_vm = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="hybrid-vm",
        )
        gcp_instance = create_mock_entity(
            entity_type="Instance",
            connector_type="gcp",
            name="hybrid-vm",
        )

        # VMware VM can match Instance, GCP Instance can match VM
        assert clustering_service._is_eligible_pair(vmware_vm, gcp_instance) is True
        assert clustering_service._is_eligible_pair(gcp_instance, vmware_vm) is True


class TestProxmoxEligibility:
    """Tests for Proxmox-specific eligibility."""

    def test_proxmox_vm_and_kubernetes_node_are_eligible(self, clustering_service):
        """Proxmox VM and K8s Node should be eligible."""
        proxmox_vm = create_mock_entity(
            entity_type="VM",
            connector_type="proxmox",
            name="k8s-node",
        )
        k8s_node = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="k8s-node",
        )

        assert clustering_service._is_eligible_pair(proxmox_vm, k8s_node) is True

    def test_proxmox_container_and_kubernetes_node_are_eligible(self, clustering_service):
        """Proxmox Container (LXC) and K8s Node should be eligible."""
        proxmox_container = create_mock_entity(
            entity_type="Container",
            connector_type="proxmox",
            name="k8s-node",
        )
        k8s_node = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="k8s-node",
        )

        assert clustering_service._is_eligible_pair(proxmox_container, k8s_node) is True

    def test_proxmox_storage_is_not_eligible(self, clustering_service):
        """Proxmox Storage should NOT be eligible for cross-connector SAME_AS."""
        proxmox_storage = create_mock_entity(
            entity_type="Storage",
            connector_type="proxmox",
            name="local-storage",
        )
        vmware_datastore = create_mock_entity(
            entity_type="Datastore",
            connector_type="vmware",
            name="local-storage",
        )

        assert clustering_service._is_eligible_pair(proxmox_storage, vmware_datastore) is False


# =============================================================================
# Match Details Tests
# =============================================================================


class TestMatchDetailsGeneration:
    """Tests for _build_match_details method."""

    def test_basic_match_details(self, clustering_service):
        """Test basic match details generation."""
        entity_a = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="worker-01",
            connector_name="prod-k8s",
        )
        entity_b = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="k8s-worker-01",
            connector_name="vcenter-prod",
        )

        details = clustering_service._build_match_details(entity_a, entity_b, 0.85)

        assert "85.0%" in details
        assert "kubernetes.Node" in details
        assert "vmware.VM" in details
        assert "prod-k8s" in details
        assert "vcenter-prod" in details

    def test_match_details_without_connector_names(self, clustering_service):
        """Test match details when connector names are not available."""
        entity_a = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="worker-01",
        )
        entity_a.connector_name = None

        entity_b = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="worker-01",
        )
        entity_b.connector_name = None

        details = clustering_service._build_match_details(entity_a, entity_b, 0.75)

        assert "75.0%" in details
        assert "kubernetes.Node" in details
        assert "vmware.VM" in details
        # Should not have connector names section
        assert "Connectors:" not in details


# =============================================================================
# Discovery Result Tests
# =============================================================================


class TestDiscoveryResult:
    """Tests for DiscoveryResult dataclass."""

    def test_discovery_result_message(self):
        """Test DiscoveryResult message generation."""
        result = DiscoveryResult(
            suggestions_created=5,
            suggestions_skipped_existing=3,
            suggestions_skipped_ineligible=10,
            total_pairs_analyzed=50,
        )

        message = result.message

        assert "5 new suggestions" in message
        assert "3 existing" in message
        assert "10 ineligible" in message
        assert "50 pairs analyzed" in message

    def test_discovery_result_zero_values(self):
        """Test DiscoveryResult with zero values."""
        result = DiscoveryResult(
            suggestions_created=0,
            suggestions_skipped_existing=0,
            suggestions_skipped_ineligible=0,
            total_pairs_analyzed=0,
        )

        message = result.message

        assert "0 new suggestions" in message


# =============================================================================
# Discovery Flow Tests (with mocked repository)
# =============================================================================


class TestDiscoveryFlow:
    """Tests for discover_same_as_candidates flow."""

    @pytest.mark.asyncio
    async def test_empty_candidates_returns_zero_results(self, clustering_service):
        """Test that empty candidates returns zero results."""
        # Mock repository to return no candidates
        clustering_service.repository.find_cross_connector_similar_pairs = AsyncMock(
            return_value=[]
        )

        result = await clustering_service.discover_same_as_candidates(
            tenant_id="test-tenant",
            min_similarity=0.70,
            limit=50,
        )

        assert result.suggestions_created == 0
        assert result.suggestions_skipped_existing == 0
        assert result.suggestions_skipped_ineligible == 0
        assert result.total_pairs_analyzed == 0

    @pytest.mark.asyncio
    async def test_skips_existing_suggestions(self, clustering_service):
        """Test that existing suggestions are skipped."""
        entity_a_id = uuid4()
        entity_b_id = uuid4()

        # Create mock entities
        entity_a = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="worker-01",
        )
        entity_a.id = entity_a_id

        entity_b = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="worker-01",
        )
        entity_b.id = entity_b_id

        # Mock repository
        clustering_service.repository.find_cross_connector_similar_pairs = AsyncMock(
            return_value=[(entity_a_id, entity_b_id, 0.85)]
        )
        clustering_service.repository.get_entity_by_id = AsyncMock(
            side_effect=lambda eid: entity_a if eid == entity_a_id else entity_b
        )
        # Existing suggestion
        clustering_service.repository.get_existing_suggestion = AsyncMock(
            return_value=MagicMock(status="pending")
        )

        result = await clustering_service.discover_same_as_candidates(
            tenant_id="test-tenant",
            min_similarity=0.70,
            limit=50,
        )

        assert result.suggestions_created == 0
        assert result.suggestions_skipped_existing == 1
        assert result.total_pairs_analyzed == 1

    @pytest.mark.asyncio
    async def test_skips_existing_same_as_relationships(self, clustering_service):
        """Test that pairs with existing SAME_AS are skipped."""
        entity_a_id = uuid4()
        entity_b_id = uuid4()

        # Create mock entities
        entity_a = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="worker-01",
        )
        entity_a.id = entity_a_id

        entity_b = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="worker-01",
        )
        entity_b.id = entity_b_id

        # Mock repository
        clustering_service.repository.find_cross_connector_similar_pairs = AsyncMock(
            return_value=[(entity_a_id, entity_b_id, 0.85)]
        )
        clustering_service.repository.get_entity_by_id = AsyncMock(
            side_effect=lambda eid: entity_a if eid == entity_a_id else entity_b
        )
        # No existing suggestion, but existing SAME_AS
        clustering_service.repository.get_existing_suggestion = AsyncMock(return_value=None)
        clustering_service.repository.check_existing_same_as = AsyncMock(
            return_value=MagicMock()  # Non-None means exists
        )

        result = await clustering_service.discover_same_as_candidates(
            tenant_id="test-tenant",
            min_similarity=0.70,
            limit=50,
        )

        assert result.suggestions_created == 0
        assert result.suggestions_skipped_existing == 1

    @pytest.mark.asyncio
    async def test_skips_ineligible_pairs(self, clustering_service):
        """Test that ineligible pairs are skipped."""
        entity_a_id = uuid4()
        entity_b_id = uuid4()

        # Create mock entities - Pod ↔ VM is NOT eligible
        entity_a = create_mock_entity(
            entity_type="Pod",  # Pods cannot have SAME_AS
            connector_type="kubernetes",
            name="nginx-abc123",
        )
        entity_a.id = entity_a_id

        entity_b = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="nginx",
        )
        entity_b.id = entity_b_id

        # Mock repository
        clustering_service.repository.find_cross_connector_similar_pairs = AsyncMock(
            return_value=[(entity_a_id, entity_b_id, 0.85)]
        )
        clustering_service.repository.get_entity_by_id = AsyncMock(
            side_effect=lambda eid: entity_a if eid == entity_a_id else entity_b
        )

        result = await clustering_service.discover_same_as_candidates(
            tenant_id="test-tenant",
            min_similarity=0.70,
            limit=50,
        )

        assert result.suggestions_created == 0
        assert result.suggestions_skipped_ineligible == 1

    @pytest.mark.asyncio
    async def test_creates_new_suggestion(self, clustering_service):
        """Test that new suggestions are created for eligible pairs."""
        entity_a_id = uuid4()
        entity_b_id = uuid4()

        # Create mock entities - Node ↔ VM is eligible
        entity_a = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="worker-01",
        )
        entity_a.id = entity_a_id

        entity_b = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="k8s-worker-01",
        )
        entity_b.id = entity_b_id

        # Mock repository
        clustering_service.repository.find_cross_connector_similar_pairs = AsyncMock(
            return_value=[(entity_a_id, entity_b_id, 0.85)]
        )
        clustering_service.repository.get_entity_by_id = AsyncMock(
            side_effect=lambda eid: entity_a if eid == entity_a_id else entity_b
        )
        clustering_service.repository.get_existing_suggestion = AsyncMock(return_value=None)
        clustering_service.repository.check_existing_same_as = AsyncMock(return_value=None)
        clustering_service.repository.create_suggestion = AsyncMock(return_value=MagicMock())

        result = await clustering_service.discover_same_as_candidates(
            tenant_id="test-tenant",
            min_similarity=0.70,
            limit=50,
        )

        assert result.suggestions_created == 1
        assert result.suggestions_skipped_existing == 0
        assert result.suggestions_skipped_ineligible == 0

        # Verify create_suggestion was called with correct args
        clustering_service.repository.create_suggestion.assert_called_once()
        call_args = clustering_service.repository.create_suggestion.call_args
        suggestion_input = call_args.kwargs.get("suggestion") or call_args.args[0]
        assert suggestion_input.entity_a_id == entity_a_id
        assert suggestion_input.entity_b_id == entity_b_id
        assert suggestion_input.confidence == pytest.approx(0.85)
        assert suggestion_input.match_type == "embedding_similarity"

    @pytest.mark.asyncio
    async def test_respects_limit(self, clustering_service):
        """Test that the limit parameter is respected."""
        # Create multiple entity pairs
        pairs = []
        entities = {}
        for i in range(10):
            entity_a_id = uuid4()
            entity_b_id = uuid4()

            entity_a = create_mock_entity(
                entity_type="Node",
                connector_type="kubernetes",
                name=f"worker-{i:02d}",
            )
            entity_a.id = entity_a_id

            entity_b = create_mock_entity(
                entity_type="VM",
                connector_type="vmware",
                name=f"worker-{i:02d}",
            )
            entity_b.id = entity_b_id

            pairs.append((entity_a_id, entity_b_id, 0.80 + i * 0.01))
            entities[entity_a_id] = entity_a
            entities[entity_b_id] = entity_b

        # Mock repository
        clustering_service.repository.find_cross_connector_similar_pairs = AsyncMock(
            return_value=pairs
        )
        clustering_service.repository.get_entity_by_id = AsyncMock(
            side_effect=lambda eid: entities.get(eid)
        )
        clustering_service.repository.get_existing_suggestion = AsyncMock(return_value=None)
        clustering_service.repository.check_existing_same_as = AsyncMock(return_value=None)
        clustering_service.repository.create_suggestion = AsyncMock(return_value=MagicMock())

        # Set limit to 3
        result = await clustering_service.discover_same_as_candidates(
            tenant_id="test-tenant",
            min_similarity=0.70,
            limit=3,
        )

        assert result.suggestions_created == 3
        assert clustering_service.repository.create_suggestion.call_count == 3


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestHelperFunctions:
    """Tests for module-level helper functions."""

    @pytest.mark.asyncio
    async def test_run_same_as_discovery(self, mock_session):
        """Test run_same_as_discovery convenience function."""
        with patch.object(
            ClusteringService, "discover_same_as_candidates", new_callable=AsyncMock
        ) as mock_discover:
            mock_discover.return_value = DiscoveryResult(
                suggestions_created=5,
                suggestions_skipped_existing=2,
                suggestions_skipped_ineligible=3,
                total_pairs_analyzed=20,
            )

            result = await run_same_as_discovery(
                session=mock_session,
                tenant_id="test-tenant",
                min_similarity=0.75,
                limit=25,
            )

            assert result.suggestions_created == 5
            mock_discover.assert_called_once_with(
                tenant_id="test-tenant",
                min_similarity=0.75,
                limit=25,
            )

    def test_get_clustering_service(self, mock_session):
        """Test get_clustering_service factory function."""
        service = get_clustering_service(mock_session)

        assert isinstance(service, ClusteringService)
        assert service.session == mock_session

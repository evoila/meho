# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for SAME_AS clustering discovery.

Tests the full discovery flow:
1. Repository query for similar pairs
2. Eligibility filtering
3. Suggestion creation
4. API endpoint trigger

TASK-160 Phase 2: Proactive SAME_AS discovery.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from meho_app.api.auth import get_current_user
from meho_app.main import app
from meho_app.modules.topology.clustering import ClusteringService, DiscoveryResult
from meho_app.modules.topology.models import (
    TopologyEntityModel,
)

# Mock user context for tests
MOCK_USER = {
    "user_id": "test-user-123",
    "tenant_id": "test-tenant",
    "roles": ["admin"],
    "email": "test@example.com",
}


@pytest.fixture
def mock_user_context():
    """Mock authenticated user context."""
    from meho_app.core.auth_context import UserContext

    return UserContext(
        user_id=MOCK_USER["user_id"],
        tenant_id=MOCK_USER["tenant_id"],
        roles=MOCK_USER["roles"],
        email=MOCK_USER["email"],
    )


@pytest.fixture
def test_client(mock_user_context):
    """Create test client with overridden auth dependency."""
    app.dependency_overrides[get_current_user] = lambda: mock_user_context
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def create_mock_entity(
    entity_type: str,
    connector_type: str,
    name: str,
    tenant_id: str = "test-tenant",
    connector_name: str = "test-connector",
) -> TopologyEntityModel:
    """Create a mock TopologyEntityModel with proper fields."""
    return TopologyEntityModel(
        id=uuid4(),
        name=name,
        entity_type=entity_type,
        connector_type=connector_type,
        connector_id=uuid4(),
        connector_name=connector_name,
        canonical_id=name,
        scope={},
        description=f"{connector_type} {entity_type} {name}",
        raw_attributes={},
        discovered_at=datetime.now(tz=UTC),
        tenant_id=tenant_id,
    )


# =============================================================================
# Discovery API Endpoint Tests
# =============================================================================


class TestDiscoveryEndpoint:
    """Tests for POST /api/topology/suggestions/discover endpoint."""

    def test_discovery_endpoint_success(self, test_client):
        """Test successful discovery via API endpoint."""
        mock_result = DiscoveryResult(
            suggestions_created=5,
            suggestions_skipped_existing=2,
            suggestions_skipped_ineligible=3,
            total_pairs_analyzed=20,
        )

        with patch(
            "meho_app.modules.topology.clustering.ClusteringService.discover_same_as_candidates",
            new_callable=AsyncMock,
        ) as mock_discover:
            mock_discover.return_value = mock_result

            response = test_client.post(
                "/api/topology/suggestions/discover", params={"min_similarity": 0.75, "limit": 25}
            )

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["suggestions_created"] == 5
            assert data["suggestions_skipped_existing"] == 2
            assert data["suggestions_skipped_ineligible"] == 3
            assert data["total_pairs_analyzed"] == 20
            assert "5 new suggestions" in data["message"]

    def test_discovery_endpoint_no_results(self, test_client):
        """Test discovery when no similar pairs exist."""
        mock_result = DiscoveryResult(
            suggestions_created=0,
            suggestions_skipped_existing=0,
            suggestions_skipped_ineligible=0,
            total_pairs_analyzed=0,
        )

        with patch(
            "meho_app.modules.topology.clustering.ClusteringService.discover_same_as_candidates",
            new_callable=AsyncMock,
        ) as mock_discover:
            mock_discover.return_value = mock_result

            response = test_client.post("/api/topology/suggestions/discover")

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["suggestions_created"] == 0

    def test_discovery_endpoint_default_params(self, test_client):
        """Test discovery with default parameters."""
        mock_result = DiscoveryResult(
            suggestions_created=3,
            suggestions_skipped_existing=0,
            suggestions_skipped_ineligible=1,
            total_pairs_analyzed=10,
        )

        with patch(
            "meho_app.modules.topology.clustering.ClusteringService.discover_same_as_candidates",
            new_callable=AsyncMock,
        ) as mock_discover:
            mock_discover.return_value = mock_result

            response = test_client.post("/api/topology/suggestions/discover")

            assert response.status_code == 200

            # Verify default parameters were used
            mock_discover.assert_called_once()
            call_kwargs = mock_discover.call_args.kwargs
            assert call_kwargs["tenant_id"] == "test-tenant"
            assert call_kwargs["min_similarity"] == 0.70
            assert call_kwargs["limit"] == 50

    def test_discovery_endpoint_with_verification(self, test_client):
        """Test discovery with LLM verification enabled."""
        mock_result = DiscoveryResult(
            suggestions_created=2,
            suggestions_skipped_existing=0,
            suggestions_skipped_ineligible=0,
            total_pairs_analyzed=5,
        )

        mock_suggestion = MagicMock()
        mock_suggestion.id = uuid4()

        with patch(
            "meho_app.modules.topology.clustering.ClusteringService.discover_same_as_candidates",
            new_callable=AsyncMock,
        ) as mock_discover:
            mock_discover.return_value = mock_result

            with patch(
                "meho_app.modules.topology.repository.TopologyRepository.get_suggestions_needing_verification",
                new_callable=AsyncMock,
            ) as mock_get_suggestions:
                mock_get_suggestions.return_value = [mock_suggestion]

                with patch(
                    "meho_app.modules.topology.suggestion_verifier.SuggestionVerifier.process_and_resolve",
                    new_callable=AsyncMock,
                ) as mock_verify:
                    mock_verify.return_value = "approved"

                    response = test_client.post(
                        "/api/topology/suggestions/discover", params={"verify": True}
                    )

                    assert response.status_code == 200
                    data = response.json()
                    assert data["suggestions_created"] == 2

                    # Verify that verification was called
                    mock_verify.assert_called_once_with(mock_suggestion.id)

    def test_discovery_endpoint_error_handling(self, test_client):
        """Test discovery endpoint error handling."""
        with patch(
            "meho_app.modules.topology.clustering.ClusteringService.discover_same_as_candidates",
            new_callable=AsyncMock,
        ) as mock_discover:
            mock_discover.side_effect = Exception("Database connection failed")

            response = test_client.post("/api/topology/suggestions/discover")

            assert response.status_code == 500
            data = response.json()
            assert "Discovery failed" in data["detail"]


# =============================================================================
# Service Integration Tests
# =============================================================================


class TestClusteringServiceIntegration:
    """Integration tests for ClusteringService with mocked repository."""

    @pytest.mark.asyncio
    async def test_full_discovery_flow_with_eligible_pair(self):
        """Test full discovery flow with an eligible Node ↔ VM pair."""
        mock_session = AsyncMock()
        service = ClusteringService(mock_session)

        # Create mock entities
        k8s_node = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="worker-01",
            connector_name="prod-k8s",
        )
        vmware_vm = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="k8s-worker-01",
            connector_name="vcenter-prod",
        )

        entities = {k8s_node.id: k8s_node, vmware_vm.id: vmware_vm}

        # Mock repository methods
        service.repository.find_cross_connector_similar_pairs = AsyncMock(
            return_value=[(k8s_node.id, vmware_vm.id, 0.85)]
        )
        service.repository.get_entity_by_id = AsyncMock(side_effect=lambda eid: entities.get(eid))
        service.repository.get_existing_suggestion = AsyncMock(return_value=None)
        service.repository.check_existing_same_as = AsyncMock(return_value=None)
        service.repository.create_suggestion = AsyncMock(return_value=MagicMock())

        # Run discovery
        result = await service.discover_same_as_candidates(
            tenant_id="test-tenant",
            min_similarity=0.70,
            limit=10,
        )

        # Verify results
        assert result.suggestions_created == 1
        assert result.suggestions_skipped_existing == 0
        assert result.suggestions_skipped_ineligible == 0

        # Verify suggestion was created with correct data
        service.repository.create_suggestion.assert_called_once()
        call_kwargs = service.repository.create_suggestion.call_args.kwargs
        suggestion = (
            call_kwargs.get("suggestion") or service.repository.create_suggestion.call_args.args[0]
        )
        assert suggestion.match_type == "embedding_similarity"
        assert suggestion.confidence == 0.85

    @pytest.mark.asyncio
    async def test_full_discovery_flow_filters_ineligible_pods(self):
        """Test that Pod ↔ VM pairs are filtered out as ineligible."""
        mock_session = AsyncMock()
        service = ClusteringService(mock_session)

        # Create mock entities - Pod should NOT match VM
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

        entities = {k8s_pod.id: k8s_pod, vmware_vm.id: vmware_vm}

        # Mock repository methods
        service.repository.find_cross_connector_similar_pairs = AsyncMock(
            return_value=[(k8s_pod.id, vmware_vm.id, 0.90)]  # High similarity
        )
        service.repository.get_entity_by_id = AsyncMock(side_effect=lambda eid: entities.get(eid))

        # Run discovery
        result = await service.discover_same_as_candidates(
            tenant_id="test-tenant",
            min_similarity=0.70,
            limit=10,
        )

        # Verify Pod ↔ VM was filtered
        assert result.suggestions_created == 0
        assert result.suggestions_skipped_ineligible == 1

    @pytest.mark.asyncio
    async def test_full_discovery_flow_skips_existing_suggestions(self):
        """Test that existing suggestions are skipped."""
        mock_session = AsyncMock()
        service = ClusteringService(mock_session)

        # Create mock entities
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

        entities = {k8s_node.id: k8s_node, vmware_vm.id: vmware_vm}

        # Mock existing suggestion
        existing_suggestion = MagicMock()
        existing_suggestion.status = "pending"

        # Mock repository methods
        service.repository.find_cross_connector_similar_pairs = AsyncMock(
            return_value=[(k8s_node.id, vmware_vm.id, 0.85)]
        )
        service.repository.get_entity_by_id = AsyncMock(side_effect=lambda eid: entities.get(eid))
        service.repository.get_existing_suggestion = AsyncMock(return_value=existing_suggestion)

        # Run discovery
        result = await service.discover_same_as_candidates(
            tenant_id="test-tenant",
            min_similarity=0.70,
            limit=10,
        )

        # Verify existing suggestion was skipped
        assert result.suggestions_created == 0
        assert result.suggestions_skipped_existing == 1

    @pytest.mark.asyncio
    async def test_discovery_with_mixed_results(self):
        """Test discovery with a mix of eligible, ineligible, and existing pairs."""
        mock_session = AsyncMock()
        service = ClusteringService(mock_session)

        # Create mock entities
        k8s_node_1 = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="worker-01",
        )
        vmware_vm_1 = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="worker-01",
        )

        k8s_pod = create_mock_entity(
            entity_type="Pod",
            connector_type="kubernetes",
            name="nginx-pod",
        )
        vmware_vm_2 = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="nginx",
        )

        k8s_node_2 = create_mock_entity(
            entity_type="Node",
            connector_type="kubernetes",
            name="worker-02",
        )
        vmware_vm_3 = create_mock_entity(
            entity_type="VM",
            connector_type="vmware",
            name="worker-02",
        )

        entities = {
            k8s_node_1.id: k8s_node_1,
            vmware_vm_1.id: vmware_vm_1,
            k8s_pod.id: k8s_pod,
            vmware_vm_2.id: vmware_vm_2,
            k8s_node_2.id: k8s_node_2,
            vmware_vm_3.id: vmware_vm_3,
        }

        # Mock repository methods
        service.repository.find_cross_connector_similar_pairs = AsyncMock(
            return_value=[
                (k8s_node_1.id, vmware_vm_1.id, 0.90),  # Eligible, new
                (k8s_pod.id, vmware_vm_2.id, 0.85),  # Ineligible (Pod)
                (k8s_node_2.id, vmware_vm_3.id, 0.80),  # Eligible, but existing
            ]
        )
        service.repository.get_entity_by_id = AsyncMock(side_effect=lambda eid: entities.get(eid))

        # First pair: new, second pair: checked after ineligible, third pair: existing
        call_count = 0

        async def mock_get_existing(entity_a_id, entity_b_id):
            nonlocal call_count
            call_count += 1
            # Third pair has existing suggestion
            if entity_a_id == k8s_node_2.id or entity_b_id == k8s_node_2.id:
                return MagicMock(status="pending")
            return None

        service.repository.get_existing_suggestion = AsyncMock(side_effect=mock_get_existing)
        service.repository.check_existing_same_as = AsyncMock(return_value=None)
        service.repository.create_suggestion = AsyncMock(return_value=MagicMock())

        # Run discovery
        result = await service.discover_same_as_candidates(
            tenant_id="test-tenant",
            min_similarity=0.70,
            limit=10,
        )

        # Verify mixed results
        assert result.suggestions_created == 1  # Only first pair
        assert result.suggestions_skipped_ineligible == 1  # Pod pair
        assert result.suggestions_skipped_existing == 1  # Third pair
        assert result.total_pairs_analyzed == 3


# =============================================================================
# Repository Query Tests
# =============================================================================


class TestRepositoryCrossConnectorQuery:
    """Tests for find_cross_connector_similar_pairs repository method."""

    @pytest.mark.asyncio
    async def test_query_returns_empty_for_no_embeddings(self):
        """Test that query returns empty when no embeddings exist."""
        from meho_app.modules.topology.repository import TopologyRepository

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=MagicMock(__iter__=lambda self: iter([])))

        repo = TopologyRepository(mock_session)

        result = await repo.find_cross_connector_similar_pairs(
            tenant_id="test-tenant",
            min_similarity=0.70,
            limit=100,
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_query_filters_by_tenant(self):
        """Test that query includes tenant_id filter."""
        from meho_app.modules.topology.repository import TopologyRepository

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda self: iter([])
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = TopologyRepository(mock_session)

        await repo.find_cross_connector_similar_pairs(
            tenant_id="specific-tenant",
            min_similarity=0.70,
            limit=100,
        )

        # Verify execute was called with query containing tenant_id
        mock_session.execute.assert_called_once()
        call_args = mock_session.execute.call_args
        params = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs
        assert params["tenant_id"] == "specific-tenant"

    @pytest.mark.asyncio
    async def test_query_respects_similarity_threshold(self):
        """Test that query uses correct similarity threshold."""
        from meho_app.modules.topology.repository import TopologyRepository

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.__iter__ = lambda self: iter([])
        mock_session.execute = AsyncMock(return_value=mock_result)

        repo = TopologyRepository(mock_session)

        await repo.find_cross_connector_similar_pairs(
            tenant_id="test-tenant",
            min_similarity=0.80,  # 80% threshold
            limit=100,
        )

        # Verify execute was called with correct max_distance
        # similarity 0.80 → max_distance = 2 * (1 - 0.80) = 0.40
        mock_session.execute.assert_called_once()
        call_args = mock_session.execute.call_args
        params = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs
        assert params["max_distance"] == pytest.approx(0.40)

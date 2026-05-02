# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for TopologyRepository.get_same_as_entities().

Tests the repository method that returns confirmed SAME_AS entities
for use in agent context injection.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from meho_app.modules.topology.models import (
    TopologyEntityModel,
    TopologySameAsModel,
)
from meho_app.modules.topology.repository import TopologyRepository


class TestGetSameAsEntities:
    """Tests for TopologyRepository.get_same_as_entities()."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock async session."""
        session = MagicMock()
        session.execute = AsyncMock()
        session.flush = AsyncMock()
        session.refresh = AsyncMock()
        return session

    @pytest.fixture
    def sample_entity_a(self):
        """Create a sample K8s Node entity."""
        entity = MagicMock(spec=TopologyEntityModel)
        entity.id = uuid4()
        entity.name = "node-01"
        entity.entity_type = "Node"
        entity.connector_type = "kubernetes"
        entity.connector_id = uuid4()
        entity.connector_name = "k8s-prod"
        entity.stale_at = None
        entity.tenant_id = "test-tenant"
        entity.raw_attributes = {"hostname": "node-01.cluster.local"}
        return entity

    @pytest.fixture
    def sample_entity_b(self):
        """Create a sample VMware VM entity (correlated with node-01)."""
        entity = MagicMock(spec=TopologyEntityModel)
        entity.id = uuid4()
        entity.name = "k8s-worker-01"
        entity.entity_type = "VM"
        entity.connector_type = "vmware"
        entity.connector_id = uuid4()
        entity.connector_name = "vcenter-prod"
        entity.stale_at = None
        entity.tenant_id = "test-tenant"
        entity.raw_attributes = {"guest.hostName": "node-01"}
        return entity

    @pytest.fixture
    def sample_same_as(self, sample_entity_a, sample_entity_b):
        """Create a sample SAME_AS relationship."""
        same_as = MagicMock(spec=TopologySameAsModel)
        same_as.id = uuid4()
        same_as.entity_a_id = sample_entity_a.id
        same_as.entity_b_id = sample_entity_b.id
        same_as.entity_a = sample_entity_a
        same_as.entity_b = sample_entity_b
        same_as.similarity_score = 0.92
        same_as.verified_via = ["IP match (10.0.0.5)", "hostname"]
        same_as.discovered_at = datetime.now(tz=UTC)
        return same_as

    @pytest.mark.asyncio
    async def test_get_same_as_entities_returns_correlated_entity(
        self,
        mock_session,
        sample_entity_a,
        sample_entity_b,
        sample_same_as,
    ):
        """Test that get_same_as_entities returns the correlated entity."""
        # Arrange
        repo = TopologyRepository(mock_session)

        # Mock get_same_as_for_entity to return the SAME_AS relationship
        with patch.object(repo, "get_same_as_for_entity", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [sample_same_as]

            # Act
            result = await repo.get_same_as_entities(sample_entity_a.id)

            # Assert
            assert len(result) == 1
            entity, verified_via = result[0]
            assert entity.id == sample_entity_b.id
            assert entity.name == "k8s-worker-01"
            assert verified_via == ["IP match (10.0.0.5)", "hostname"]

    @pytest.mark.asyncio
    async def test_get_same_as_entities_bidirectional(
        self,
        mock_session,
        sample_entity_a,
        sample_entity_b,
        sample_same_as,
    ):
        """Test that querying from either entity returns the other."""
        repo = TopologyRepository(mock_session)

        with patch.object(repo, "get_same_as_for_entity", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [sample_same_as]

            # Query from entity B - should return entity A
            result = await repo.get_same_as_entities(sample_entity_b.id)

            assert len(result) == 1
            entity, _ = result[0]
            assert entity.id == sample_entity_a.id

    @pytest.mark.asyncio
    async def test_get_same_as_entities_empty_when_none(self, mock_session):
        """Test that empty list is returned when no SAME_AS exists."""
        repo = TopologyRepository(mock_session)

        with patch.object(repo, "get_same_as_for_entity", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []

            result = await repo.get_same_as_entities(uuid4())

            assert result == []

    @pytest.mark.asyncio
    async def test_get_same_as_entities_excludes_stale(
        self,
        mock_session,
        sample_entity_a,
        sample_same_as,
    ):
        """Test that stale entities are excluded from results."""
        # Mark entity_b as stale
        stale_entity = MagicMock(spec=TopologyEntityModel)
        stale_entity.id = uuid4()
        stale_entity.stale_at = datetime.now(tz=UTC)

        sample_same_as.entity_b = stale_entity
        sample_same_as.entity_b_id = stale_entity.id

        repo = TopologyRepository(mock_session)

        with patch.object(repo, "get_same_as_for_entity", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [sample_same_as]

            result = await repo.get_same_as_entities(sample_entity_a.id)

            # Stale entity should be excluded
            assert result == []

    @pytest.mark.asyncio
    async def test_get_same_as_entities_multiple_correlations(
        self,
        mock_session,
        sample_entity_a,
    ):
        """Test handling multiple SAME_AS relationships."""
        # Create two correlated entities
        entity_b = MagicMock(spec=TopologyEntityModel)
        entity_b.id = uuid4()
        entity_b.name = "vm-worker-01"
        entity_b.entity_type = "VM"
        entity_b.stale_at = None

        entity_c = MagicMock(spec=TopologyEntityModel)
        entity_c.id = uuid4()
        entity_c.name = "gce-instance-01"
        entity_c.entity_type = "Instance"
        entity_c.stale_at = None

        same_as_b = MagicMock(spec=TopologySameAsModel)
        same_as_b.entity_a_id = sample_entity_a.id
        same_as_b.entity_b_id = entity_b.id
        same_as_b.entity_a = sample_entity_a
        same_as_b.entity_b = entity_b
        same_as_b.verified_via = ["IP match"]

        same_as_c = MagicMock(spec=TopologySameAsModel)
        same_as_c.entity_a_id = sample_entity_a.id
        same_as_c.entity_b_id = entity_c.id
        same_as_c.entity_a = sample_entity_a
        same_as_c.entity_b = entity_c
        same_as_c.verified_via = ["hostname"]

        repo = TopologyRepository(mock_session)

        with patch.object(repo, "get_same_as_for_entity", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [same_as_b, same_as_c]

            result = await repo.get_same_as_entities(sample_entity_a.id)

            assert len(result) == 2
            names = [e.name for e, _ in result]
            assert "vm-worker-01" in names
            assert "gce-instance-01" in names

    @pytest.mark.asyncio
    async def test_get_same_as_entities_handles_empty_verified_via(
        self,
        mock_session,
        sample_entity_a,
        sample_entity_b,
        sample_same_as,
    ):
        """Test handling of SAME_AS with no verified_via."""
        sample_same_as.verified_via = None

        repo = TopologyRepository(mock_session)

        with patch.object(repo, "get_same_as_for_entity", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [sample_same_as]

            result = await repo.get_same_as_entities(sample_entity_a.id)

            assert len(result) == 1
            _, verified_via = result[0]
            assert verified_via == []


class TestCorrelatedEntitySchema:
    """Tests for CorrelatedEntity schema."""

    def test_correlated_entity_creation(self):
        """Test creating a CorrelatedEntity schema."""
        from datetime import datetime

        from meho_app.modules.topology.schemas import CorrelatedEntity, TopologyEntity

        # Create a TopologyEntity
        entity = TopologyEntity(
            id=uuid4(),
            name="k8s-worker-01",
            entity_type="VM",
            connector_type="vmware",
            connector_id=uuid4(),
            connector_name="vcenter-prod",
            scope={},
            canonical_id="k8s-worker-01",
            description="VMware VM running K8s worker",
            raw_attributes={},
            discovered_at=datetime.now(tz=UTC),
            tenant_id="test-tenant",
        )

        correlated = CorrelatedEntity(
            entity=entity,
            connector_type="vmware",
            connector_name="vcenter-prod",
            verified_via=["IP match", "hostname"],
        )

        assert correlated.entity.name == "k8s-worker-01"
        assert correlated.connector_type == "vmware"
        assert len(correlated.verified_via) == 2

    def test_correlated_entity_without_connector_name(self):
        """Test CorrelatedEntity with no connector_name."""
        from datetime import datetime

        from meho_app.modules.topology.schemas import CorrelatedEntity, TopologyEntity

        entity = TopologyEntity(
            id=uuid4(),
            name="instance-01",
            entity_type="Instance",
            connector_type="gcp",
            connector_id=uuid4(),
            scope={},
            canonical_id="instance-01",
            description="GCP instance",
            raw_attributes={},
            discovered_at=datetime.now(tz=UTC),
            tenant_id="test-tenant",
        )

        correlated = CorrelatedEntity(
            entity=entity,
            connector_type="gcp",
            connector_name=None,
            verified_via=[],
        )

        assert correlated.connector_name is None
        assert correlated.verified_via == []


class TestLookupTopologyResultWithSameAs:
    """Tests for LookupTopologyResult including same_as_entities."""

    def test_lookup_result_includes_same_as(self):
        """Test that LookupTopologyResult includes same_as_entities field."""
        from datetime import datetime

        from meho_app.modules.topology.schemas import (
            CorrelatedEntity,
            LookupTopologyResult,
            TopologyEntity,
        )

        entity = TopologyEntity(
            id=uuid4(),
            name="node-01",
            entity_type="Node",
            connector_type="kubernetes",
            connector_id=uuid4(),
            scope={},
            canonical_id="node-01",
            description="K8s node",
            raw_attributes={},
            discovered_at=datetime.now(tz=UTC),
            tenant_id="test-tenant",
        )

        correlated_entity = TopologyEntity(
            id=uuid4(),
            name="vm-01",
            entity_type="VM",
            connector_type="vmware",
            connector_id=uuid4(),
            scope={},
            canonical_id="vm-01",
            description="VMware VM",
            raw_attributes={},
            discovered_at=datetime.now(tz=UTC),
            tenant_id="test-tenant",
        )

        result = LookupTopologyResult(
            found=True,
            entity=entity,
            topology_chain=[],
            connectors_traversed=["k8s-prod"],
            same_as_entities=[
                CorrelatedEntity(
                    entity=correlated_entity,
                    connector_type="vmware",
                    connector_name="vcenter-prod",
                    verified_via=["IP match"],
                )
            ],
            possibly_related=[],
        )

        assert result.found is True
        assert len(result.same_as_entities) == 1
        assert result.same_as_entities[0].entity.name == "vm-01"

    def test_lookup_result_empty_same_as_by_default(self):
        """Test that same_as_entities defaults to empty list."""
        from meho_app.modules.topology.schemas import LookupTopologyResult

        result = LookupTopologyResult(
            found=False,
            suggestions=["Try again"],
        )

        assert result.same_as_entities == []

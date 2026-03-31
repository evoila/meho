# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for agent SAME_AS context injection.

Tests the full flow from lookup to context formatting:
1. TopologyService.lookup() returns same_as_entities
2. TopologyLookupNode includes SAME_AS in result
3. _format_context() includes SAME_AS section

TASK-160 Phase 4: Agent Integration.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from meho_app.modules.topology.models import (
    TopologyEntityModel,
)
from meho_app.modules.topology.schemas import (
    CorrelatedEntity,
    LookupTopologyInput,
    LookupTopologyResult,
    TopologyEntity,
)
from meho_app.modules.topology.service import TopologyService


def create_test_entity(
    name: str,
    entity_type: str,
    connector_type: str,
    connector_name: str = "test-connector",
) -> TopologyEntityModel:
    """Create a test topology entity model."""
    entity = MagicMock(spec=TopologyEntityModel)
    entity.id = uuid4()
    entity.name = name
    entity.entity_type = entity_type
    entity.connector_type = connector_type
    entity.connector_id = uuid4()
    entity.connector_name = connector_name
    entity.canonical_id = name
    entity.scope = {}
    entity.description = f"{connector_type} {entity_type}: {name}"
    entity.raw_attributes = {"hostname": name}
    entity.discovered_at = datetime.now(UTC)
    entity.last_verified_at = None
    entity.stale_at = None
    entity.tenant_id = "test-tenant"
    return entity


# =============================================================================
# TopologyService.lookup() Integration Tests
# =============================================================================


class TestServiceLookupWithSameAs:
    """Tests for TopologyService.lookup() including same_as_entities."""

    @pytest.mark.asyncio
    async def test_lookup_returns_same_as_entities(self):
        """Test that lookup() returns confirmed SAME_AS entities."""
        # Arrange
        mock_session = MagicMock()
        mock_session.execute = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        service = TopologyService(mock_session)

        # Create test entities
        k8s_node = create_test_entity("node-01", "Node", "kubernetes", "k8s-prod")
        vmware_vm = create_test_entity("k8s-worker-01", "VM", "vmware", "vcenter-prod")

        # Mock repository methods
        with patch.object(
            service.repository, "get_entity_by_name", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = k8s_node

            with patch.object(
                service.repository, "traverse_topology", new_callable=AsyncMock
            ) as mock_traverse:
                mock_traverse.return_value = []

                with patch.object(
                    service.repository, "get_same_as_entities", new_callable=AsyncMock
                ) as mock_same_as:
                    # Return the correlated VM entity
                    mock_same_as.return_value = [(vmware_vm, ["IP match", "hostname"])]

                    with patch.object(
                        service.correlation_service, "find_possibly_related", new_callable=AsyncMock
                    ) as mock_related:
                        mock_related.return_value = []

                        # Act
                        result = await service.lookup(
                            LookupTopologyInput(query="node-01"),
                            tenant_id="test-tenant",
                        )

                        # Assert
                        assert result.found is True
                        assert result.entity is not None
                        assert len(result.same_as_entities) == 1

                        correlated = result.same_as_entities[0]
                        assert correlated.entity.name == "k8s-worker-01"
                        assert correlated.connector_type == "vmware"
                        assert correlated.connector_name == "vcenter-prod"
                        assert "IP match" in correlated.verified_via

    @pytest.mark.asyncio
    async def test_lookup_empty_same_as_when_none(self):
        """Test that lookup() returns empty same_as when no correlations exist."""
        mock_session = MagicMock()
        mock_session.execute = AsyncMock()

        service = TopologyService(mock_session)

        k8s_node = create_test_entity("node-01", "Node", "kubernetes", "k8s-prod")

        with patch.object(
            service.repository, "get_entity_by_name", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = k8s_node

            with patch.object(
                service.repository, "traverse_topology", new_callable=AsyncMock
            ) as mock_traverse:
                mock_traverse.return_value = []

                with patch.object(
                    service.repository, "get_same_as_entities", new_callable=AsyncMock
                ) as mock_same_as:
                    mock_same_as.return_value = []  # No SAME_AS entities

                    with patch.object(
                        service.correlation_service, "find_possibly_related", new_callable=AsyncMock
                    ) as mock_related:
                        mock_related.return_value = []

                        result = await service.lookup(
                            LookupTopologyInput(query="node-01"),
                            tenant_id="test-tenant",
                        )

                        assert result.found is True
                        assert result.same_as_entities == []

    @pytest.mark.asyncio
    async def test_lookup_handles_same_as_error_gracefully(self):
        """Test that lookup() handles get_same_as_entities errors gracefully."""
        mock_session = MagicMock()
        mock_session.execute = AsyncMock()

        service = TopologyService(mock_session)

        k8s_node = create_test_entity("node-01", "Node", "kubernetes", "k8s-prod")

        with patch.object(
            service.repository, "get_entity_by_name", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = k8s_node

            with patch.object(
                service.repository, "traverse_topology", new_callable=AsyncMock
            ) as mock_traverse:
                mock_traverse.return_value = []

                with patch.object(
                    service.repository, "get_same_as_entities", new_callable=AsyncMock
                ) as mock_same_as:
                    mock_same_as.side_effect = Exception("Database error")

                    with patch.object(
                        service.correlation_service, "find_possibly_related", new_callable=AsyncMock
                    ) as mock_related:
                        mock_related.return_value = []

                        # Should not raise, just return empty same_as
                        result = await service.lookup(
                            LookupTopologyInput(query="node-01"),
                            tenant_id="test-tenant",
                        )

                        assert result.found is True
                        assert result.same_as_entities == []


# =============================================================================
# Context Formatting Tests
# =============================================================================


class TestContextFormattingWithSameAs:
    """Tests for _format_context() including SAME_AS section."""

    def test_format_context_includes_same_as_section(self):
        """Test that context includes SAME_AS section when present."""
        from meho_app.modules.agents.shared.graph.nodes.topology_lookup_node import (
            TopologyLookupNode,
        )

        node = TopologyLookupNode()

        # Create mock entity
        mock_entity = MagicMock()
        mock_entity.name = "node-01"
        mock_entity.entity_type = "Node"
        mock_entity.connector_id = uuid4()
        mock_entity.connector_name = "k8s-prod"
        mock_entity.description = "Kubernetes worker node"
        mock_entity.raw_attributes = {"hostname": "node-01.cluster.local"}

        # Create mock correlated entity
        mock_correlated = MagicMock()
        mock_correlated.entity = MagicMock()
        mock_correlated.entity.name = "k8s-worker-01"
        mock_correlated.entity.entity_type = "VM"
        mock_correlated.entity.raw_attributes = {"vmid": "vm-123"}
        mock_correlated.connector_type = "vmware"
        mock_correlated.connector_name = "vcenter-prod"
        mock_correlated.verified_via = ["IP match (10.0.0.5)", "hostname"]

        context_parts = [
            {
                "entity": mock_entity,
                "chain": [],
                "same_as": [mock_correlated],
                "related": [],
            }
        ]

        # Format context
        result = node._format_context(context_parts)

        # Assert SAME_AS section is included
        assert "Confirmed Cross-Connector Correlations (SAME_AS)" in result
        assert "k8s-worker-01" in result
        assert "VM" in result
        assert "vcenter-prod" in result
        assert "IP match (10.0.0.5)" in result
        assert "query BOTH connectors for comprehensive diagnostics" in result

    def test_format_context_no_same_as_section_when_empty(self):
        """Test that context omits SAME_AS section when no correlations."""
        from meho_app.modules.agents.shared.graph.nodes.topology_lookup_node import (
            TopologyLookupNode,
        )

        node = TopologyLookupNode()

        mock_entity = MagicMock()
        mock_entity.name = "node-01"
        mock_entity.entity_type = "Node"
        mock_entity.connector_id = uuid4()
        mock_entity.connector_name = "k8s-prod"
        mock_entity.description = "Kubernetes worker node"
        mock_entity.raw_attributes = {}

        context_parts = [
            {
                "entity": mock_entity,
                "chain": [],
                "same_as": [],  # No SAME_AS
                "related": [],
            }
        ]

        result = node._format_context(context_parts)

        # Should NOT include SAME_AS section
        assert "Confirmed Cross-Connector Correlations" not in result
        assert "Query BOTH connectors" not in result

    def test_format_context_shows_key_identifiers_for_correlated(self):
        """Test that context shows key identifiers for correlated entities."""
        from meho_app.modules.agents.shared.graph.nodes.topology_lookup_node import (
            TopologyLookupNode,
        )

        node = TopologyLookupNode()

        mock_entity = MagicMock()
        mock_entity.name = "node-01"
        mock_entity.entity_type = "Node"
        mock_entity.connector_id = uuid4()
        mock_entity.connector_name = "k8s-prod"
        mock_entity.description = "Kubernetes worker node"
        mock_entity.raw_attributes = {}

        mock_correlated = MagicMock()
        mock_correlated.entity = MagicMock()
        mock_correlated.entity.name = "k8s-worker-01"
        mock_correlated.entity.entity_type = "VM"
        mock_correlated.entity.raw_attributes = {"vmid": "vm-123", "node": "esxi-01"}
        mock_correlated.connector_type = "vmware"
        mock_correlated.connector_name = "vcenter-prod"
        mock_correlated.verified_via = ["hostname"]

        context_parts = [
            {
                "entity": mock_entity,
                "chain": [],
                "same_as": [mock_correlated],
                "related": [],
            }
        ]

        result = node._format_context(context_parts)

        # Should include key identifiers
        assert "vmid=vm-123" in result
        assert "node=esxi-01" in result


# =============================================================================
# Tool Node Output Tests
# =============================================================================


class TestToolNodeSameAsOutput:
    """Tests for LookupTopologyNode output formatting with SAME_AS."""

    @pytest.mark.asyncio
    async def test_lookup_topology_node_includes_same_as(self):
        """Test that LookupTopologyNode output includes SAME_AS section."""
        from meho_app.modules.topology.tool_nodes import LookupTopologyNode

        # Create test result with SAME_AS
        result = LookupTopologyResult(
            found=True,
            entity=TopologyEntity(
                id=uuid4(),
                name="node-01",
                entity_type="Node",
                connector_type="kubernetes",
                connector_id=uuid4(),
                connector_name="k8s-prod",
                scope={},
                canonical_id="node-01",
                description="K8s node",
                raw_attributes={},
                discovered_at=datetime.now(UTC),
                tenant_id="test-tenant",
            ),
            topology_chain=[],
            connectors_traversed=["k8s-prod"],
            same_as_entities=[
                CorrelatedEntity(
                    entity=TopologyEntity(
                        id=uuid4(),
                        name="vm-worker-01",
                        entity_type="VM",
                        connector_type="vmware",
                        connector_id=uuid4(),
                        connector_name="vcenter-prod",
                        scope={},
                        canonical_id="vm-worker-01",
                        description="VMware VM",
                        raw_attributes={},
                        discovered_at=datetime.now(UTC),
                        tenant_id="test-tenant",
                    ),
                    connector_type="vmware",
                    connector_name="vcenter-prod",
                    verified_via=["IP match", "hostname"],
                ),
            ],
            possibly_related=[],
        )

        # Mock the service lookup
        mock_session = MagicMock()
        mock_deps = MagicMock()
        mock_deps.meho_deps = MagicMock()
        mock_deps.meho_deps.db_session = mock_session
        mock_deps.meho_deps.tenant_id = "test-tenant"

        node = LookupTopologyNode(query="node-01")

        with patch("meho_app.modules.topology.service.TopologyService") as mock_service_cls:
            mock_service = AsyncMock()
            mock_service.lookup = AsyncMock(return_value=result)
            mock_service_cls.return_value = mock_service

            output = await node._execute_lookup(mock_deps)

            # Assert output includes SAME_AS section
            assert "CONFIRMED SAME_AS" in output
            assert "vm-worker-01" in output
            assert "VM" in output
            assert "vcenter-prod" in output
            assert "IP match" in output
            assert "Query BOTH connectors" in output


# =============================================================================
# End-to-End Flow Tests
# =============================================================================


class TestSameAsEndToEndFlow:
    """End-to-end tests for SAME_AS context injection into agent."""

    @pytest.mark.asyncio
    async def test_full_lookup_to_context_flow(self):
        """Test the full flow from lookup to context formatting."""
        from meho_app.modules.agents.shared.graph.nodes.topology_lookup_node import (
            TopologyLookupNode,
        )

        # Create a full result with SAME_AS
        k8s_entity = TopologyEntity(
            id=uuid4(),
            name="node-01",
            entity_type="Node",
            connector_type="kubernetes",
            connector_id=uuid4(),
            connector_name="k8s-prod",
            scope={},
            canonical_id="node-01",
            description="Kubernetes worker node running pods",
            raw_attributes={"hostname": "node-01.k8s.local", "ip": "10.0.0.5"},
            discovered_at=datetime.now(UTC),
            tenant_id="test-tenant",
        )

        vmware_entity = TopologyEntity(
            id=uuid4(),
            name="k8s-worker-01",
            entity_type="VM",
            connector_type="vmware",
            connector_id=uuid4(),
            connector_name="vcenter-prod",
            scope={},
            canonical_id="k8s-worker-01",
            description="VMware VM hosting K8s worker",
            raw_attributes={"vmid": "vm-123", "hostname": "k8s-worker-01"},
            discovered_at=datetime.now(UTC),
            tenant_id="test-tenant",
        )

        correlated = CorrelatedEntity(
            entity=vmware_entity,
            connector_type="vmware",
            connector_name="vcenter-prod",
            verified_via=["IP match (10.0.0.5)", "hostname pattern"],
        )

        node = TopologyLookupNode()

        context_parts = [
            {
                "entity": k8s_entity,
                "chain": [],
                "same_as": [correlated],
                "related": [],
            }
        ]

        context = node._format_context(context_parts)

        # Verify context has all expected information
        assert "node-01 (Node)" in context
        assert "k8s-prod" in context

        # SAME_AS section
        assert "Confirmed Cross-Connector Correlations (SAME_AS)" in context
        assert "same physical resource" in context
        assert "k8s-worker-01" in context
        assert "(VM)" in context
        assert "vcenter-prod" in context
        assert "IP match (10.0.0.5)" in context
        assert "query BOTH connectors for comprehensive diagnostics" in context

        # Key identifiers for the correlated entity
        assert "vmid=vm-123" in context

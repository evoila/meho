# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for TopologyLookupNode context formatting.

Tests the schema context injection for topology lookup results (TASK-158):
- Navigation hints from schema are included
- Common queries from schema are included
- Unknown connector types are handled gracefully
"""

from datetime import UTC, datetime
from uuid import uuid4

from meho_app.modules.agents.shared.graph.nodes.topology_lookup_node import TopologyLookupNode
from meho_app.modules.topology.schemas import TopologyEntity

# =============================================================================
# Test Fixtures
# =============================================================================


def make_entity(
    name: str,
    entity_type: str,
    connector_type: str,
    description: str = "Test entity",
    raw_attributes: dict | None = None,
) -> TopologyEntity:
    """Create a TopologyEntity for testing."""
    return TopologyEntity(
        id=uuid4(),
        name=name,
        entity_type=entity_type,
        connector_type=connector_type,
        connector_id=uuid4(),
        connector_name="test-connector",
        scope={},
        canonical_id=f"{connector_type}/{name}",
        description=description,
        raw_attributes=raw_attributes or {},
        discovered_at=datetime.now(tz=UTC),
        tenant_id="test-tenant",
    )


# =============================================================================
# _format_context Tests
# =============================================================================


class TestFormatContextSchemaHints:
    """Tests for schema navigation hints in _format_context()."""

    def test_format_context_includes_navigation_hints_for_k8s_pod(self):
        """Verify K8s Pod includes navigation hints from schema."""
        node = TopologyLookupNode()

        entity = make_entity(
            name="nginx-pod",
            entity_type="Pod",
            connector_type="kubernetes",
            description="K8s Pod nginx-pod in default namespace",
        )

        context_parts = [
            {
                "entity": entity,
                "chain": [],
                "same_as": [],
                "related": [],
            }
        ]

        result = node._format_context(context_parts)

        # Should include navigation hints header
        assert "**How to navigate:**" in result
        # Should include hints from kubernetes.py Pod definition
        assert "managed_by" in result or "runs_on" in result

    def test_format_context_includes_common_queries_for_k8s_pod(self):
        """Verify K8s Pod includes common queries from schema."""
        node = TopologyLookupNode()

        entity = make_entity(
            name="nginx-pod",
            entity_type="Pod",
            connector_type="kubernetes",
            description="K8s Pod nginx-pod in default namespace",
        )

        context_parts = [
            {
                "entity": entity,
                "chain": [],
                "same_as": [],
                "related": [],
            }
        ]

        result = node._format_context(context_parts)

        # Should include common queries header
        assert "**Common questions you can answer:**" in result
        # Should include queries from kubernetes.py Pod definition
        assert "node" in result.lower() or "deployment" in result.lower()

    def test_format_context_includes_hints_for_proxmox_vm(self):
        """Verify Proxmox VM includes navigation hints from schema."""
        node = TopologyLookupNode()

        entity = make_entity(
            name="DEV-gameflow-db",
            entity_type="VM",
            connector_type="proxmox",
            description="Proxmox VM DEV-gameflow-db (VMID: 100)",
            raw_attributes={"vmid": 100, "node": "pve-node-01"},
        )

        context_parts = [
            {
                "entity": entity,
                "chain": [],
                "same_as": [],
                "related": [],
            }
        ]

        result = node._format_context(context_parts)

        # Should include navigation hints
        assert "**How to navigate:**" in result
        # Proxmox VM hints mention runs_on relationship
        assert "runs_on" in result

        # Should include common queries
        assert "**Common questions you can answer:**" in result

    def test_format_context_includes_hints_for_vmware_vm(self):
        """Verify VMware VM includes navigation hints from schema."""
        node = TopologyLookupNode()

        entity = make_entity(
            name="web-server-01",
            entity_type="VM",
            connector_type="vmware",
            description="VMware VM web-server-01",
        )

        context_parts = [
            {
                "entity": entity,
                "chain": [],
                "same_as": [],
                "related": [],
            }
        ]

        result = node._format_context(context_parts)

        # Should include navigation hints
        assert "**How to navigate:**" in result
        # VMware VM hints mention runs_on and uses_storage
        assert "runs_on" in result or "uses_storage" in result

        # Should include common queries
        assert "**Common questions you can answer:**" in result

    def test_format_context_includes_hints_for_gcp_instance(self):
        """Verify GCP Instance includes navigation hints from schema."""
        node = TopologyLookupNode()

        entity = make_entity(
            name="gcp-vm-01",
            entity_type="Instance",
            connector_type="gcp",
            description="GCP Compute Engine instance",
        )

        context_parts = [
            {
                "entity": entity,
                "chain": [],
                "same_as": [],
                "related": [],
            }
        ]

        result = node._format_context(context_parts)

        # Should include navigation hints
        assert "**How to navigate:**" in result
        # GCP Instance hints mention disk and network
        assert "disk" in result.lower() or "network" in result.lower()

        # Should include common queries
        assert "**Common questions you can answer:**" in result


class TestFormatContextGracefulHandling:
    """Tests for graceful handling of edge cases."""

    def test_format_context_handles_unknown_connector_type(self):
        """Verify REST/SOAP connectors without schemas don't break."""
        node = TopologyLookupNode()

        entity = make_entity(
            name="api-endpoint",
            entity_type="Endpoint",
            connector_type="rest",  # No schema for REST
            description="REST API endpoint",
        )

        context_parts = [
            {
                "entity": entity,
                "chain": [],
                "same_as": [],
                "related": [],
            }
        ]

        result = node._format_context(context_parts)

        # Should still format without error
        assert "api-endpoint" in result
        assert "Endpoint" in result
        # Should NOT include navigation hints (no schema)
        assert "**How to navigate:**" not in result
        assert "**Common questions you can answer:**" not in result

    def test_format_context_handles_unknown_entity_type_in_known_schema(self):
        """Verify unknown entity types in known schemas don't break."""
        node = TopologyLookupNode()

        # Create entity with valid connector but invalid entity type
        entity = make_entity(
            name="custom-resource",
            entity_type="CustomResourceDefinition",  # Not in kubernetes schema
            connector_type="kubernetes",
            description="Custom K8s resource",
        )

        context_parts = [
            {
                "entity": entity,
                "chain": [],
                "same_as": [],
                "related": [],
            }
        ]

        result = node._format_context(context_parts)

        # Should still format without error
        assert "custom-resource" in result
        # Should NOT include navigation hints (entity type not in schema)
        assert "**How to navigate:**" not in result

    def test_format_context_handles_soap_connector(self):
        """Verify SOAP connectors without schemas don't break."""
        node = TopologyLookupNode()

        entity = make_entity(
            name="soap-service",
            entity_type="Service",
            connector_type="soap",  # No schema for SOAP
            description="SOAP web service",
        )

        context_parts = [
            {
                "entity": entity,
                "chain": [],
                "same_as": [],
                "related": [],
            }
        ]

        result = node._format_context(context_parts)

        # Should still format without error
        assert "soap-service" in result
        # Should NOT include navigation hints (no schema)
        assert "**How to navigate:**" not in result


class TestAddSchemaHintsMethod:
    """Direct tests for _add_schema_hints helper method."""

    def test_add_schema_hints_appends_to_lines(self):
        """Verify hints are appended to the lines list."""
        node = TopologyLookupNode()

        entity = make_entity(
            name="test-deployment",
            entity_type="Deployment",
            connector_type="kubernetes",
            description="K8s Deployment",
        )

        lines: list[str] = ["Initial line"]
        node._add_schema_hints(lines, entity)

        # Should have added lines
        assert len(lines) > 1
        # Should include navigation header
        assert any("How to navigate" in line for line in lines)

    def test_add_schema_hints_no_op_for_missing_connector_type(self):
        """Verify no-op when connector_type is empty."""
        node = TopologyLookupNode()

        # Create entity with empty connector_type (edge case)
        entity = make_entity(
            name="test",
            entity_type="Pod",
            connector_type="",  # Empty string
            description="Test",
        )

        lines: list[str] = ["Initial line"]
        node._add_schema_hints(lines, entity)

        # Should not have added anything
        assert lines == ["Initial line"]


class TestFormatContextPreservesExistingBehavior:
    """Tests to verify existing functionality is preserved."""

    def test_basic_entity_info_still_included(self):
        """Verify basic entity info is still in the output."""
        node = TopologyLookupNode()

        entity = make_entity(
            name="nginx-pod",
            entity_type="Pod",
            connector_type="kubernetes",
            description="K8s Pod nginx-pod in default namespace",
        )

        context_parts = [
            {
                "entity": entity,
                "chain": [],
                "same_as": [],
                "related": [],
            }
        ]

        result = node._format_context(context_parts)

        # Basic info should still be present
        assert "## Known Topology" in result
        assert "nginx-pod (Pod)" in result
        assert "**Managed by connector**" in result
        assert "**Description**" in result

    def test_multiple_entities_all_get_hints(self):
        """Verify multiple entities each get their schema hints."""
        node = TopologyLookupNode()

        pod_entity = make_entity(
            name="nginx-pod",
            entity_type="Pod",
            connector_type="kubernetes",
            description="K8s Pod",
        )

        vm_entity = make_entity(
            name="web-vm",
            entity_type="VM",
            connector_type="proxmox",
            description="Proxmox VM",
        )

        context_parts = [
            {"entity": pod_entity, "chain": [], "same_as": [], "related": []},
            {"entity": vm_entity, "chain": [], "same_as": [], "related": []},
        ]

        result = node._format_context(context_parts)

        # Both entities should be present
        assert "nginx-pod (Pod)" in result
        assert "web-vm (VM)" in result

        # Count how many times navigation hints appear (should be 2)
        hint_count = result.count("**How to navigate:**")
        assert hint_count == 2, f"Expected 2 hint sections, found {hint_count}"

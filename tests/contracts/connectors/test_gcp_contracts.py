# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Contract tests for GCP Connector (TASK-102)

Validates that the GCP connector properly implements the BaseConnector interface
and that operation definitions follow the expected contract.
"""

import pytest

from meho_app.modules.connectors.base import (
    BaseConnector,
    OperationDefinition,
    TypeDefinition,
)
from meho_app.modules.connectors.gcp import (
    GCP_OPERATIONS,
    GCP_TYPES,
    GCPConnector,
)


class TestGCPConnectorContract:
    """Contract tests for GCPConnector class."""

    def test_inherits_base_connector(self):
        """Verify GCPConnector inherits from BaseConnector."""
        assert issubclass(GCPConnector, BaseConnector)

    def test_has_required_methods(self):
        """Verify all required methods exist."""
        required_methods = [
            "connect",
            "disconnect",
            "test_connection",
            "execute",
            "get_operations",
            "get_types",
        ]

        for method in required_methods:
            assert hasattr(GCPConnector, method), f"Missing method: {method}"
            assert callable(getattr(GCPConnector, method)), f"{method} is not callable"

    def test_init_signature(self):
        """Verify constructor accepts required parameters."""
        # Should not raise
        connector = GCPConnector(
            connector_id="test-id",
            config={"project_id": "test-project"},
            credentials={},
        )

        assert connector.connector_id == "test-id"

    def test_is_connected_property(self):
        """Verify is_connected property exists."""
        connector = GCPConnector(
            connector_id="test-id",
            config={"project_id": "test-project"},
            credentials={},
        )

        assert hasattr(connector, "is_connected")
        assert isinstance(connector.is_connected, bool)
        assert connector.is_connected is False  # Not connected by default


class TestGCPOperationsContract:
    """Contract tests for GCP operation definitions."""

    def test_operations_is_list(self):
        """Verify GCP_OPERATIONS is a list."""
        assert isinstance(GCP_OPERATIONS, list)

    def test_operations_not_empty(self):
        """Verify operations list is not empty."""
        assert len(GCP_OPERATIONS) > 0

    def test_all_operations_are_operation_definitions(self):
        """Verify all items are OperationDefinition instances."""
        for op in GCP_OPERATIONS:
            assert isinstance(op, OperationDefinition), f"Not OperationDefinition: {op}"

    def test_operations_have_required_fields(self):
        """Verify all operations have required fields."""
        for op in GCP_OPERATIONS:
            assert op.operation_id, f"Missing operation_id: {op}"
            assert op.name, f"Missing name: {op.operation_id}"
            assert op.description, f"Missing description: {op.operation_id}"
            assert op.category, f"Missing category: {op.operation_id}"

    def test_operation_ids_are_unique(self):
        """Verify all operation IDs are unique."""
        op_ids = [op.operation_id for op in GCP_OPERATIONS]
        assert len(op_ids) == len(set(op_ids)), "Duplicate operation IDs found"

    def test_operation_ids_are_valid_identifiers(self):
        """Verify operation IDs are valid Python identifiers."""
        import re

        pattern = re.compile(r"^[a-z][a-z0-9_]*$")

        for op in GCP_OPERATIONS:
            assert pattern.match(op.operation_id), f"Invalid operation_id format: {op.operation_id}"

    def test_operations_have_valid_categories(self):
        """Verify operations have valid categories."""
        valid_categories = {
            "compute",
            "storage",
            "containers",
            "networking",
            "monitoring",
            "ci_cd",
            "registry",
        }

        for op in GCP_OPERATIONS:
            assert op.category in valid_categories, (
                f"Invalid category '{op.category}' for {op.operation_id}"
            )

    def test_parameters_are_properly_structured(self):
        """Verify operation parameters follow expected structure."""
        for op in GCP_OPERATIONS:
            for param in op.parameters:
                assert isinstance(param, dict), f"Param not dict in {op.operation_id}"
                assert "name" in param, f"Param missing 'name' in {op.operation_id}"
                assert "type" in param, f"Param missing 'type' in {op.operation_id}"

    def test_required_operations_exist(self):
        """Verify essential operations are defined."""
        op_ids = {op.operation_id for op in GCP_OPERATIONS}

        required_ops = [
            # Compute
            "list_instances",
            "get_instance",
            "start_instance",
            "stop_instance",
            # GKE
            "list_clusters",
            "get_cluster",
            # Networking
            "list_networks",
            "list_firewalls",
            # Monitoring
            "list_metric_descriptors",
            "get_time_series",
        ]

        for op_id in required_ops:
            assert op_id in op_ids, f"Missing required operation: {op_id}"


class TestGCPTypesContract:
    """Contract tests for GCP type definitions."""

    def test_types_is_list(self):
        """Verify GCP_TYPES is a list."""
        assert isinstance(GCP_TYPES, list)

    def test_types_not_empty(self):
        """Verify types list is not empty."""
        assert len(GCP_TYPES) > 0

    def test_all_types_are_type_definitions(self):
        """Verify all items are TypeDefinition instances."""
        for t in GCP_TYPES:
            assert isinstance(t, TypeDefinition), f"Not TypeDefinition: {t}"

    def test_types_have_required_fields(self):
        """Verify all types have required fields."""
        for t in GCP_TYPES:
            assert t.type_name, "Missing type_name"
            assert t.description, f"Missing description: {t.type_name}"
            assert t.category, f"Missing category: {t.type_name}"

    def test_type_names_are_unique(self):
        """Verify all type names are unique."""
        type_names = [t.type_name for t in GCP_TYPES]
        assert len(type_names) == len(set(type_names)), "Duplicate type names found"

    def test_required_types_exist(self):
        """Verify essential types are defined."""
        type_names = {t.type_name for t in GCP_TYPES}

        required_types = [
            "Instance",
            "Disk",
            "Snapshot",
            "GKECluster",
            "NodePool",
            "VPCNetwork",
            "Subnetwork",
            "Firewall",
        ]

        for type_name in required_types:
            assert type_name in type_names, f"Missing required type: {type_name}"


class TestGCPConnectorPoolIntegration:
    """Contract tests for connector pool integration."""

    @pytest.mark.asyncio
    async def test_connector_registered_in_pool(self):
        """Verify GCP connector can be instantiated via pool."""
        from meho_app.modules.connectors.pool import get_connector_instance

        connector = await get_connector_instance(
            connector_type="gcp",
            connector_id="test-id",
            config={"project_id": "test-project"},
            credentials={},
        )
        assert isinstance(connector, GCPConnector)

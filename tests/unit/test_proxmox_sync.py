# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for Proxmox Operations Sync (TASK-100)

Tests the auto-sync functionality for Proxmox operations.
"""

from meho_app.modules.connectors.proxmox.operations import (
    PROXMOX_OPERATIONS,
    PROXMOX_OPERATIONS_VERSION,
)
from meho_app.modules.connectors.proxmox.sync import (
    _format_proxmox_operation_as_text,
    _generate_proxmox_search_keywords,
)


class TestProxmoxSyncFormatting:
    """Test Proxmox operation formatting for search."""

    def test_format_operation_as_text(self):
        """Test formatting an operation as searchable text."""
        from meho_app.modules.connectors.base import OperationDefinition

        op = OperationDefinition(
            operation_id="list_vms",
            name="List Virtual Machines",
            description="Get all QEMU VMs across all nodes",
            category="compute",
            parameters=[
                {
                    "name": "node",
                    "type": "string",
                    "required": False,
                    "description": "Filter to specific node",
                },
            ],
            example="list_vms()",
        )

        text = _format_proxmox_operation_as_text(op, "Test Proxmox")

        assert "list_vms" in text
        assert "List Virtual Machines" in text
        assert "Test Proxmox" in text
        assert "compute" in text
        assert "node" in text

    def test_generate_search_keywords_vm(self):
        """Test keyword generation for VM operations."""
        from meho_app.modules.connectors.base import OperationDefinition

        op = OperationDefinition(
            operation_id="list_vms",
            name="List Virtual Machines",
            description="Get all QEMU VMs",
            category="compute",
            parameters=[],
        )

        keywords = _generate_proxmox_search_keywords(op)

        assert "vm" in keywords
        assert "list" in keywords
        assert "compute" in keywords

    def test_generate_search_keywords_container(self):
        """Test keyword generation for container operations."""
        from meho_app.modules.connectors.base import OperationDefinition

        op = OperationDefinition(
            operation_id="list_containers",
            name="List Containers",
            description="Get all LXC containers",
            category="compute",
            parameters=[],
        )

        keywords = _generate_proxmox_search_keywords(op)

        assert "container" in keywords or "lxc" in keywords
        assert "list" in keywords

    def test_generate_search_keywords_snapshot(self):
        """Test keyword generation for snapshot operations."""
        from meho_app.modules.connectors.base import OperationDefinition

        op = OperationDefinition(
            operation_id="create_vm_snapshot",
            name="Create VM Snapshot",
            description="Create a snapshot of a VM",
            category="compute",
            parameters=[],
        )

        keywords = _generate_proxmox_search_keywords(op)

        assert "snapshot" in keywords or "snap" in keywords
        assert "create" in keywords


class TestProxmoxOperationsVersion:
    """Test operations version handling."""

    def test_operations_version_format(self):
        """Test that operations version follows expected format."""
        # Version should be YYYY.MM.DD.revision format
        parts = PROXMOX_OPERATIONS_VERSION.split(".")
        assert len(parts) == 4
        assert int(parts[0]) >= 2024  # Year
        assert 1 <= int(parts[1]) <= 12  # Month
        assert 1 <= int(parts[2]) <= 31  # Day
        assert int(parts[3]) >= 1  # Revision

    def test_all_operations_have_required_fields(self):
        """Test that all operations have required fields for sync."""
        for op in PROXMOX_OPERATIONS:
            assert op.operation_id, "Operation missing operation_id"
            assert op.name, f"Operation {op.operation_id} missing name"
            assert op.description, f"Operation {op.operation_id} missing description"
            assert op.category, f"Operation {op.operation_id} missing category"

    def test_operation_ids_unique(self):
        """Test that all operation IDs are unique."""
        op_ids = [op.operation_id for op in PROXMOX_OPERATIONS]
        assert len(op_ids) == len(set(op_ids)), "Duplicate operation IDs found"

    def test_operations_categories_valid(self):
        """Test that operation categories are from expected set."""
        valid_categories = {"compute", "nodes", "storage"}
        for op in PROXMOX_OPERATIONS:
            assert op.category in valid_categories, f"Invalid category: {op.category}"

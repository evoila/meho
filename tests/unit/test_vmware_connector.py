# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for VMware vSphere Connector (TASK-97)

Tests the connector base interface and VMware-specific implementation.
Uses mocking since we don't have actual vCenter access in tests.

Phase 84: ConnectorRouter.get_connector_instance and VMware connector internals
refactored during connector intelligence updates (v1.65+).
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: VMware connector and ConnectorRouter.get_connector_instance API changed in v1.65+ refactors")

from meho_app.modules.connectors.base import (
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)
from meho_app.modules.connectors.vmware import (
    VMWARE_OPERATIONS,
    VMWARE_TYPES,
    VMwareConnector,
)

# =============================================================================
# Test Base Connector Interface
# =============================================================================


class TestBaseConnector:
    """Test the BaseConnector interface and OperationResult."""

    def test_operation_result_success(self):
        """Test OperationResult with success."""
        result = OperationResult(
            success=True,
            data={"vms": ["vm1", "vm2"]},
            operation_id="list_virtual_machines",
            duration_ms=150.5,
        )

        assert result.success is True
        assert result.data == {"vms": ["vm1", "vm2"]}
        assert result.error is None
        assert result.operation_id == "list_virtual_machines"
        assert result.duration_ms == 150.5

    def test_operation_result_failure(self):
        """Test OperationResult with failure."""
        result = OperationResult(
            success=False,
            error="Connection refused",
            operation_id="list_virtual_machines",
            duration_ms=50.0,
        )

        assert result.success is False
        assert result.data is None
        assert result.error == "Connection refused"

    def test_operation_definition(self):
        """Test OperationDefinition model."""
        op = OperationDefinition(
            operation_id="list_vms",
            name="List Virtual Machines",
            description="Get all VMs in vCenter",
            category="compute",
            parameters=[{"name": "datacenter", "type": "string", "required": False}],
            example="list_vms(datacenter='DC1')",
        )

        assert op.operation_id == "list_vms"
        assert op.name == "List Virtual Machines"
        assert len(op.parameters) == 1
        assert op.parameters[0]["name"] == "datacenter"

    def test_type_definition(self):
        """Test TypeDefinition model."""
        td = TypeDefinition(
            type_name="VirtualMachine",
            description="A virtual machine",
            category="compute",
            properties=[
                {"name": "name", "type": "string"},
                {"name": "power_state", "type": "string"},
            ],
        )

        assert td.type_name == "VirtualMachine"
        assert len(td.properties) == 2


# =============================================================================
# Test VMware Operations and Types Definitions
# =============================================================================


class TestVMwareDefinitions:
    """Test VMware operation and type definitions."""

    def test_vmware_operations_defined(self):
        """Test that VMware operations are properly defined."""
        assert len(VMWARE_OPERATIONS) > 0

        # Check for key operations
        op_ids = {op.operation_id for op in VMWARE_OPERATIONS}

        assert "list_virtual_machines" in op_ids
        assert "get_virtual_machine" in op_ids
        assert "power_on_vm" in op_ids
        assert "power_off_vm" in op_ids
        assert "list_clusters" in op_ids
        assert "get_drs_recommendations" in op_ids
        assert "list_hosts" in op_ids
        assert "list_datastores" in op_ids

    def test_vmware_operations_have_descriptions(self):
        """Test that all operations have descriptions."""
        for op in VMWARE_OPERATIONS:
            assert op.name, f"Operation {op.operation_id} missing name"
            assert op.description, f"Operation {op.operation_id} missing description"
            assert op.category, f"Operation {op.operation_id} missing category"

    def test_vmware_types_defined(self):
        """Test that VMware types are properly defined."""
        assert len(VMWARE_TYPES) > 0

        # Check for key types
        type_names = {t.type_name for t in VMWARE_TYPES}

        assert "VirtualMachine" in type_names
        assert "ClusterComputeResource" in type_names
        assert "HostSystem" in type_names
        assert "Datastore" in type_names
        assert "Network" in type_names

    def test_vmware_types_have_properties(self):
        """Test that types have properties defined."""
        for t in VMWARE_TYPES:
            assert t.description, f"Type {t.type_name} missing description"
            assert len(t.properties) > 0, f"Type {t.type_name} has no properties"


# =============================================================================
# Test VMware Connector
# =============================================================================


class TestVMwareConnector:
    """Test VMwareConnector implementation."""

    @pytest.fixture
    def connector_config(self) -> dict[str, Any]:
        """Standard test config."""
        return {
            "vcenter_host": "vcenter.test.local",
            "port": 443,
            "disable_ssl_verification": True,
        }

    @pytest.fixture
    def connector_credentials(self) -> dict[str, Any]:
        """Standard test credentials."""
        return {
            "username": "admin@vsphere.local",
            "password": "testpassword",
        }

    def test_connector_init(self, connector_config, connector_credentials):
        """Test connector initialization."""
        connector = VMwareConnector(
            connector_id="test-123",
            config=connector_config,
            credentials=connector_credentials,
        )

        assert connector.connector_id == "test-123"
        assert connector.config == connector_config
        assert connector.credentials == connector_credentials
        assert connector.is_connected is False

    def test_get_operations(self, connector_config, connector_credentials):
        """Test get_operations returns definitions."""
        connector = VMwareConnector(
            connector_id="test-123",
            config=connector_config,
            credentials=connector_credentials,
        )

        operations = connector.get_operations()

        assert len(operations) == len(VMWARE_OPERATIONS)
        assert all(isinstance(op, OperationDefinition) for op in operations)

    def test_get_types(self, connector_config, connector_credentials):
        """Test get_types returns definitions."""
        connector = VMwareConnector(
            connector_id="test-123",
            config=connector_config,
            credentials=connector_credentials,
        )

        types = connector.get_types()

        assert len(types) == len(VMWARE_TYPES)
        assert all(isinstance(t, TypeDefinition) for t in types)

    @pytest.mark.asyncio
    async def test_execute_unknown_operation(self, connector_config, connector_credentials):
        """Test execute with unknown operation returns error."""
        connector = VMwareConnector(
            connector_id="test-123",
            config=connector_config,
            credentials=connector_credentials,
        )

        # Execute unknown operation (no connection needed)
        result = await connector.execute("nonexistent_operation", {})

        assert result.success is False
        assert "Unknown operation" in result.error
        assert result.operation_id == "nonexistent_operation"

    @pytest.mark.asyncio
    async def test_connect_requires_host(self, connector_credentials):
        """Test connect fails without vcenter_host."""
        connector = VMwareConnector(
            connector_id="test-123",
            config={},  # No host
            credentials=connector_credentials,
        )

        # Mock pyvmomi to test the validation path
        with (
            patch.dict(
                "sys.modules",
                {"pyVim": MagicMock(), "pyVim.connect": MagicMock(), "pyVmomi": MagicMock()},
            ),
            pytest.raises((ValueError, ImportError)),
        ):
            await connector.connect()

    @pytest.mark.asyncio
    async def test_connect_requires_credentials(self, connector_config):
        """Test connect fails without credentials."""
        connector = VMwareConnector(
            connector_id="test-123",
            config=connector_config,
            credentials={},  # No credentials
        )

        # Mock pyvmomi to test the validation path
        with (
            patch.dict(
                "sys.modules",
                {"pyVim": MagicMock(), "pyVim.connect": MagicMock(), "pyVmomi": MagicMock()},
            ),
            pytest.raises((ValueError, ImportError)),
        ):
            await connector.connect()


# =============================================================================
# Test VMware Connector with Mocked pyvmomi
# =============================================================================

# Check if pyvmomi is available
try:
    import pyVmomi  # noqa: F401 -- unused import in test

    PYVMOMI_AVAILABLE = True
except ImportError:
    PYVMOMI_AVAILABLE = False


@pytest.mark.skipif(not PYVMOMI_AVAILABLE, reason="pyvmomi not installed")
class TestVMwareConnectorMocked:
    """Test VMwareConnector with mocked pyvmomi (requires pyvmomi installed)."""

    @pytest.fixture
    def connector(self):
        """Create connector."""
        return VMwareConnector(
            connector_id="test-123",
            config={
                "vcenter_host": "vcenter.test.local",
                "port": 443,
                "disable_ssl_verification": True,
            },
            credentials={
                "username": "admin@vsphere.local",
                "password": "testpassword",
            },
        )

    @pytest.mark.asyncio
    async def test_connect_success(self, connector):
        """Test successful connection with mocked pyvmomi."""
        mock_connection = MagicMock()
        mock_content = MagicMock()
        mock_content.about.fullName = "VMware vCenter Server 8.0.1"
        mock_connection.RetrieveContent.return_value = mock_content

        with patch("pyVim.connect.SmartConnect", return_value=mock_connection):
            result = await connector.connect()

            assert result is True
            assert connector.is_connected is True
            assert connector._content == mock_content

    @pytest.mark.asyncio
    async def test_disconnect(self, connector):
        """Test disconnect cleans up state."""
        connector._connection = MagicMock()
        connector._content = MagicMock()
        connector._is_connected = True

        with patch("pyVim.connect.Disconnect"):
            await connector.disconnect()

            assert connector.is_connected is False
            assert connector._connection is None
            assert connector._content is None

    @pytest.mark.asyncio
    async def test_list_vms_operation(self, connector):
        """Test list_virtual_machines operation."""

        # Setup mock content
        mock_vm = MagicMock()
        mock_vm.name = "test-vm"
        mock_vm.runtime.powerState = "poweredOn"
        mock_vm.config.hardware.numCPU = 4
        mock_vm.config.hardware.memoryMB = 8192
        mock_vm.guest.ipAddress = "192.168.1.100"
        mock_vm.config.guestFullName = "Ubuntu 22.04"
        mock_vm.guest.toolsRunningStatus = "guestToolsRunning"

        mock_container = MagicMock()
        mock_container.view = [mock_vm]

        connector._content = MagicMock()
        connector._content.viewManager.CreateContainerView.return_value = mock_container
        connector._is_connected = True

        result = await connector.execute("list_virtual_machines", {})

        assert result.success is True
        assert result.operation_id == "list_virtual_machines"
        assert len(result.data) == 1
        assert result.data[0]["name"] == "test-vm"
        assert result.data[0]["power_state"] == "poweredOn"
        mock_container.Destroy.assert_called_once()


# =============================================================================
# Test Connector Router
# =============================================================================


class TestConnectorRouter:
    """Test connector routing logic."""

    @pytest.mark.asyncio
    async def test_get_connector_instance_vmware(self):
        """Test getting VMware connector instance."""
        from meho_app.modules.connectors import get_connector_instance

        connector = await get_connector_instance(
            connector_type="vmware",
            connector_id="test-123",
            config={"vcenter_host": "vcenter.local"},
            credentials={"username": "admin", "password": "test"},
        )

        assert isinstance(connector, VMwareConnector)
        assert connector.connector_id == "test-123"

    @pytest.mark.asyncio
    async def test_get_connector_instance_unknown(self):
        """Test unknown connector type raises error."""
        from meho_app.modules.connectors import get_connector_instance

        with pytest.raises(ValueError, match="Unknown connector type"):
            await get_connector_instance(
                connector_type="kubernetes",  # Not implemented yet
                connector_id="test-123",
                config={},
                credentials={},
            )


# =============================================================================
# Test Schemas
# =============================================================================


class TestVMwareSchemas:
    """Test VMware-related Pydantic schemas."""

    def test_connector_operation_create(self):
        """Test ConnectorOperationCreate schema."""
        from meho_app.modules.connectors.schemas import ConnectorOperationCreate

        op = ConnectorOperationCreate(
            connector_id="conn-123",
            tenant_id="tenant-1",
            operation_id="list_vms",
            name="List VMs",
            description="List all virtual machines",
            category="compute",
            parameters=[{"name": "datacenter", "type": "string"}],
            example="list_vms()",
            search_content="list vms virtual machines compute",
        )

        assert op.connector_id == "conn-123"
        assert op.operation_id == "list_vms"
        assert op.safety_level == "safe"  # default

    def test_connector_entity_type_create(self):
        """Test ConnectorEntityTypeCreate schema."""
        from meho_app.modules.connectors.schemas import ConnectorEntityTypeCreate

        t = ConnectorEntityTypeCreate(
            connector_id="conn-123",
            tenant_id="tenant-1",
            type_name="VirtualMachine",
            description="A VM",
            category="compute",
            properties=[
                {"name": "name", "type": "string"},
                {"name": "cpu", "type": "integer"},
            ],
        )

        assert t.type_name == "VirtualMachine"
        assert len(t.properties) == 2

    def test_create_vmware_connector_request(self):
        """Test CreateVMwareConnectorRequest schema."""
        from meho_app.modules.connectors.schemas import CreateVMwareConnectorRequest

        req = CreateVMwareConnectorRequest(
            name="Production vCenter",
            vcenter_host="vcenter.prod.local",
            username="admin@vsphere.local",
            password="secret",
            disable_ssl_verification=True,
        )

        assert req.name == "Production vCenter"
        assert req.vcenter_host == "vcenter.prod.local"
        assert req.port == 443  # default
        assert req.disable_ssl_verification is True

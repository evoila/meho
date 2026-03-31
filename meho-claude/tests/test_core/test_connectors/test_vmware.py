"""Tests for VMwareConnector with mocked pyvmomi."""

import asyncio
import ssl
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from meho_claude.core.connectors.models import (
    ConnectorConfig,
    Operation,
    TrustOverride,
)


class MockProperty:
    """Simulates a pyvmomi PropertyCollector ObjectContent.propSet entry."""

    def __init__(self, name, val):
        self.name = name
        self.val = val


class MockObjectContent:
    """Simulates a pyvmomi PropertyCollector ObjectContent result."""

    def __init__(self, obj, props: dict):
        self.obj = obj
        self.propSet = [MockProperty(k, v) for k, v in props.items()]


@pytest.fixture
def vmware_config():
    """VMware connector config with basic auth."""
    return ConnectorConfig(
        name="vcenter-prod",
        connector_type="vmware",
        base_url="vcenter.prod.example.com",
        verify_ssl=False,
        tags={"port": "443", "environment": "production"},
    )


@pytest.fixture
def vmware_credentials():
    """VMware basic auth credentials."""
    return {"username": "admin@vsphere.local", "password": "s3cret"}


@pytest.fixture
def vmware_config_verify_ssl():
    """VMware config with SSL verification enabled."""
    return ConnectorConfig(
        name="vcenter-secure",
        connector_type="vmware",
        base_url="vcenter.secure.example.com",
        verify_ssl=True,
    )


# --- Registration Tests ---

class TestVMwareConnectorRegistration:
    def test_vmware_registered_in_registry(self):
        from meho_claude.core.connectors.vmware import VMwareConnector
        from meho_claude.core.connectors.registry import get_connector_class

        cls = get_connector_class("vmware")
        assert cls is VMwareConnector

    def test_vmware_in_list(self):
        from meho_claude.core.connectors.vmware import VMwareConnector  # noqa: F401
        from meho_claude.core.connectors.registry import list_connector_types

        types = list_connector_types()
        assert "vmware" in types


# --- Discover Operations Tests ---

class TestVMwareConnectorDiscoverOperations:
    def test_discover_returns_correct_count(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        ops = asyncio.run(connector.discover_operations())
        # 6 READ + 5 WRITE + 1 DESTRUCTIVE = 12
        assert len(ops) == 12

    def test_discover_returns_operation_models(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        ops = asyncio.run(connector.discover_operations())
        for op in ops:
            assert isinstance(op, Operation)
            assert op.connector_name == "vcenter-prod"

    def test_discover_includes_all_operation_ids(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        ops = asyncio.run(connector.discover_operations())
        op_ids = {op.operation_id for op in ops}
        expected = {
            "list-vms", "get-vm",
            "list-hosts", "list-clusters",
            "list-datastores", "list-networks",
            "power-on-vm", "power-off-vm",
            "create-snapshot", "revert-snapshot",
            "vmotion-vm",
            "delete-snapshot",
        }
        assert op_ids == expected

    def test_write_operations_have_correct_trust_tier(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        ops = asyncio.run(connector.discover_operations())
        op_map = {op.operation_id: op for op in ops}

        assert op_map["power-on-vm"].trust_tier == "WRITE"
        assert op_map["power-off-vm"].trust_tier == "WRITE"
        assert op_map["create-snapshot"].trust_tier == "WRITE"
        assert op_map["revert-snapshot"].trust_tier == "WRITE"
        assert op_map["vmotion-vm"].trust_tier == "WRITE"
        assert op_map["delete-snapshot"].trust_tier == "DESTRUCTIVE"

    def test_read_operations_have_read_trust_tier(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        ops = asyncio.run(connector.discover_operations())
        read_ids = {"list-vms", "get-vm", "list-hosts", "list-clusters", "list-datastores", "list-networks"}
        for op in ops:
            if op.operation_id in read_ids:
                assert op.trust_tier == "READ", f"{op.operation_id} should be READ"


# --- Get Trust Tier Tests ---

class TestVMwareConnectorGetTrustTier:
    def test_default_tier_from_operation(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        op = Operation(
            connector_name="vcenter-prod",
            operation_id="list-vms",
            display_name="List VMs",
            trust_tier="READ",
        )
        assert connector.get_trust_tier(op) == "READ"

    def test_override_from_config(self, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        config = ConnectorConfig(
            name="vcenter-prod",
            connector_type="vmware",
            base_url="vcenter.prod.example.com",
            trust_overrides=[
                TrustOverride(operation_id="list-vms", trust_tier="WRITE"),
            ],
        )
        connector = VMwareConnector(config, vmware_credentials)
        op = Operation(
            connector_name="vcenter-prod",
            operation_id="list-vms",
            display_name="List VMs",
            trust_tier="READ",
        )
        assert connector.get_trust_tier(op) == "WRITE"


# --- SSL / _connect Tests ---

class TestVMwareConnectorConnect:
    @patch("meho_claude.core.connectors.vmware.SmartConnect")
    def test_connect_creates_unverified_ssl_when_verify_ssl_false(
        self, mock_smart_connect, vmware_config, vmware_credentials
    ):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        connector._connect()

        call_args = mock_smart_connect.call_args
        ctx = call_args.kwargs.get("sslContext") or call_args[1].get("sslContext")
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE

    @patch("meho_claude.core.connectors.vmware.SmartConnect")
    def test_connect_uses_config_host_and_port(
        self, mock_smart_connect, vmware_config, vmware_credentials
    ):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        connector._connect()

        call_args = mock_smart_connect.call_args
        assert call_args.kwargs.get("host") == "vcenter.prod.example.com"
        assert call_args.kwargs.get("port") == 443

    @patch("meho_claude.core.connectors.vmware.SmartConnect")
    def test_connect_uses_credentials(
        self, mock_smart_connect, vmware_config, vmware_credentials
    ):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        connector._connect()

        call_args = mock_smart_connect.call_args
        assert call_args.kwargs.get("user") == "admin@vsphere.local"
        assert call_args.kwargs.get("pwd") == "s3cret"

    @patch("meho_claude.core.connectors.vmware.SmartConnect")
    def test_connect_ssl_verified_when_verify_ssl_true(
        self, mock_smart_connect, vmware_config_verify_ssl, vmware_credentials
    ):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config_verify_ssl, vmware_credentials)
        connector._connect()

        call_args = mock_smart_connect.call_args
        ctx = call_args.kwargs.get("sslContext") or call_args[1].get("sslContext")
        # When verify_ssl=True, check_hostname should remain True (default)
        assert ctx.check_hostname is True


# --- test_connection Tests ---

class TestVMwareConnectorTestConnection:
    @patch("meho_claude.core.connectors.vmware.Disconnect")
    @patch("meho_claude.core.connectors.vmware.SmartConnect")
    @patch("meho_claude.core.connectors.vmware.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_test_connection_success(
        self, mock_to_thread, mock_smart_connect, mock_disconnect,
        vmware_config, vmware_credentials
    ):
        from meho_claude.core.connectors.vmware import VMwareConnector

        mock_si = MagicMock()
        server_time = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
        mock_si.CurrentTime.return_value = server_time

        # Make to_thread call the function synchronously
        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync
        mock_smart_connect.return_value = mock_si

        connector = VMwareConnector(vmware_config, vmware_credentials)
        result = await connector.test_connection()

        assert result["status"] == "ok"
        assert "server_time" in result
        mock_disconnect.assert_called_once_with(mock_si)

    @patch("meho_claude.core.connectors.vmware.SmartConnect")
    @patch("meho_claude.core.connectors.vmware.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_test_connection_failure(
        self, mock_to_thread, mock_smart_connect,
        vmware_config, vmware_credentials
    ):
        from meho_claude.core.connectors.vmware import VMwareConnector

        mock_smart_connect.side_effect = Exception("Cannot complete login due to an incorrect user name or password")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = VMwareConnector(vmware_config, vmware_credentials)
        result = await connector.test_connection()

        assert result["status"] == "error"
        assert "incorrect user name" in result["message"]


# --- execute list operations Tests ---

class TestVMwareConnectorExecuteList:
    @patch("meho_claude.core.connectors.vmware.Disconnect")
    @patch("meho_claude.core.connectors.vmware.SmartConnect")
    @patch("meho_claude.core.connectors.vmware.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_list_vms(
        self, mock_to_thread, mock_smart_connect, mock_disconnect,
        vmware_config, vmware_credentials
    ):
        from meho_claude.core.connectors.vmware import VMwareConnector

        mock_si = MagicMock()
        mock_smart_connect.return_value = mock_si

        # Mock _collect_properties return value
        vm_data = [
            {"name": "vm-1", "config.instanceUuid": "uuid-1", "_moref": "vm-100"},
            {"name": "vm-2", "config.instanceUuid": "uuid-2", "_moref": "vm-101"},
        ]

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = VMwareConnector(vmware_config, vmware_credentials)

        # Mock _collect_properties to return our test data
        connector._collect_properties = MagicMock(return_value=vm_data)

        op = Operation(
            connector_name="vcenter-prod",
            operation_id="list-vms",
            display_name="List VMs",
        )
        result = await connector.execute(op, {})

        assert result["data"] == vm_data
        assert len(result["data"]) == 2

    @patch("meho_claude.core.connectors.vmware.Disconnect")
    @patch("meho_claude.core.connectors.vmware.SmartConnect")
    @patch("meho_claude.core.connectors.vmware.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_list_hosts(
        self, mock_to_thread, mock_smart_connect, mock_disconnect,
        vmware_config, vmware_credentials
    ):
        from meho_claude.core.connectors.vmware import VMwareConnector

        mock_si = MagicMock()
        mock_smart_connect.return_value = mock_si

        host_data = [{"name": "esxi-01", "_moref": "host-10"}]

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = VMwareConnector(vmware_config, vmware_credentials)
        connector._collect_properties = MagicMock(return_value=host_data)

        op = Operation(
            connector_name="vcenter-prod",
            operation_id="list-hosts",
            display_name="List Hosts",
        )
        result = await connector.execute(op, {})

        assert result["data"] == host_data


# --- _serialize_property Tests ---

class TestVMwareConnectorSerializeProperty:
    def test_serialize_none(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        assert connector._serialize_property(None) is None

    def test_serialize_str(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        assert connector._serialize_property("hello") == "hello"

    def test_serialize_int(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        assert connector._serialize_property(42) == 42

    def test_serialize_float(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        assert connector._serialize_property(3.14) == 3.14

    def test_serialize_bool(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        assert connector._serialize_property(True) is True

    def test_serialize_datetime(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        dt = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
        result = connector._serialize_property(dt)
        assert result == dt.isoformat()

    def test_serialize_list(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        result = connector._serialize_property([1, "two", 3.0])
        assert result == [1, "two", 3.0]

    def test_serialize_fallback_to_str(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)

        class Exotic:
            def __str__(self):
                return "exotic-obj"

        result = connector._serialize_property(Exotic())
        assert result == "exotic-obj"


# --- _collect_properties ContainerView cleanup Tests ---

class TestVMwareConnectorCollectProperties:
    @patch("meho_claude.core.connectors.vmware.vmodl")
    @patch("meho_claude.core.connectors.vmware.SmartConnect")
    def test_collect_properties_destroys_container_view(
        self, mock_smart_connect, mock_vmodl, vmware_config, vmware_credentials
    ):
        from meho_claude.core.connectors.vmware import VMwareConnector
        from pyVmomi import vim

        mock_si = MagicMock()
        mock_view = MagicMock()
        mock_si.content.viewManager.CreateContainerView.return_value = mock_view

        # Mock PropertyCollector to return empty
        mock_si.content.propertyCollector.RetrieveContents.return_value = []

        connector = VMwareConnector(vmware_config, vmware_credentials)
        connector._collect_properties(mock_si, vim.VirtualMachine, ["name"])

        mock_view.Destroy.assert_called_once()

    @patch("meho_claude.core.connectors.vmware.vmodl")
    @patch("meho_claude.core.connectors.vmware.SmartConnect")
    def test_collect_properties_destroys_view_on_exception(
        self, mock_smart_connect, mock_vmodl, vmware_config, vmware_credentials
    ):
        from meho_claude.core.connectors.vmware import VMwareConnector
        from pyVmomi import vim

        mock_si = MagicMock()
        mock_view = MagicMock()
        mock_si.content.viewManager.CreateContainerView.return_value = mock_view

        # Make RetrieveContents raise
        mock_si.content.propertyCollector.RetrieveContents.side_effect = RuntimeError("boom")

        connector = VMwareConnector(vmware_config, vmware_credentials)

        with pytest.raises(RuntimeError, match="boom"):
            connector._collect_properties(mock_si, vim.VirtualMachine, ["name"])

        # View must still be destroyed
        mock_view.Destroy.assert_called_once()

    @patch("meho_claude.core.connectors.vmware.vmodl")
    @patch("meho_claude.core.connectors.vmware.SmartConnect")
    def test_collect_properties_parses_results(
        self, mock_smart_connect, mock_vmodl, vmware_config, vmware_credentials
    ):
        from meho_claude.core.connectors.vmware import VMwareConnector
        from pyVmomi import vim

        mock_si = MagicMock()
        mock_view = MagicMock()
        mock_si.content.viewManager.CreateContainerView.return_value = mock_view

        # Simulate one VM result
        obj_content = MockObjectContent(
            obj=MagicMock(__str__=lambda self: "vim.VirtualMachine:vm-42"),
            props={"name": "test-vm", "config.instanceUuid": "abc-123"},
        )
        mock_si.content.propertyCollector.RetrieveContents.return_value = [obj_content]

        connector = VMwareConnector(vmware_config, vmware_credentials)
        result = connector._collect_properties(mock_si, vim.VirtualMachine, ["name", "config.instanceUuid"])

        assert len(result) == 1
        assert result[0]["name"] == "test-vm"
        assert result[0]["config.instanceUuid"] == "abc-123"


# --- execute write operations Tests ---

class TestVMwareConnectorExecuteWrite:
    @patch("meho_claude.core.connectors.vmware.Disconnect")
    @patch("meho_claude.core.connectors.vmware.SmartConnect")
    @patch("meho_claude.core.connectors.vmware.WaitForTask")
    @patch("meho_claude.core.connectors.vmware.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_power_on_vm(
        self, mock_to_thread, mock_wait_task, mock_smart_connect, mock_disconnect,
        vmware_config, vmware_credentials
    ):
        from meho_claude.core.connectors.vmware import VMwareConnector

        mock_si = MagicMock()
        mock_smart_connect.return_value = mock_si

        mock_vm = MagicMock()
        mock_task = MagicMock()
        mock_vm.PowerOn.return_value = mock_task

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = VMwareConnector(vmware_config, vmware_credentials)
        connector._find_vm_by_name = MagicMock(return_value=mock_vm)

        op = Operation(
            connector_name="vcenter-prod",
            operation_id="power-on-vm",
            display_name="Power On VM",
            trust_tier="WRITE",
        )
        result = await connector.execute(op, {"name": "test-vm"})

        assert result["status"] == "ok"
        mock_vm.PowerOn.assert_called_once()
        mock_wait_task.assert_called_once_with(mock_task)


# --- close Tests ---

class TestVMwareConnectorClose:
    def test_close_is_noop(self, vmware_config, vmware_credentials):
        from meho_claude.core.connectors.vmware import VMwareConnector

        connector = VMwareConnector(vmware_config, vmware_credentials)
        # Should not raise
        connector.close()

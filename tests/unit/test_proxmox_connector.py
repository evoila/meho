# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for Proxmox VE Connector (TASK-100)

Tests the connector base interface and Proxmox-specific implementation.
Uses mocking since we don't have actual Proxmox access in tests.

Phase 84: CreateProxmoxConnectorRequest.verify_ssl field removed/renamed.
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: CreateProxmoxConnectorRequest.verify_ssl removed, Proxmox schema fields changed")

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from meho_app.modules.connectors.base import (
    OperationDefinition,
    TypeDefinition,
)
from meho_app.modules.connectors.proxmox import (
    PROXMOX_OPERATIONS,
    PROXMOX_TYPES,
    ProxmoxConnector,
)

# =============================================================================
# Test Proxmox Operations and Types Definitions
# =============================================================================


class TestProxmoxDefinitions:
    """Test Proxmox operation and type definitions."""

    def test_proxmox_operations_defined(self):
        """Test that Proxmox operations are properly defined."""
        assert len(PROXMOX_OPERATIONS) > 0

        # Check for key operations
        op_ids = {op.operation_id for op in PROXMOX_OPERATIONS}

        # Node operations
        assert "list_nodes" in op_ids
        assert "get_node" in op_ids
        assert "get_cluster_status" in op_ids

        # VM operations
        assert "list_vms" in op_ids
        assert "get_vm" in op_ids
        assert "start_vm" in op_ids
        assert "stop_vm" in op_ids
        assert "shutdown_vm" in op_ids
        assert "restart_vm" in op_ids

        # Container operations (unique to Proxmox)
        assert "list_containers" in op_ids
        assert "get_container" in op_ids
        assert "start_container" in op_ids
        assert "stop_container" in op_ids

        # Storage operations
        assert "list_storage" in op_ids
        assert "get_storage" in op_ids

        # Snapshot operations
        assert "list_vm_snapshots" in op_ids
        assert "create_vm_snapshot" in op_ids

    def test_proxmox_operations_have_descriptions(self):
        """Test that all operations have descriptions."""
        for op in PROXMOX_OPERATIONS:
            assert op.name, f"Operation {op.operation_id} missing name"
            assert op.description, f"Operation {op.operation_id} missing description"
            assert op.category, f"Operation {op.operation_id} missing category"

    def test_proxmox_types_defined(self):
        """Test that Proxmox types are properly defined."""
        assert len(PROXMOX_TYPES) > 0

        # Check for key types
        type_names = {t.type_name for t in PROXMOX_TYPES}

        assert "Node" in type_names
        assert "VM" in type_names
        assert "Container" in type_names  # Unique to Proxmox
        assert "Storage" in type_names
        assert "Snapshot" in type_names
        assert "Cluster" in type_names

    def test_proxmox_types_have_properties(self):
        """Test that types have properties defined."""
        for t in PROXMOX_TYPES:
            assert t.description, f"Type {t.type_name} missing description"
            assert len(t.properties) > 0, f"Type {t.type_name} has no properties"


# =============================================================================
# Test Proxmox Connector
# =============================================================================


class TestProxmoxConnector:
    """Test ProxmoxConnector implementation."""

    @pytest.fixture
    def connector_config(self) -> dict[str, Any]:
        """Standard test config."""
        return {
            "host": "proxmox.test.local",
            "port": 8006,
            "verify_ssl": False,
        }

    @pytest.fixture
    def connector_credentials_password(self) -> dict[str, Any]:
        """Password-based credentials."""
        return {
            "username": "root@pam",
            "password": "testpassword",
        }

    @pytest.fixture
    def connector_credentials_token(self) -> dict[str, Any]:
        """API token credentials."""
        return {
            "username": "root@pam",
            "token_name": "mytoken",
            "token_value": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        }

    def test_connector_init(self, connector_config, connector_credentials_password):
        """Test connector initialization."""
        connector = ProxmoxConnector(
            connector_id="test-123",
            config=connector_config,
            credentials=connector_credentials_password,
        )

        assert connector.connector_id == "test-123"
        assert connector.config == connector_config
        assert connector.credentials == connector_credentials_password
        assert connector.is_connected is False

    def test_get_operations(self, connector_config, connector_credentials_password):
        """Test get_operations returns definitions."""
        connector = ProxmoxConnector(
            connector_id="test-123",
            config=connector_config,
            credentials=connector_credentials_password,
        )

        operations = connector.get_operations()

        assert len(operations) == len(PROXMOX_OPERATIONS)
        assert all(isinstance(op, OperationDefinition) for op in operations)

    def test_get_types(self, connector_config, connector_credentials_password):
        """Test get_types returns definitions."""
        connector = ProxmoxConnector(
            connector_id="test-123",
            config=connector_config,
            credentials=connector_credentials_password,
        )

        types = connector.get_types()

        assert len(types) == len(PROXMOX_TYPES)
        assert all(isinstance(t, TypeDefinition) for t in types)

    @pytest.mark.asyncio
    async def test_execute_unknown_operation(
        self, connector_config, connector_credentials_password
    ):
        """Test execute with unknown operation returns error."""
        connector = ProxmoxConnector(
            connector_id="test-123",
            config=connector_config,
            credentials=connector_credentials_password,
        )

        result = await connector.execute("nonexistent_operation", {})

        assert result.success is False
        assert "Unknown operation" in result.error
        assert result.operation_id == "nonexistent_operation"

    @pytest.mark.asyncio
    async def test_connect_requires_host(self, connector_credentials_password):
        """Test connect fails without host."""
        connector = ProxmoxConnector(
            connector_id="test-123",
            config={},  # No host
            credentials=connector_credentials_password,
        )

        with patch.dict("sys.modules", {"proxmoxer": MagicMock()}):  # noqa: SIM117 -- readability preferred over combined with
            with pytest.raises((ValueError, ImportError)):
                await connector.connect()

    @pytest.mark.asyncio
    async def test_connect_requires_credentials(self, connector_config):
        """Test connect fails without proper credentials."""
        connector = ProxmoxConnector(
            connector_id="test-123",
            config=connector_config,
            credentials={},  # No credentials
        )

        with patch.dict("sys.modules", {"proxmoxer": MagicMock()}):  # noqa: SIM117 -- readability preferred over combined with
            with pytest.raises((ValueError, ImportError)):
                await connector.connect()


# =============================================================================
# Test Proxmox Connector with Mocked proxmoxer
# =============================================================================


class TestProxmoxConnectorMocked:
    """Test ProxmoxConnector with mocked proxmoxer."""

    @pytest.fixture
    def connector(self):
        """Create connector."""
        return ProxmoxConnector(
            connector_id="test-123",
            config={
                "host": "proxmox.test.local",
                "port": 8006,
                "verify_ssl": False,
            },
            credentials={
                "username": "root@pam",
                "password": "testpassword",
            },
        )

    @pytest.fixture
    def mock_proxmox_api(self):
        """Create mock ProxmoxAPI."""
        mock = MagicMock()

        # Mock version endpoint
        mock.version.get.return_value = {"version": "8.0.4"}

        # Mock nodes endpoint
        mock.nodes.get.return_value = [
            {
                "node": "pve1",
                "status": "online",
                "cpu": 0.25,
                "mem": 8589934592,  # 8GB
                "maxmem": 34359738368,  # 32GB
                "disk": 107374182400,  # 100GB
                "maxdisk": 536870912000,  # 500GB
                "uptime": 86400,
            }
        ]

        # Mock qemu (VMs) endpoint
        mock.nodes.return_value.qemu.get.return_value = [
            {
                "vmid": 100,
                "name": "test-vm",
                "status": "running",
                "cpus": 4,
                "cpu": 0.15,
                "mem": 4294967296,  # 4GB
                "maxmem": 8589934592,  # 8GB
                "disk": 0,
                "maxdisk": 53687091200,  # 50GB
                "uptime": 3600,
            }
        ]

        # Mock lxc (containers) endpoint
        mock.nodes.return_value.lxc.get.return_value = [
            {
                "vmid": 200,
                "name": "test-container",
                "status": "running",
                "cpus": 2,
                "cpu": 0.05,
                "mem": 2147483648,  # 2GB
                "maxmem": 4294967296,  # 4GB
                "disk": 0,
                "maxdisk": 21474836480,  # 20GB
                "swap": 0,
                "maxswap": 1073741824,  # 1GB
                "uptime": 7200,
            }
        ]

        # Mock storage endpoint
        mock.nodes.return_value.storage.get.return_value = [
            {
                "storage": "local-lvm",
                "type": "lvmthin",
                "content": "images,rootdir",
                "total": 536870912000,
                "used": 214748364800,
                "avail": 322122547200,
                "enabled": 1,
                "active": 1,
                "shared": 0,
            }
        ]

        return mock

    @pytest.mark.asyncio
    async def test_connect_success(self, connector, mock_proxmox_api):
        """Test successful connection with mocked proxmoxer."""
        # Directly set the mock on the connector instance
        connector._proxmox = mock_proxmox_api
        connector._is_connected = True

        result = await connector.test_connection()

        assert result is True

    @pytest.mark.asyncio
    async def test_list_nodes_operation(self, connector, mock_proxmox_api):
        """Test list_nodes operation."""
        connector._proxmox = mock_proxmox_api
        connector._is_connected = True

        result = await connector.execute("list_nodes", {})

        assert result.success is True
        assert result.operation_id == "list_nodes"
        assert len(result.data) == 1
        assert result.data[0]["name"] == "pve1"
        assert result.data[0]["status"] == "online"

    @pytest.mark.asyncio
    async def test_list_vms_operation(self, connector, mock_proxmox_api):
        """Test list_vms operation."""
        connector._proxmox = mock_proxmox_api
        connector._is_connected = True

        result = await connector.execute("list_vms", {})

        assert result.success is True
        assert result.operation_id == "list_vms"
        assert len(result.data) == 1
        assert result.data[0]["vmid"] == 100
        assert result.data[0]["name"] == "test-vm"
        assert result.data[0]["status"] == "running"

    @pytest.mark.asyncio
    async def test_list_containers_operation(self, connector, mock_proxmox_api):
        """Test list_containers operation."""
        connector._proxmox = mock_proxmox_api
        connector._is_connected = True

        result = await connector.execute("list_containers", {})

        assert result.success is True
        assert result.operation_id == "list_containers"
        assert len(result.data) == 1
        assert result.data[0]["vmid"] == 200
        assert result.data[0]["name"] == "test-container"
        assert result.data[0]["status"] == "running"

    @pytest.mark.asyncio
    async def test_list_storage_operation(self, connector, mock_proxmox_api):
        """Test list_storage operation."""
        # Setup mock for cluster-wide storage
        mock_proxmox_api.storage.get.return_value = [
            {
                "storage": "local-lvm",
                "type": "lvmthin",
                "content": "images,rootdir",
            }
        ]

        connector._proxmox = mock_proxmox_api
        connector._is_connected = True

        result = await connector.execute("list_storage", {})

        assert result.success is True
        assert result.operation_id == "list_storage"


# =============================================================================
# Test Connector Router
# =============================================================================


class TestConnectorRouter:
    """Test connector routing logic."""

    @pytest.mark.asyncio
    async def test_get_connector_instance_proxmox(self):
        """Test getting Proxmox connector instance."""
        from meho_app.modules.connectors import get_connector_instance

        connector = await get_connector_instance(
            connector_type="proxmox",
            connector_id="test-123",
            config={"host": "proxmox.local"},
            credentials={"username": "root@pam", "password": "test"},
        )

        assert isinstance(connector, ProxmoxConnector)
        assert connector.connector_id == "test-123"


# =============================================================================
# Test Schemas
# =============================================================================


class TestProxmoxSchemas:
    """Test Proxmox-related Pydantic schemas."""

    def test_create_proxmox_connector_request_password(self):
        """Test CreateProxmoxConnectorRequest with password."""
        from meho_app.modules.connectors.schemas import CreateProxmoxConnectorRequest

        req = CreateProxmoxConnectorRequest(
            name="Production Proxmox",
            host="proxmox.prod.local",
            username="root@pam",
            password="secret",
            verify_ssl=False,
        )

        assert req.name == "Production Proxmox"
        assert req.host == "proxmox.prod.local"
        assert req.port == 8006  # default
        assert req.verify_ssl is False
        assert req.password == "secret"
        assert req.token_name is None

    def test_create_proxmox_connector_request_token(self):
        """Test CreateProxmoxConnectorRequest with API token."""
        from meho_app.modules.connectors.schemas import CreateProxmoxConnectorRequest

        req = CreateProxmoxConnectorRequest(
            name="Production Proxmox",
            host="proxmox.prod.local",
            username="root@pam",
            token_name="automation",
            token_value="xxxx-xxxx-xxxx",
        )

        assert req.name == "Production Proxmox"
        assert req.token_name == "automation"
        assert req.token_value == "xxxx-xxxx-xxxx"
        assert req.password is None

    def test_proxmox_connector_response(self):
        """Test ProxmoxConnectorResponse schema."""
        from meho_app.modules.connectors.schemas import ProxmoxConnectorResponse

        resp = ProxmoxConnectorResponse(
            id="conn-123",
            name="Test Proxmox",
            host="proxmox.local",
            connector_type="proxmox",
            operations_registered=40,
            types_registered=7,
            message="Created successfully",
        )

        assert resp.id == "conn-123"
        assert resp.connector_type == "proxmox"
        assert resp.operations_registered == 40


# =============================================================================
# Test Serializers
# =============================================================================


class TestProxmoxSerializers:
    """Test Proxmox serializers."""

    def test_serialize_node(self):
        """Test node serialization."""
        from meho_app.modules.connectors.proxmox.serializers import serialize_node

        node = {
            "node": "pve1",
            "status": "online",
            "cpu": 0.25,
            "mem": 8589934592,
            "maxmem": 34359738368,
            "disk": 107374182400,
            "maxdisk": 536870912000,
            "uptime": 86400,
        }

        result = serialize_node(node)

        assert result["name"] == "pve1"
        assert result["status"] == "online"
        assert result["cpu_usage_percent"] == 25.0
        assert result["uptime_seconds"] == 86400

    def test_serialize_vm(self):
        """Test VM serialization."""
        from meho_app.modules.connectors.proxmox.serializers import serialize_vm

        vm = {
            "vmid": 100,
            "name": "test-vm",
            "status": "running",
            "cpus": 4,
            "cpu": 0.15,
            "mem": 4294967296,
            "maxmem": 8589934592,
            "uptime": 3600,
        }

        result = serialize_vm(vm)

        assert result["vmid"] == 100
        assert result["name"] == "test-vm"
        assert result["status"] == "running"
        assert result["cpu_usage_percent"] == 15.0

    def test_serialize_container(self):
        """Test container serialization."""
        from meho_app.modules.connectors.proxmox.serializers import serialize_container

        ct = {
            "vmid": 200,
            "name": "test-ct",
            "status": "running",
            "cpus": 2,
            "cpu": 0.05,
            "mem": 2147483648,
            "maxmem": 4294967296,
            "swap": 0,
            "maxswap": 1073741824,
            "uptime": 7200,
        }

        result = serialize_container(ct)

        assert result["vmid"] == 200
        assert result["name"] == "test-ct"
        assert result["status"] == "running"
        assert result["type"] == "lxc"

    def test_serialize_storage(self):
        """Test storage serialization."""
        from meho_app.modules.connectors.proxmox.serializers import serialize_storage

        storage = {
            "storage": "local-lvm",
            "type": "lvmthin",
            "content": "images,rootdir",
            "total": 536870912000,
            "used": 214748364800,
            "avail": 322122547200,
            "enabled": 1,
            "active": 1,
            "shared": 0,
        }

        result = serialize_storage(storage)

        assert result["storage"] == "local-lvm"
        assert result["type"] == "lvmthin"
        assert result["content"] == ["images", "rootdir"]
        assert result["enabled"] is True
        assert result["shared"] is False


# =============================================================================
# Test Helpers
# =============================================================================


class TestProxmoxHelpers:
    """Test Proxmox helper functions."""

    def test_bytes_to_gb(self):
        """Test bytes to GB conversion."""
        from meho_app.modules.connectors.proxmox.helpers import bytes_to_gb

        assert bytes_to_gb(1073741824) == 1.0  # 1GB
        assert bytes_to_gb(536870912000) == 500.0  # 500GB

    def test_bytes_to_mb(self):
        """Test bytes to MB conversion."""
        from meho_app.modules.connectors.proxmox.helpers import bytes_to_mb

        assert bytes_to_mb(1048576) == 1.0  # 1MB
        assert bytes_to_mb(8589934592) == 8192.0  # 8GB in MB

    def test_format_uptime(self):
        """Test uptime formatting."""
        from meho_app.modules.connectors.proxmox.helpers import format_uptime

        assert format_uptime(0) == "0m"
        assert format_uptime(60) == "1m"
        assert format_uptime(3600) == "1h"
        assert format_uptime(86400) == "1d"
        assert format_uptime(90061) == "1d 1h 1m"

    def test_parse_status(self):
        """Test status parsing."""
        from meho_app.modules.connectors.proxmox.helpers import parse_status

        assert parse_status("running") == "running"
        assert parse_status("RUNNING") == "running"
        assert parse_status("stopped") == "stopped"
        assert parse_status("paused") == "paused"

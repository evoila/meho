"""Tests for ProxmoxConnector with mocked proxmoxer."""

import asyncio
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from meho_claude.core.connectors.models import (
    ConnectorConfig,
    Operation,
    TrustOverride,
)


@pytest.fixture
def proxmox_token_config():
    """Proxmox connector config with API token auth."""
    return ConnectorConfig(
        name="proxmox-prod",
        connector_type="proxmox",
        base_url="https://pve.prod.example.com:8006",
        proxmox_token_id="root@pam!meho-token",
        verify_ssl=False,
        timeout=30,
        tags={"environment": "production"},
    )


@pytest.fixture
def proxmox_token_credentials():
    """Proxmox API token credentials."""
    return {
        "username": "root@pam",
        "token_value": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    }


@pytest.fixture
def proxmox_password_config():
    """Proxmox connector config with user/password auth (no token_id)."""
    return ConnectorConfig(
        name="proxmox-dev",
        connector_type="proxmox",
        base_url="pve.dev.example.com",
        verify_ssl=True,
        timeout=15,
    )


@pytest.fixture
def proxmox_password_credentials():
    """Proxmox user/password credentials."""
    return {"username": "admin@pve", "password": "s3cret"}


# --- Registration Tests ---


class TestProxmoxConnectorRegistration:
    def test_proxmox_registered_in_registry(self):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector
        from meho_claude.core.connectors.registry import get_connector_class

        cls = get_connector_class("proxmox")
        assert cls is ProxmoxConnector

    def test_proxmox_in_list(self):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector  # noqa: F401
        from meho_claude.core.connectors.registry import list_connector_types

        types = list_connector_types()
        assert "proxmox" in types


# --- Discover Operations Tests ---


class TestProxmoxConnectorDiscoverOperations:
    def test_discover_returns_16_operations(self, proxmox_token_config, proxmox_token_credentials):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        ops = asyncio.run(connector.discover_operations())
        assert len(ops) == 16

    def test_discover_returns_operation_models(self, proxmox_token_config, proxmox_token_credentials):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        ops = asyncio.run(connector.discover_operations())
        for op in ops:
            assert isinstance(op, Operation)
            assert op.connector_name == "proxmox-prod"

    def test_discover_includes_all_operation_ids(self, proxmox_token_config, proxmox_token_credentials):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        ops = asyncio.run(connector.discover_operations())
        op_ids = {op.operation_id for op in ops}
        expected = {
            # 8 READ
            "list-vms", "get-vm", "list-containers", "get-container",
            "list-nodes", "get-node", "list-storage", "list-ceph-pools",
            # 6 WRITE
            "power-on-vm", "power-off-vm", "power-on-container",
            "power-off-container", "snapshot-vm", "migrate-vm",
            # 2 DESTRUCTIVE
            "revert-snapshot-vm", "delete-snapshot-vm",
        }
        assert op_ids == expected

    def test_read_operations_have_read_trust_tier(self, proxmox_token_config, proxmox_token_credentials):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        ops = asyncio.run(connector.discover_operations())
        read_ids = {
            "list-vms", "get-vm", "list-containers", "get-container",
            "list-nodes", "get-node", "list-storage", "list-ceph-pools",
        }
        for op in ops:
            if op.operation_id in read_ids:
                assert op.trust_tier == "READ", f"{op.operation_id} should be READ"

    def test_write_operations_have_correct_trust_tier(self, proxmox_token_config, proxmox_token_credentials):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        ops = asyncio.run(connector.discover_operations())
        op_map = {op.operation_id: op for op in ops}

        assert op_map["power-on-vm"].trust_tier == "WRITE"
        assert op_map["power-off-vm"].trust_tier == "WRITE"
        assert op_map["power-on-container"].trust_tier == "WRITE"
        assert op_map["power-off-container"].trust_tier == "WRITE"
        assert op_map["snapshot-vm"].trust_tier == "WRITE"
        assert op_map["migrate-vm"].trust_tier == "WRITE"
        assert op_map["revert-snapshot-vm"].trust_tier == "DESTRUCTIVE"
        assert op_map["delete-snapshot-vm"].trust_tier == "DESTRUCTIVE"


# --- Get Trust Tier Tests ---


class TestProxmoxConnectorGetTrustTier:
    def test_default_tier_from_operation(self, proxmox_token_config, proxmox_token_credentials):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="list-vms",
            display_name="List VMs",
            trust_tier="READ",
        )
        assert connector.get_trust_tier(op) == "READ"

    def test_override_from_config(self, proxmox_token_credentials):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        config = ConnectorConfig(
            name="proxmox-prod",
            connector_type="proxmox",
            base_url="pve.prod.example.com",
            trust_overrides=[
                TrustOverride(operation_id="list-vms", trust_tier="WRITE"),
            ],
        )
        connector = ProxmoxConnector(config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="list-vms",
            display_name="List VMs",
            trust_tier="READ",
        )
        assert connector.get_trust_tier(op) == "WRITE"


# --- _connect Tests ---


class TestProxmoxConnectorConnect:
    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    def test_connect_with_token_auth(
        self, mock_proxmox_api, proxmox_token_config, proxmox_token_credentials
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        connector._connect()

        mock_proxmox_api.assert_called_once_with(
            "pve.prod.example.com",
            port=8006,
            user="root@pam",
            token_name="meho-token",
            token_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            verify_ssl=False,
            timeout=30,
        )

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    def test_connect_with_password_auth(
        self, mock_proxmox_api, proxmox_password_config, proxmox_password_credentials
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        connector = ProxmoxConnector(proxmox_password_config, proxmox_password_credentials)
        connector._connect()

        mock_proxmox_api.assert_called_once_with(
            "pve.dev.example.com",
            port=8006,
            user="admin@pve",
            password="s3cret",
            verify_ssl=True,
            timeout=15,
        )

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    def test_connect_strips_https_prefix(
        self, mock_proxmox_api, proxmox_token_config, proxmox_token_credentials
    ):
        """base_url with https:// prefix should have protocol stripped."""
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        connector._connect()

        call_args = mock_proxmox_api.call_args
        # First positional arg is the host
        assert call_args[0][0] == "pve.prod.example.com"

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    def test_connect_extracts_port_from_url(
        self, mock_proxmox_api, proxmox_token_config, proxmox_token_credentials
    ):
        """Port from URL like https://host:8006 should be extracted."""
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        connector._connect()

        call_args = mock_proxmox_api.call_args
        assert call_args[1]["port"] == 8006

    def test_connect_missing_credentials_raises(self, proxmox_token_config):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        connector = ProxmoxConnector(proxmox_token_config, None)
        with pytest.raises((ValueError, TypeError, KeyError)):
            connector._connect()


# --- test_connection Tests ---


class TestProxmoxConnectorTestConnection:
    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_test_connection_success(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_pve.nodes.get.return_value = [
            {"node": "pve1", "status": "online"},
            {"node": "pve2", "status": "online"},
        ]
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        result = await connector.test_connection()

        assert result["status"] == "ok"
        assert result["node_count"] == 2

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_test_connection_failure(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_proxmox_api.side_effect = Exception("Connection refused")

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        result = await connector.test_connection()

        assert result["status"] == "error"
        assert "Connection refused" in result["message"]


# --- execute list operations Tests ---


class TestProxmoxConnectorExecuteList:
    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_list_vms(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_pve.nodes.get.return_value = [{"node": "pve1"}, {"node": "pve2"}]
        mock_pve.nodes.return_value.qemu.get.side_effect = [
            [{"vmid": 100, "name": "vm-1", "node": "pve1"}],
            [{"vmid": 101, "name": "vm-2", "node": "pve2"}],
        ]
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="list-vms",
            display_name="List VMs",
        )
        result = await connector.execute(op, {})

        assert "data" in result
        assert len(result["data"]) == 2

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_get_vm(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_pve.nodes.return_value.qemu.return_value.status.current.get.return_value = {
            "vmid": 100, "name": "vm-1", "status": "running",
        }
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="get-vm",
            display_name="Get VM",
        )
        result = await connector.execute(op, {"node": "pve1", "vmid": "100"})

        assert "data" in result
        assert result["data"]["vmid"] == 100

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_list_containers(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_pve.nodes.get.return_value = [{"node": "pve1"}]
        mock_pve.nodes.return_value.lxc.get.return_value = [
            {"vmid": 200, "name": "ct-1", "node": "pve1"},
        ]
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="list-containers",
            display_name="List Containers",
        )
        result = await connector.execute(op, {})

        assert "data" in result
        assert len(result["data"]) == 1

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_list_nodes(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_pve.nodes.get.return_value = [
            {"node": "pve1", "status": "online"},
            {"node": "pve2", "status": "online"},
        ]
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="list-nodes",
            display_name="List Nodes",
        )
        result = await connector.execute(op, {})

        assert "data" in result
        assert len(result["data"]) == 2

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_list_ceph_pools(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_pve.nodes.get.return_value = [{"node": "pve1"}]
        mock_pve.nodes.return_value.ceph.pools.get.return_value = [
            {"name": "rbd-pool", "size": 3, "pg_num": 128},
        ]
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="list-ceph-pools",
            display_name="List Ceph Pools",
        )
        result = await connector.execute(op, {})

        assert "data" in result
        assert len(result["data"]) == 1

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_list_storage(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_pve.nodes.get.return_value = [{"node": "pve1"}]
        mock_pve.nodes.return_value.storage.get.return_value = [
            {"storage": "local", "type": "dir", "total": 100000},
        ]
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="list-storage",
            display_name="List Storage",
        )
        result = await connector.execute(op, {})

        assert "data" in result
        assert len(result["data"]) == 1


# --- execute write operations Tests ---


class TestProxmoxConnectorExecuteWrite:
    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_power_on_vm(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_pve.nodes.return_value.qemu.return_value.status.start.post.return_value = "UPID:pve1:001"
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="power-on-vm",
            display_name="Power On VM",
            trust_tier="WRITE",
        )
        result = await connector.execute(op, {"node": "pve1", "vmid": "100"})

        assert result["status"] == "ok"

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_power_off_vm(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_pve.nodes.return_value.qemu.return_value.status.stop.post.return_value = "UPID:pve1:002"
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="power-off-vm",
            display_name="Power Off VM",
            trust_tier="WRITE",
        )
        result = await connector.execute(op, {"node": "pve1", "vmid": "100"})

        assert result["status"] == "ok"

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_snapshot_vm(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_pve.nodes.return_value.qemu.return_value.snapshot.post.return_value = "UPID:pve1:003"
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="snapshot-vm",
            display_name="Snapshot VM",
            trust_tier="WRITE",
        )
        result = await connector.execute(
            op, {"node": "pve1", "vmid": "100", "snapname": "before-upgrade", "description": "Pre-upgrade snapshot"}
        )

        assert result["status"] == "ok"

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_revert_snapshot_vm(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_pve.nodes.return_value.qemu.return_value.snapshot.return_value.rollback.post.return_value = "UPID:pve1:004"
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="revert-snapshot-vm",
            display_name="Revert Snapshot VM",
            trust_tier="DESTRUCTIVE",
        )
        result = await connector.execute(
            op, {"node": "pve1", "vmid": "100", "snapname": "before-upgrade"}
        )

        assert result["status"] == "ok"

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_delete_snapshot_vm(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_pve.nodes.return_value.qemu.return_value.snapshot.return_value.delete.return_value = "UPID:pve1:005"
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="delete-snapshot-vm",
            display_name="Delete Snapshot VM",
            trust_tier="DESTRUCTIVE",
        )
        result = await connector.execute(
            op, {"node": "pve1", "vmid": "100", "snapname": "before-upgrade"}
        )

        assert result["status"] == "ok"

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_migrate_vm(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_pve.nodes.return_value.qemu.return_value.migrate.post.return_value = "UPID:pve1:006"
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="migrate-vm",
            display_name="Migrate VM",
            trust_tier="WRITE",
        )
        result = await connector.execute(
            op, {"node": "pve1", "vmid": "100", "target": "pve2"}
        )

        assert result["status"] == "ok"

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_power_on_container(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_pve.nodes.return_value.lxc.return_value.status.start.post.return_value = "UPID:pve1:007"
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="power-on-container",
            display_name="Power On Container",
            trust_tier="WRITE",
        )
        result = await connector.execute(op, {"node": "pve1", "vmid": "200"})

        assert result["status"] == "ok"

    @patch("meho_claude.core.connectors.proxmox.ProxmoxAPI")
    @patch("meho_claude.core.connectors.proxmox.asyncio.to_thread")
    @pytest.mark.asyncio
    async def test_execute_unknown_operation_raises(
        self, mock_to_thread, mock_proxmox_api,
        proxmox_token_config, proxmox_token_credentials,
    ):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        mock_pve = MagicMock()
        mock_proxmox_api.return_value = mock_pve

        async def call_sync(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_to_thread.side_effect = call_sync

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        op = Operation(
            connector_name="proxmox-prod",
            operation_id="unknown-op",
            display_name="Unknown Op",
        )
        with pytest.raises(ValueError, match="Unknown operation"):
            await connector.execute(op, {})


# --- close Tests ---


class TestProxmoxConnectorClose:
    def test_close_is_noop(self, proxmox_token_config, proxmox_token_credentials):
        from meho_claude.core.connectors.proxmox import ProxmoxConnector

        connector = ProxmoxConnector(proxmox_token_config, proxmox_token_credentials)
        # Should not raise
        connector.close()

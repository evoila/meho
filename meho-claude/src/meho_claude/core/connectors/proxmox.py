"""Proxmox connector implementing BaseConnector via proxmoxer.

Registered as "proxmox" in the connector registry. Uses synchronous proxmoxer
wrapped with asyncio.to_thread() for async compatibility. Supports 5 resource
types (VMs, Containers, Nodes, Storage, Ceph Pools) plus write operations
(power on/off, snapshots, migration).
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

from proxmoxer import ProxmoxAPI

from meho_claude.core.connectors.base import BaseConnector
from meho_claude.core.connectors.models import ConnectorConfig, Operation
from meho_claude.core.connectors.registry import register_connector

# Operation definitions (connector_name substituted at discover time)
_PROXMOX_OPERATIONS = [
    # READ operations (8)
    {
        "operation_id": "list-vms",
        "display_name": "List VMs",
        "description": "List all QEMU virtual machines across all nodes",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["proxmox", "vms", "qemu"],
    },
    {
        "operation_id": "get-vm",
        "display_name": "Get VM",
        "description": "Get a specific QEMU virtual machine status",
        "trust_tier": "READ",
        "input_schema": {
            "node": {"type": "string", "required": True},
            "vmid": {"type": "string", "required": True},
        },
        "tags": ["proxmox", "vms", "qemu"],
    },
    {
        "operation_id": "list-containers",
        "display_name": "List Containers",
        "description": "List all LXC containers across all nodes",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["proxmox", "containers", "lxc"],
    },
    {
        "operation_id": "get-container",
        "display_name": "Get Container",
        "description": "Get a specific LXC container status",
        "trust_tier": "READ",
        "input_schema": {
            "node": {"type": "string", "required": True},
            "vmid": {"type": "string", "required": True},
        },
        "tags": ["proxmox", "containers", "lxc"],
    },
    {
        "operation_id": "list-nodes",
        "display_name": "List Nodes",
        "description": "List all Proxmox cluster nodes with status",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["proxmox", "nodes"],
    },
    {
        "operation_id": "get-node",
        "display_name": "Get Node",
        "description": "Get detailed status of a specific Proxmox node",
        "trust_tier": "READ",
        "input_schema": {
            "node": {"type": "string", "required": True},
        },
        "tags": ["proxmox", "nodes"],
    },
    {
        "operation_id": "list-storage",
        "display_name": "List Storage",
        "description": "List all storage across all nodes",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["proxmox", "storage"],
    },
    {
        "operation_id": "list-ceph-pools",
        "display_name": "List Ceph Pools",
        "description": "List all Ceph pools (cluster-wide via first available node)",
        "trust_tier": "READ",
        "input_schema": {},
        "tags": ["proxmox", "ceph", "storage"],
    },
    # WRITE operations (6)
    {
        "operation_id": "power-on-vm",
        "display_name": "Power On VM",
        "description": "Start a QEMU virtual machine",
        "trust_tier": "WRITE",
        "input_schema": {
            "node": {"type": "string", "required": True},
            "vmid": {"type": "string", "required": True},
        },
        "tags": ["proxmox", "vms", "power"],
    },
    {
        "operation_id": "power-off-vm",
        "display_name": "Power Off VM",
        "description": "Stop a QEMU virtual machine",
        "trust_tier": "WRITE",
        "input_schema": {
            "node": {"type": "string", "required": True},
            "vmid": {"type": "string", "required": True},
        },
        "tags": ["proxmox", "vms", "power"],
    },
    {
        "operation_id": "power-on-container",
        "display_name": "Power On Container",
        "description": "Start an LXC container",
        "trust_tier": "WRITE",
        "input_schema": {
            "node": {"type": "string", "required": True},
            "vmid": {"type": "string", "required": True},
        },
        "tags": ["proxmox", "containers", "power"],
    },
    {
        "operation_id": "power-off-container",
        "display_name": "Power Off Container",
        "description": "Stop an LXC container",
        "trust_tier": "WRITE",
        "input_schema": {
            "node": {"type": "string", "required": True},
            "vmid": {"type": "string", "required": True},
        },
        "tags": ["proxmox", "containers", "power"],
    },
    {
        "operation_id": "snapshot-vm",
        "display_name": "Snapshot VM",
        "description": "Create a snapshot of a QEMU virtual machine",
        "trust_tier": "WRITE",
        "input_schema": {
            "node": {"type": "string", "required": True},
            "vmid": {"type": "string", "required": True},
            "snapname": {"type": "string", "required": True},
            "description": {"type": "string", "required": False},
        },
        "tags": ["proxmox", "vms", "snapshots"],
    },
    {
        "operation_id": "migrate-vm",
        "display_name": "Migrate VM",
        "description": "Migrate a QEMU virtual machine to another node",
        "trust_tier": "WRITE",
        "input_schema": {
            "node": {"type": "string", "required": True},
            "vmid": {"type": "string", "required": True},
            "target": {"type": "string", "required": True},
        },
        "tags": ["proxmox", "vms", "migration"],
    },
    # DESTRUCTIVE operations (2)
    {
        "operation_id": "revert-snapshot-vm",
        "display_name": "Revert Snapshot VM",
        "description": "Revert a QEMU virtual machine to a named snapshot (destroys current state)",
        "trust_tier": "DESTRUCTIVE",
        "input_schema": {
            "node": {"type": "string", "required": True},
            "vmid": {"type": "string", "required": True},
            "snapname": {"type": "string", "required": True},
        },
        "tags": ["proxmox", "vms", "snapshots", "destructive"],
    },
    {
        "operation_id": "delete-snapshot-vm",
        "display_name": "Delete Snapshot VM",
        "description": "Delete a named snapshot from a QEMU virtual machine",
        "trust_tier": "DESTRUCTIVE",
        "input_schema": {
            "node": {"type": "string", "required": True},
            "vmid": {"type": "string", "required": True},
            "snapname": {"type": "string", "required": True},
        },
        "tags": ["proxmox", "vms", "snapshots", "destructive"],
    },
]


def _parse_host_and_port(base_url: str) -> tuple[str, int]:
    """Extract host and port from base_url, stripping protocol prefix.

    Handles:
        - "pve.example.com" -> ("pve.example.com", 8006)
        - "https://pve.example.com:8006" -> ("pve.example.com", 8006)
        - "pve.example.com:8006" -> ("pve.example.com", 8006)
    """
    url = base_url
    # If no scheme, add one for urlparse to work correctly
    if "://" not in url:
        url = f"https://{url}"

    parsed = urlparse(url)
    host = parsed.hostname or base_url
    port = parsed.port or 8006
    return host, port


@register_connector("proxmox")
class ProxmoxConnector(BaseConnector):
    """Proxmox VE connector using proxmoxer library.

    Uses synchronous proxmoxer wrapped with asyncio.to_thread() for all
    Proxmox API operations. Creates a fresh connection per execute() call
    (no cached ProxmoxAPI instance).

    Auth supports two modes:
        1. API token (primary): proxmox_token_id in config + token_value in credentials
        2. User/password (fallback): username + password in credentials
    """

    def __init__(self, config_obj: ConnectorConfig, credentials: dict | None = None) -> None:
        super().__init__(config_obj, credentials)

    def _connect(self) -> ProxmoxAPI:
        """Create a synchronous Proxmox API connection.

        Uses API token auth when config.proxmox_token_id is set,
        otherwise falls back to user/password auth.
        """
        if not self.credentials:
            raise ValueError("Proxmox connector requires credentials")

        host, port = _parse_host_and_port(self.config.base_url)

        if self.config.proxmox_token_id:
            # Token auth: proxmox_token_id format is "user@realm!tokenname"
            # Split on "!" to get token_name
            parts = self.config.proxmox_token_id.split("!")
            if len(parts) == 2:
                user = parts[0]
                token_name = parts[1]
            else:
                user = self.credentials["username"]
                token_name = self.config.proxmox_token_id

            return ProxmoxAPI(
                host,
                port=port,
                user=user,
                token_name=token_name,
                token_value=self.credentials["token_value"],
                verify_ssl=self.config.verify_ssl,
                timeout=self.config.timeout,
            )
        else:
            return ProxmoxAPI(
                host,
                port=port,
                user=self.credentials["username"],
                password=self.credentials["password"],
                verify_ssl=self.config.verify_ssl,
                timeout=self.config.timeout,
            )

    async def test_connection(self) -> dict[str, Any]:
        """Test connectivity by connecting and listing nodes.

        Returns:
            Dict with status and node_count on success,
            or status and error message on failure.
        """
        try:
            pve = await asyncio.to_thread(self._connect)
            nodes = await asyncio.to_thread(pve.nodes.get)
            return {"status": "ok", "node_count": len(nodes)}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    async def discover_operations(self) -> list[Operation]:
        """Return hardcoded operations for all Proxmox resource types.

        Operations are defined at module level and built with the
        actual connector name at discovery time.
        """
        operations = []
        for op_def in _PROXMOX_OPERATIONS:
            operations.append(
                Operation(
                    connector_name=self.config.name,
                    operation_id=op_def["operation_id"],
                    display_name=op_def["display_name"],
                    description=op_def["description"],
                    trust_tier=op_def["trust_tier"],
                    input_schema=op_def["input_schema"],
                    tags=op_def["tags"],
                )
            )
        return operations

    async def execute(self, operation: Operation, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a Proxmox operation via proxmoxer.

        Creates a fresh ProxmoxAPI connection per call. All sync proxmoxer
        calls are wrapped in asyncio.to_thread().
        """
        op_id = operation.operation_id
        pve = await asyncio.to_thread(self._connect)

        # READ operations
        if op_id == "list-vms":
            return await self._execute_list_vms(pve)
        elif op_id == "get-vm":
            return await self._execute_get_vm(pve, params)
        elif op_id == "list-containers":
            return await self._execute_list_containers(pve)
        elif op_id == "get-container":
            return await self._execute_get_container(pve, params)
        elif op_id == "list-nodes":
            return await self._execute_list_nodes(pve)
        elif op_id == "get-node":
            return await self._execute_get_node(pve, params)
        elif op_id == "list-storage":
            return await self._execute_list_storage(pve)
        elif op_id == "list-ceph-pools":
            return await self._execute_list_ceph_pools(pve)

        # WRITE operations
        elif op_id == "power-on-vm":
            return await self._execute_power_on_vm(pve, params)
        elif op_id == "power-off-vm":
            return await self._execute_power_off_vm(pve, params)
        elif op_id == "power-on-container":
            return await self._execute_power_on_container(pve, params)
        elif op_id == "power-off-container":
            return await self._execute_power_off_container(pve, params)
        elif op_id == "snapshot-vm":
            return await self._execute_snapshot_vm(pve, params)
        elif op_id == "migrate-vm":
            return await self._execute_migrate_vm(pve, params)

        # DESTRUCTIVE operations
        elif op_id == "revert-snapshot-vm":
            return await self._execute_revert_snapshot_vm(pve, params)
        elif op_id == "delete-snapshot-vm":
            return await self._execute_delete_snapshot_vm(pve, params)

        else:
            raise ValueError(f"Unknown operation: {op_id}")

    # --- READ handlers ---

    async def _execute_list_vms(self, pve: Any) -> dict[str, Any]:
        """List all QEMU VMs across all nodes."""
        nodes = await asyncio.to_thread(pve.nodes.get)
        all_vms = []
        for node_info in nodes:
            node_name = node_info["node"]
            vms = await asyncio.to_thread(pve.nodes(node_name).qemu.get)
            for vm in vms:
                vm["node"] = node_name
            all_vms.extend(vms)
        return {"data": all_vms}

    async def _execute_get_vm(self, pve: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Get specific QEMU VM status."""
        node = params["node"]
        vmid = params["vmid"]
        data = await asyncio.to_thread(pve.nodes(node).qemu(vmid).status.current.get)
        return {"data": data}

    async def _execute_list_containers(self, pve: Any) -> dict[str, Any]:
        """List all LXC containers across all nodes."""
        nodes = await asyncio.to_thread(pve.nodes.get)
        all_cts = []
        for node_info in nodes:
            node_name = node_info["node"]
            cts = await asyncio.to_thread(pve.nodes(node_name).lxc.get)
            for ct in cts:
                ct["node"] = node_name
            all_cts.extend(cts)
        return {"data": all_cts}

    async def _execute_get_container(self, pve: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Get specific LXC container status."""
        node = params["node"]
        vmid = params["vmid"]
        data = await asyncio.to_thread(pve.nodes(node).lxc(vmid).status.current.get)
        return {"data": data}

    async def _execute_list_nodes(self, pve: Any) -> dict[str, Any]:
        """List all Proxmox cluster nodes."""
        data = await asyncio.to_thread(pve.nodes.get)
        return {"data": data}

    async def _execute_get_node(self, pve: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Get detailed status of a specific node."""
        node = params["node"]
        data = await asyncio.to_thread(pve.nodes(node).status.get)
        return {"data": data}

    async def _execute_list_storage(self, pve: Any) -> dict[str, Any]:
        """List all storage across all nodes."""
        nodes = await asyncio.to_thread(pve.nodes.get)
        all_storage = []
        for node_info in nodes:
            node_name = node_info["node"]
            storage = await asyncio.to_thread(pve.nodes(node_name).storage.get)
            for s in storage:
                s["node"] = node_name
            all_storage.extend(storage)
        return {"data": all_storage}

    async def _execute_list_ceph_pools(self, pve: Any) -> dict[str, Any]:
        """List Ceph pools (cluster-wide, queried via first available node)."""
        nodes = await asyncio.to_thread(pve.nodes.get)
        if not nodes:
            return {"data": []}
        first_node = nodes[0]["node"]
        data = await asyncio.to_thread(pve.nodes(first_node).ceph.pools.get)
        return {"data": data}

    # --- WRITE handlers ---

    async def _execute_power_on_vm(self, pve: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Power on a QEMU VM."""
        node, vmid = params["node"], params["vmid"]
        upid = await asyncio.to_thread(pve.nodes(node).qemu(vmid).status.start.post)
        return {"status": "ok", "message": f"VM {vmid} power on started", "upid": upid}

    async def _execute_power_off_vm(self, pve: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Power off a QEMU VM."""
        node, vmid = params["node"], params["vmid"]
        upid = await asyncio.to_thread(pve.nodes(node).qemu(vmid).status.stop.post)
        return {"status": "ok", "message": f"VM {vmid} power off started", "upid": upid}

    async def _execute_power_on_container(self, pve: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Power on an LXC container."""
        node, vmid = params["node"], params["vmid"]
        upid = await asyncio.to_thread(pve.nodes(node).lxc(vmid).status.start.post)
        return {"status": "ok", "message": f"Container {vmid} power on started", "upid": upid}

    async def _execute_power_off_container(self, pve: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Power off an LXC container."""
        node, vmid = params["node"], params["vmid"]
        upid = await asyncio.to_thread(pve.nodes(node).lxc(vmid).status.stop.post)
        return {"status": "ok", "message": f"Container {vmid} power off started", "upid": upid}

    async def _execute_snapshot_vm(self, pve: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Create a VM snapshot."""
        node, vmid = params["node"], params["vmid"]
        snapname = params["snapname"]
        description = params.get("description", "")
        upid = await asyncio.to_thread(
            pve.nodes(node).qemu(vmid).snapshot.post,
            snapname=snapname,
            description=description,
        )
        return {"status": "ok", "message": f"Snapshot '{snapname}' created for VM {vmid}", "upid": upid}

    async def _execute_migrate_vm(self, pve: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Migrate a VM to another node."""
        node, vmid = params["node"], params["vmid"]
        target = params["target"]
        upid = await asyncio.to_thread(
            pve.nodes(node).qemu(vmid).migrate.post,
            target=target,
        )
        return {"status": "ok", "message": f"VM {vmid} migration to {target} started", "upid": upid}

    # --- DESTRUCTIVE handlers ---

    async def _execute_revert_snapshot_vm(self, pve: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Revert a VM to a named snapshot."""
        node, vmid = params["node"], params["vmid"]
        snapname = params["snapname"]
        upid = await asyncio.to_thread(
            pve.nodes(node).qemu(vmid).snapshot(snapname).rollback.post,
        )
        return {"status": "ok", "message": f"VM {vmid} reverted to snapshot '{snapname}'", "upid": upid}

    async def _execute_delete_snapshot_vm(self, pve: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Delete a named snapshot from a VM."""
        node, vmid = params["node"], params["vmid"]
        snapname = params["snapname"]
        upid = await asyncio.to_thread(
            pve.nodes(node).qemu(vmid).snapshot(snapname).delete,
        )
        return {"status": "ok", "message": f"Snapshot '{snapname}' deleted from VM {vmid}", "upid": upid}

    def get_trust_tier(self, operation: Operation) -> str:
        """Determine trust tier, checking config overrides first."""
        override_map = {o.operation_id: o.trust_tier for o in self.config.trust_overrides}
        if operation.operation_id in override_map:
            return override_map[operation.operation_id]
        return operation.trust_tier

    def close(self) -> None:
        """No-op -- Proxmox connection created per-execute call."""
        pass

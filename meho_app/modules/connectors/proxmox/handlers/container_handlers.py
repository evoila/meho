# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox Container (LXC) Operation Handlers

Mixin class containing LXC container operation handlers.
"""

from typing import Any

from meho_app.modules.connectors.proxmox.helpers import find_container, get_all_containers
from meho_app.modules.connectors.proxmox.serializers import serialize_container, serialize_snapshot


class ContainerHandlerMixin:
    """Mixin for LXC container operation handlers."""

    # This will be provided by ProxmoxConnector
    _proxmox: Any

    async def _list_containers(self, params: dict[str, Any]) -> list[dict]:
        """List all containers across all nodes or on a specific node."""
        node = params.get("node")

        if node:
            # List containers on specific node
            containers = self._proxmox.nodes(node).lxc.get()
            result = []
            for ct in containers:
                ct["node"] = node
                result.append(serialize_container(ct))
            return result
        else:
            # List all containers across all nodes
            all_containers = get_all_containers(self._proxmox)
            return [serialize_container(ct) for ct in all_containers]

    async def _get_container(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get detailed information about a specific container."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        # Find container if node not specified
        if not node:
            ct_info = find_container(self._proxmox, vmid)
            if not ct_info:
                raise ValueError(f"Container {vmid} not found")
            node = ct_info["node"]

        # Get container info and config
        ct_api = self._proxmox.nodes(node).lxc(vmid)
        status = ct_api.status.current.get()
        config = ct_api.config.get()

        # Merge status into a dict and add node
        ct_data = dict(status)
        ct_data["node"] = node
        ct_data["vmid"] = vmid

        return serialize_container(ct_data, config)

    async def _get_container_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get current status and metrics for a container."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            ct_info = find_container(self._proxmox, vmid)
            if not ct_info:
                raise ValueError(f"Container {vmid} not found")
            node = ct_info["node"]

        status = self._proxmox.nodes(node).lxc(vmid).status.current.get()
        status["node"] = node
        status["vmid"] = vmid

        return serialize_container(status)

    async def _start_container(self, params: dict[str, Any]) -> dict[str, Any]:
        """Start a container."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            ct_info = find_container(self._proxmox, vmid)
            if not ct_info:
                raise ValueError(f"Container {vmid} not found")
            node = ct_info["node"]

        upid = self._proxmox.nodes(node).lxc(vmid).status.start.post()
        return {"success": True, "task_id": upid, "vmid": vmid, "action": "start"}

    async def _stop_container(self, params: dict[str, Any]) -> dict[str, Any]:
        """Stop a container (immediate stop)."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            ct_info = find_container(self._proxmox, vmid)
            if not ct_info:
                raise ValueError(f"Container {vmid} not found")
            node = ct_info["node"]

        upid = self._proxmox.nodes(node).lxc(vmid).status.stop.post()
        return {"success": True, "task_id": upid, "vmid": vmid, "action": "stop"}

    async def _shutdown_container(self, params: dict[str, Any]) -> dict[str, Any]:
        """Gracefully shutdown a container."""
        vmid = params.get("vmid")
        node = params.get("node")
        timeout = params.get("timeout", 60)

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            ct_info = find_container(self._proxmox, vmid)
            if not ct_info:
                raise ValueError(f"Container {vmid} not found")
            node = ct_info["node"]

        upid = self._proxmox.nodes(node).lxc(vmid).status.shutdown.post(timeout=timeout)
        return {"success": True, "task_id": upid, "vmid": vmid, "action": "shutdown"}

    async def _restart_container(self, params: dict[str, Any]) -> dict[str, Any]:
        """Restart a container."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            ct_info = find_container(self._proxmox, vmid)
            if not ct_info:
                raise ValueError(f"Container {vmid} not found")
            node = ct_info["node"]

        upid = self._proxmox.nodes(node).lxc(vmid).status.reboot.post()
        return {"success": True, "task_id": upid, "vmid": vmid, "action": "restart"}

    async def _get_container_config(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get container configuration."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            ct_info = find_container(self._proxmox, vmid)
            if not ct_info:
                raise ValueError(f"Container {vmid} not found")
            node = ct_info["node"]

        config = self._proxmox.nodes(node).lxc(vmid).config.get()
        return {"vmid": vmid, "node": node, "config": dict(config)}

    async def _clone_container(self, params: dict[str, Any]) -> dict[str, Any]:
        """Clone a container."""
        vmid = params.get("vmid")
        newid = params.get("newid")
        node = params.get("node")
        hostname = params.get("hostname")
        full = params.get("full", True)

        if not vmid or not newid:
            raise ValueError("vmid and newid are required")

        if not node:
            ct_info = find_container(self._proxmox, vmid)
            if not ct_info:
                raise ValueError(f"Container {vmid} not found")
            node = ct_info["node"]

        clone_params = {"newid": newid, "full": 1 if full else 0}
        if hostname:
            clone_params["hostname"] = hostname

        upid = self._proxmox.nodes(node).lxc(vmid).clone.post(**clone_params)
        return {
            "success": True,
            "task_id": upid,
            "source_vmid": vmid,
            "new_vmid": newid,
            "action": "clone",
        }

    async def _migrate_container(self, params: dict[str, Any]) -> dict[str, Any]:
        """Migrate a container to another node."""
        vmid = params.get("vmid")
        node = params.get("node")
        target = params.get("target")
        online = params.get("online", True)

        if not vmid or not node or not target:
            raise ValueError("vmid, node, and target are required")

        migrate_params = {"target": target, "online": 1 if online else 0}

        upid = self._proxmox.nodes(node).lxc(vmid).migrate.post(**migrate_params)
        return {
            "success": True,
            "task_id": upid,
            "vmid": vmid,
            "source": node,
            "target": target,
            "action": "migrate",
        }

    # =========================================================================
    # CONTAINER SNAPSHOT OPERATIONS
    # =========================================================================

    async def _list_container_snapshots(self, params: dict[str, Any]) -> list[dict]:
        """List all snapshots for a container."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            ct_info = find_container(self._proxmox, vmid)
            if not ct_info:
                raise ValueError(f"Container {vmid} not found")
            node = ct_info["node"]

        snapshots = self._proxmox.nodes(node).lxc(vmid).snapshot.get()
        return [serialize_snapshot(s) for s in snapshots if s.get("name") != "current"]

    async def _create_container_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create a container snapshot."""
        vmid = params.get("vmid")
        snapname = params.get("snapname")
        node = params.get("node")
        description = params.get("description", "")

        if not vmid or not snapname:
            raise ValueError("vmid and snapname are required")

        if not node:
            ct_info = find_container(self._proxmox, vmid)
            if not ct_info:
                raise ValueError(f"Container {vmid} not found")
            node = ct_info["node"]

        snap_params = {
            "snapname": snapname,
            "description": description,
        }

        upid = self._proxmox.nodes(node).lxc(vmid).snapshot.post(**snap_params)
        return {
            "success": True,
            "task_id": upid,
            "vmid": vmid,
            "snapname": snapname,
            "action": "create_snapshot",
        }

    async def _delete_container_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delete a container snapshot."""
        vmid = params.get("vmid")
        snapname = params.get("snapname")
        node = params.get("node")

        if not vmid or not snapname:
            raise ValueError("vmid and snapname are required")

        if not node:
            ct_info = find_container(self._proxmox, vmid)
            if not ct_info:
                raise ValueError(f"Container {vmid} not found")
            node = ct_info["node"]

        upid = self._proxmox.nodes(node).lxc(vmid).snapshot(snapname).delete()
        return {
            "success": True,
            "task_id": upid,
            "vmid": vmid,
            "snapname": snapname,
            "action": "delete_snapshot",
        }

    async def _rollback_container_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        """Rollback a container to a snapshot."""
        vmid = params.get("vmid")
        snapname = params.get("snapname")
        node = params.get("node")

        if not vmid or not snapname:
            raise ValueError("vmid and snapname are required")

        if not node:
            ct_info = find_container(self._proxmox, vmid)
            if not ct_info:
                raise ValueError(f"Container {vmid} not found")
            node = ct_info["node"]

        upid = self._proxmox.nodes(node).lxc(vmid).snapshot(snapname).rollback.post()
        return {
            "success": True,
            "task_id": upid,
            "vmid": vmid,
            "snapname": snapname,
            "action": "rollback_snapshot",
        }

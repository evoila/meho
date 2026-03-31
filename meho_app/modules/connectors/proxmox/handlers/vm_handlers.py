# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox VM (QEMU) Operation Handlers

Mixin class containing VM operation handlers.
"""

from typing import Any

from meho_app.modules.connectors.proxmox.helpers import find_vm, get_all_vms
from meho_app.modules.connectors.proxmox.serializers import serialize_snapshot, serialize_vm


class VMHandlerMixin:
    """Mixin for VM operation handlers."""

    # This will be provided by ProxmoxConnector
    _proxmox: Any

    async def _list_vms(self, params: dict[str, Any]) -> list[dict]:
        """List all VMs across all nodes or on a specific node."""
        node = params.get("node")

        if node:
            # List VMs on specific node
            vms = self._proxmox.nodes(node).qemu.get()
            result = []
            for vm in vms:
                vm["node"] = node
                result.append(serialize_vm(vm))
            return result
        else:
            # List all VMs across all nodes
            all_vms = get_all_vms(self._proxmox)
            return [serialize_vm(vm) for vm in all_vms]

    async def _get_vm(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get detailed information about a specific VM."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        # Find VM if node not specified
        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        # Get VM info and config
        vm_api = self._proxmox.nodes(node).qemu(vmid)
        status = vm_api.status.current.get()
        config = vm_api.config.get()

        # Merge status into a dict and add node
        vm_data = dict(status)
        vm_data["node"] = node
        vm_data["vmid"] = vmid

        return serialize_vm(vm_data, config)

    async def _get_vm_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get current status and metrics for a VM."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        status = self._proxmox.nodes(node).qemu(vmid).status.current.get()
        status["node"] = node
        status["vmid"] = vmid

        return serialize_vm(status)

    async def _start_vm(self, params: dict[str, Any]) -> dict[str, Any]:
        """Start a VM."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        upid = self._proxmox.nodes(node).qemu(vmid).status.start.post()
        return {"success": True, "task_id": upid, "vmid": vmid, "action": "start"}

    async def _stop_vm(self, params: dict[str, Any]) -> dict[str, Any]:
        """Stop a VM (hard stop)."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        upid = self._proxmox.nodes(node).qemu(vmid).status.stop.post()
        return {"success": True, "task_id": upid, "vmid": vmid, "action": "stop"}

    async def _shutdown_vm(self, params: dict[str, Any]) -> dict[str, Any]:
        """Gracefully shutdown a VM."""
        vmid = params.get("vmid")
        node = params.get("node")
        timeout = params.get("timeout", 60)

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        upid = self._proxmox.nodes(node).qemu(vmid).status.shutdown.post(timeout=timeout)
        return {"success": True, "task_id": upid, "vmid": vmid, "action": "shutdown"}

    async def _restart_vm(self, params: dict[str, Any]) -> dict[str, Any]:
        """Reboot a VM."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        upid = self._proxmox.nodes(node).qemu(vmid).status.reboot.post()
        return {"success": True, "task_id": upid, "vmid": vmid, "action": "reboot"}

    async def _reset_vm(self, params: dict[str, Any]) -> dict[str, Any]:
        """Hard reset a VM."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        upid = self._proxmox.nodes(node).qemu(vmid).status.reset.post()
        return {"success": True, "task_id": upid, "vmid": vmid, "action": "reset"}

    async def _suspend_vm(self, params: dict[str, Any]) -> dict[str, Any]:
        """Suspend a VM to disk."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        upid = self._proxmox.nodes(node).qemu(vmid).status.suspend.post()
        return {"success": True, "task_id": upid, "vmid": vmid, "action": "suspend"}

    async def _resume_vm(self, params: dict[str, Any]) -> dict[str, Any]:
        """Resume a suspended VM."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        upid = self._proxmox.nodes(node).qemu(vmid).status.resume.post()
        return {"success": True, "task_id": upid, "vmid": vmid, "action": "resume"}

    async def _get_vm_config(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get VM configuration."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        config = self._proxmox.nodes(node).qemu(vmid).config.get()
        return {"vmid": vmid, "node": node, "config": dict(config)}

    async def _clone_vm(self, params: dict[str, Any]) -> dict[str, Any]:
        """Clone a VM."""
        vmid = params.get("vmid")
        newid = params.get("newid")
        node = params.get("node")
        name = params.get("name")
        full = params.get("full", True)

        if not vmid or not newid:
            raise ValueError("vmid and newid are required")

        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        clone_params = {"newid": newid, "full": 1 if full else 0}
        if name:
            clone_params["name"] = name

        upid = self._proxmox.nodes(node).qemu(vmid).clone.post(**clone_params)
        return {
            "success": True,
            "task_id": upid,
            "source_vmid": vmid,
            "new_vmid": newid,
            "action": "clone",
        }

    async def _migrate_vm(self, params: dict[str, Any]) -> dict[str, Any]:
        """Migrate a VM to another node."""
        vmid = params.get("vmid")
        node = params.get("node")
        target = params.get("target")
        online = params.get("online", True)

        if not vmid or not node or not target:
            raise ValueError("vmid, node, and target are required")

        migrate_params = {"target": target, "online": 1 if online else 0}

        upid = self._proxmox.nodes(node).qemu(vmid).migrate.post(**migrate_params)
        return {
            "success": True,
            "task_id": upid,
            "vmid": vmid,
            "source": node,
            "target": target,
            "action": "migrate",
        }

    # =========================================================================
    # VM SNAPSHOT OPERATIONS
    # =========================================================================

    async def _list_vm_snapshots(self, params: dict[str, Any]) -> list[dict]:
        """List all snapshots for a VM."""
        vmid = params.get("vmid")
        node = params.get("node")

        if not vmid:
            raise ValueError("vmid is required")

        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        snapshots = self._proxmox.nodes(node).qemu(vmid).snapshot.get()
        return [serialize_snapshot(s) for s in snapshots if s.get("name") != "current"]

    async def _create_vm_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create a VM snapshot."""
        vmid = params.get("vmid")
        snapname = params.get("snapname")
        node = params.get("node")
        description = params.get("description", "")
        vmstate = params.get("vmstate", False)

        if not vmid or not snapname:
            raise ValueError("vmid and snapname are required")

        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        snap_params = {
            "snapname": snapname,
            "description": description,
            "vmstate": 1 if vmstate else 0,
        }

        upid = self._proxmox.nodes(node).qemu(vmid).snapshot.post(**snap_params)
        return {
            "success": True,
            "task_id": upid,
            "vmid": vmid,
            "snapname": snapname,
            "action": "create_snapshot",
        }

    async def _delete_vm_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        """Delete a VM snapshot."""
        vmid = params.get("vmid")
        snapname = params.get("snapname")
        node = params.get("node")

        if not vmid or not snapname:
            raise ValueError("vmid and snapname are required")

        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        upid = self._proxmox.nodes(node).qemu(vmid).snapshot(snapname).delete()
        return {
            "success": True,
            "task_id": upid,
            "vmid": vmid,
            "snapname": snapname,
            "action": "delete_snapshot",
        }

    async def _rollback_vm_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        """Rollback a VM to a snapshot."""
        vmid = params.get("vmid")
        snapname = params.get("snapname")
        node = params.get("node")

        if not vmid or not snapname:
            raise ValueError("vmid and snapname are required")

        if not node:
            vm_info = find_vm(self._proxmox, vmid)
            if not vm_info:
                raise ValueError(f"VM {vmid} not found")
            node = vm_info["node"]

        upid = self._proxmox.nodes(node).qemu(vmid).snapshot(snapname).rollback.post()
        return {
            "success": True,
            "task_id": upid,
            "vmid": vmid,
            "snapname": snapname,
            "action": "rollback_snapshot",
        }

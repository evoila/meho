# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox Connector Helper Functions

Utility functions for finding resources and common operations.
"""

from typing import Any

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


def find_node(proxmox: Any, node_name: str) -> dict[str, Any] | None:
    """
    Find a node by name.

    Args:
        proxmox: ProxmoxAPI instance
        node_name: Name of the node to find

    Returns:
        Node info dict or None if not found
    """
    try:
        nodes = proxmox.nodes.get()
        for node in nodes:
            if node.get("node") == node_name:
                return node
        return None
    except Exception as e:
        logger.error(f"Error finding node {node_name}: {e}")
        return None


def find_vm(proxmox: Any, vmid: int, node: str | None = None) -> dict[str, Any] | None:
    """
    Find a VM by VMID, optionally on a specific node.

    Args:
        proxmox: ProxmoxAPI instance
        vmid: VM ID
        node: Optional node name to search on

    Returns:
        Dict with vm info and node, or None if not found
    """
    try:
        if node:
            # Search on specific node
            vms = proxmox.nodes(node).qemu.get()
            for vm in vms:
                if vm.get("vmid") == vmid:
                    return {"vm": vm, "node": node}
        else:
            # Search all nodes
            nodes = proxmox.nodes.get()
            for n in nodes:
                node_name = n.get("node")
                try:
                    vms = proxmox.nodes(node_name).qemu.get()
                    for vm in vms:
                        if vm.get("vmid") == vmid:
                            return {"vm": vm, "node": node_name}
                except Exception:  # noqa: S112 -- intentional continue on exception
                    continue
        return None
    except Exception as e:
        logger.error(f"Error finding VM {vmid}: {e}")
        return None


def find_container(proxmox: Any, vmid: int, node: str | None = None) -> dict[str, Any] | None:
    """
    Find a container by VMID, optionally on a specific node.

    Args:
        proxmox: ProxmoxAPI instance
        vmid: Container ID
        node: Optional node name to search on

    Returns:
        Dict with container info and node, or None if not found
    """
    try:
        if node:
            # Search on specific node
            containers = proxmox.nodes(node).lxc.get()
            for ct in containers:
                if ct.get("vmid") == vmid:
                    return {"container": ct, "node": node}
        else:
            # Search all nodes
            nodes = proxmox.nodes.get()
            for n in nodes:
                node_name = n.get("node")
                try:
                    containers = proxmox.nodes(node_name).lxc.get()
                    for ct in containers:
                        if ct.get("vmid") == vmid:
                            return {"container": ct, "node": node_name}
                except Exception:  # noqa: S112 -- intentional continue on exception
                    continue
        return None
    except Exception as e:
        logger.error(f"Error finding container {vmid}: {e}")
        return None


def bytes_to_gb(bytes_val: int) -> float:
    """Convert bytes to gigabytes."""
    return round(bytes_val / (1024**3), 2)


def bytes_to_mb(bytes_val: int) -> float:
    """Convert bytes to megabytes."""
    return round(bytes_val / (1024**2), 2)


def format_uptime(seconds: int) -> str:
    """Format uptime seconds to human-readable string."""
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")

    return " ".join(parts) if parts else "0m"


def parse_status(status: str) -> str:
    """Normalize Proxmox status strings."""
    status_map = {
        "running": "running",
        "stopped": "stopped",
        "paused": "paused",
        "suspended": "suspended",
    }
    return status_map.get(status.lower(), status)


def get_all_vms(proxmox: Any) -> list[dict[str, Any]]:
    """
    Get all VMs across all nodes.

    Args:
        proxmox: ProxmoxAPI instance

    Returns:
        List of VM info dicts with node name included
    """
    all_vms = []
    try:
        nodes = proxmox.nodes.get()
        for node in nodes:
            node_name = node.get("node")
            try:
                vms = proxmox.nodes(node_name).qemu.get()
                for vm in vms:
                    vm["node"] = node_name
                    all_vms.append(vm)
            except Exception as e:
                logger.warning(f"Error getting VMs from node {node_name}: {e}")
                continue
    except Exception as e:
        logger.error(f"Error getting nodes: {e}")

    return all_vms


def get_all_containers(proxmox: Any) -> list[dict[str, Any]]:
    """
    Get all containers across all nodes.

    Args:
        proxmox: ProxmoxAPI instance

    Returns:
        List of container info dicts with node name included
    """
    all_containers = []
    try:
        nodes = proxmox.nodes.get()
        for node in nodes:
            node_name = node.get("node")
            try:
                containers = proxmox.nodes(node_name).lxc.get()
                for ct in containers:
                    ct["node"] = node_name
                    all_containers.append(ct)
            except Exception as e:
                logger.warning(f"Error getting containers from node {node_name}: {e}")
                continue
    except Exception as e:
        logger.error(f"Error getting nodes: {e}")

    return all_containers

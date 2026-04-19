# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox Object Serializers

Convert Proxmox API responses to standardized dictionaries
for consistent agent consumption.
"""

from typing import Any

from meho_app.modules.connectors.proxmox.helpers import (
    bytes_to_gb,
    bytes_to_mb,
    format_uptime,
    parse_status,
)


def serialize_node(node: dict[str, Any], status: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Serialize a Proxmox node to a standardized dict.

    Args:
        node: Node info from proxmox.nodes.get()
        status: Optional detailed status from node.status.get()

    Returns:
        Serialized node dict
    """
    result = {
        "name": node.get("node"),
        "status": node.get("status", "unknown"),
        "uptime": format_uptime(node.get("uptime", 0)),
        "uptime_seconds": node.get("uptime", 0),
        "cpu_usage_percent": round(node.get("cpu", 0) * 100, 1),
        "memory_used_mb": bytes_to_mb(node.get("mem", 0)),
        "memory_total_mb": bytes_to_mb(node.get("maxmem", 0)),
        "memory_usage_percent": round((node.get("mem", 0) / node.get("maxmem", 1)) * 100, 1)
        if node.get("maxmem")
        else 0,
        "disk_used_gb": bytes_to_gb(node.get("disk", 0)),
        "disk_total_gb": bytes_to_gb(node.get("maxdisk", 0)),
        "disk_usage_percent": round((node.get("disk", 0) / node.get("maxdisk", 1)) * 100, 1)
        if node.get("maxdisk")
        else 0,
    }

    if status:
        result.update(
            {
                "kernel_version": status.get("kversion", ""),
                "pve_version": status.get("pveversion", ""),
                "cpu_info": status.get("cpuinfo", {}),
                "boot_mode": status.get("boot-info", {}).get("mode", ""),
            }
        )

    return result


def serialize_vm(vm: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Serialize a Proxmox VM to a standardized dict.

    Args:
        vm: VM info from proxmox.nodes(node).qemu.get()
        config: Optional VM config from qemu(vmid).config.get()

    Returns:
        Serialized VM dict
    """
    result = {
        "vmid": vm.get("vmid"),
        "name": vm.get("name", f"VM {vm.get('vmid')}"),
        "node": vm.get("node", ""),
        "status": parse_status(vm.get("status", "unknown")),
        "cpu_count": vm.get("cpus", 0),
        "cpu_usage_percent": round(vm.get("cpu", 0) * 100, 1),
        "memory_mb": bytes_to_mb(vm.get("maxmem", 0)),
        "memory_used_mb": bytes_to_mb(vm.get("mem", 0)),
        "memory_usage_percent": round((vm.get("mem", 0) / vm.get("maxmem", 1)) * 100, 1)
        if vm.get("maxmem")
        else 0,
        "disk_size_gb": bytes_to_gb(vm.get("maxdisk", 0)),
        "disk_used_gb": bytes_to_gb(vm.get("disk", 0)),
        "uptime": format_uptime(vm.get("uptime", 0)),
        "uptime_seconds": vm.get("uptime", 0),
        "template": vm.get("template", 0) == 1,
        "tags": vm.get("tags", "").split(";") if vm.get("tags") else [],
        "network_in_bytes": vm.get("netin", 0),
        "network_out_bytes": vm.get("netout", 0),
        "disk_read_bytes": vm.get("diskread", 0),
        "disk_write_bytes": vm.get("diskwrite", 0),
    }

    if config:
        result.update(
            {
                "cores": config.get("cores", 1),
                "sockets": config.get("sockets", 1),
                "os_type": config.get("ostype", ""),
                "boot_order": config.get("boot", ""),
                "description": config.get("description", ""),
                "agent_enabled": config.get("agent", "0") == "1",
            }
        )

    return result


def serialize_container(ct: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Serialize a Proxmox LXC container to a standardized dict.

    Args:
        ct: Container info from proxmox.nodes(node).lxc.get()
        config: Optional container config from lxc(vmid).config.get()

    Returns:
        Serialized container dict
    """
    result = {
        "vmid": ct.get("vmid"),
        "name": ct.get("name", f"CT {ct.get('vmid')}"),
        "node": ct.get("node", ""),
        "status": parse_status(ct.get("status", "unknown")),
        "cpu_count": ct.get("cpus", 0),
        "cpu_usage_percent": round(ct.get("cpu", 0) * 100, 1),
        "memory_mb": bytes_to_mb(ct.get("maxmem", 0)),
        "memory_used_mb": bytes_to_mb(ct.get("mem", 0)),
        "memory_usage_percent": round((ct.get("mem", 0) / ct.get("maxmem", 1)) * 100, 1)
        if ct.get("maxmem")
        else 0,
        "disk_size_gb": bytes_to_gb(ct.get("maxdisk", 0)),
        "disk_used_gb": bytes_to_gb(ct.get("disk", 0)),
        "swap_mb": bytes_to_mb(ct.get("maxswap", 0)),
        "swap_used_mb": bytes_to_mb(ct.get("swap", 0)),
        "uptime": format_uptime(ct.get("uptime", 0)),
        "uptime_seconds": ct.get("uptime", 0),
        "template": ct.get("template", 0) == 1,
        "tags": ct.get("tags", "").split(";") if ct.get("tags") else [],
        "network_in_bytes": ct.get("netin", 0),
        "network_out_bytes": ct.get("netout", 0),
        "disk_read_bytes": ct.get("diskread", 0),
        "disk_write_bytes": ct.get("diskwrite", 0),
        "type": "lxc",
    }

    if config:
        result.update(
            {
                "hostname": config.get("hostname", ""),
                "os_template": config.get("ostemplate", ""),
                "arch": config.get("arch", ""),
                "description": config.get("description", ""),
                "unprivileged": config.get("unprivileged", 0) == 1,
            }
        )

    return result


def serialize_storage(storage: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize Proxmox storage info to a standardized dict.

    Args:
        storage: Storage info from proxmox.nodes(node).storage.get()

    Returns:
        Serialized storage dict
    """
    return {
        "storage": storage.get("storage"),
        "type": storage.get("type", ""),
        "content": storage.get("content", "").split(",") if storage.get("content") else [],
        "total_gb": bytes_to_gb(storage.get("total", 0)),
        "used_gb": bytes_to_gb(storage.get("used", 0)),
        "available_gb": bytes_to_gb(storage.get("avail", 0)),
        "usage_percent": round((storage.get("used", 0) / storage.get("total", 1)) * 100, 1)
        if storage.get("total")
        else 0,
        "enabled": storage.get("enabled", 1) == 1,
        "active": storage.get("active", 0) == 1,
        "shared": storage.get("shared", 0) == 1,
    }


def serialize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a Proxmox snapshot to a standardized dict.

    Args:
        snapshot: Snapshot info from qemu/lxc snapshots list

    Returns:
        Serialized snapshot dict
    """
    return {
        "name": snapshot.get("name"),
        "description": snapshot.get("description", ""),
        "snaptime": snapshot.get("snaptime"),
        "parent": snapshot.get("parent", ""),
        "vmstate": snapshot.get("vmstate", 0) == 1,  # Includes RAM state
    }


def serialize_task(task: dict[str, Any]) -> dict[str, Any]:
    """
    Serialize a Proxmox task to a standardized dict.

    Args:
        task: Task info

    Returns:
        Serialized task dict
    """
    return {
        "upid": task.get("upid"),
        "node": task.get("node", ""),
        "type": task.get("type", ""),
        "status": task.get("status", ""),
        "user": task.get("user", ""),
        "starttime": task.get("starttime"),
        "endtime": task.get("endtime"),
        "exitstatus": task.get("exitstatus", ""),
    }

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox Node Operation Handlers

Mixin class containing node and cluster operation handlers.
"""

from typing import Any

from meho_app.modules.connectors.proxmox.serializers import serialize_node


class NodeHandlerMixin:
    """Mixin for node operation handlers."""

    # This will be provided by ProxmoxConnector
    _proxmox: Any

    async def _list_nodes(self, _params: dict[str, Any]) -> list[dict]:
        """List all nodes in the cluster."""
        nodes = self._proxmox.nodes.get()
        return [serialize_node(node) for node in nodes]

    async def _get_node(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get detailed information about a specific node."""
        node_name = params.get("node")

        if not node_name:
            raise ValueError("node is required")

        # Get basic node info from nodes list
        nodes = self._proxmox.nodes.get()
        node_info = None
        for n in nodes:
            if n.get("node") == node_name:
                node_info = n
                break

        if not node_info:
            raise ValueError(f"Node {node_name} not found")

        # Get detailed status
        status = self._proxmox.nodes(node_name).status.get()

        return serialize_node(node_info, status)

    async def _get_node_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get current status and system info for a node."""
        node_name = params.get("node")

        if not node_name:
            raise ValueError("node is required")

        status = self._proxmox.nodes(node_name).status.get()

        return {
            "node": node_name,
            "kernel_version": status.get("kversion", ""),
            "pve_version": status.get("pveversion", ""),
            "cpu_info": status.get("cpuinfo", {}),
            "boot_mode": status.get("boot-info", {}).get("mode", ""),
            "memory": {
                "total_mb": round(status.get("memory", {}).get("total", 0) / (1024 * 1024), 2),
                "used_mb": round(status.get("memory", {}).get("used", 0) / (1024 * 1024), 2),
                "free_mb": round(status.get("memory", {}).get("free", 0) / (1024 * 1024), 2),
            },
            "swap": {
                "total_mb": round(status.get("swap", {}).get("total", 0) / (1024 * 1024), 2),
                "used_mb": round(status.get("swap", {}).get("used", 0) / (1024 * 1024), 2),
                "free_mb": round(status.get("swap", {}).get("free", 0) / (1024 * 1024), 2),
            },
            "uptime": status.get("uptime", 0),
            "loadavg": status.get("loadavg", []),
        }

    async def _get_node_resources(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get detailed resource usage for a node."""
        node_name = params.get("node")

        if not node_name:
            raise ValueError("node is required")

        # Get basic node info
        nodes = self._proxmox.nodes.get()
        node_info = None
        for n in nodes:
            if n.get("node") == node_name:
                node_info = n
                break

        if not node_info:
            raise ValueError(f"Node {node_name} not found")

        # Get RRD data for more detailed metrics if available
        try:
            rrddata = self._proxmox.nodes(node_name).rrddata.get(timeframe="hour")
            latest = rrddata[-1] if rrddata else {}
        except Exception:
            latest = {}

        from meho_app.modules.connectors.proxmox.helpers import bytes_to_gb, bytes_to_mb

        return {
            "node": node_name,
            "cpu": {
                "usage_percent": round(node_info.get("cpu", 0) * 100, 1),
                "count": latest.get("maxcpu", 0),
            },
            "memory": {
                "used_mb": bytes_to_mb(node_info.get("mem", 0)),
                "total_mb": bytes_to_mb(node_info.get("maxmem", 0)),
                "usage_percent": round(
                    (node_info.get("mem", 0) / node_info.get("maxmem", 1)) * 100, 1
                )
                if node_info.get("maxmem")
                else 0,
            },
            "disk": {
                "used_gb": bytes_to_gb(node_info.get("disk", 0)),
                "total_gb": bytes_to_gb(node_info.get("maxdisk", 0)),
                "usage_percent": round(
                    (node_info.get("disk", 0) / node_info.get("maxdisk", 1)) * 100, 1
                )
                if node_info.get("maxdisk")
                else 0,
            },
            "network": {
                "in_bytes": latest.get("netin", 0),
                "out_bytes": latest.get("netout", 0),
            },
        }

    async def _get_cluster_status(self, _params: dict[str, Any]) -> dict[str, Any]:
        """Get overall cluster status."""
        try:
            status = self._proxmox.cluster.status.get()
        except Exception:
            # Single-node setup may not have cluster
            status = []

        # Parse cluster status
        cluster_info = None
        nodes_info = []

        for item in status:
            if item.get("type") == "cluster":
                cluster_info = item
            elif item.get("type") == "node":
                nodes_info.append(
                    {
                        "name": item.get("name"),
                        "id": item.get("id"),
                        "online": item.get("online", 0) == 1,
                        "local": item.get("local", 0) == 1,
                        "ip": item.get("ip", ""),
                    }
                )

        return {
            "cluster_name": cluster_info.get("name") if cluster_info else "standalone",
            "quorate": cluster_info.get("quorate", 1) == 1 if cluster_info else True,
            "version": cluster_info.get("version") if cluster_info else 0,
            "nodes": nodes_info,
            "node_count": len(nodes_info),
            "online_count": sum(1 for n in nodes_info if n.get("online")),
        }

    async def _get_cluster_resources(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Get all resources in the cluster."""
        resource_type = params.get("type")

        resources = self._proxmox.cluster.resources.get()

        if resource_type:
            resources = [r for r in resources if r.get("type") == resource_type]

        result = []
        for r in resources:
            result.append(
                {
                    "id": r.get("id"),
                    "type": r.get("type"),
                    "name": r.get("name", ""),
                    "node": r.get("node", ""),
                    "status": r.get("status", ""),
                    "vmid": r.get("vmid"),
                    "cpu": round(r.get("cpu", 0) * 100, 1) if r.get("cpu") else None,
                    "maxcpu": r.get("maxcpu"),
                    "mem": r.get("mem"),
                    "maxmem": r.get("maxmem"),
                    "disk": r.get("disk"),
                    "maxdisk": r.get("maxdisk"),
                    "uptime": r.get("uptime"),
                }
            )

        return result

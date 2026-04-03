# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proxmox Storage Operation Handlers

Mixin class containing storage operation handlers.
"""

from typing import Any

from meho_app.modules.connectors.proxmox.serializers import serialize_storage


class StorageHandlerMixin:
    """Mixin for storage operation handlers."""

    # This will be provided by ProxmoxConnector
    _proxmox: Any

    def _list_storage(self, params: dict[str, Any]) -> list[dict]:
        """List all storage pools."""
        node = params.get("node")

        if node:
            # List storage on specific node
            storage_list = self._proxmox.nodes(node).storage.get()
        else:
            # List all storage (cluster-wide config)
            storage_list = self._proxmox.storage.get()

        return [serialize_storage(s) for s in storage_list]

    def _get_storage(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get detailed information about a storage pool."""
        storage_name = params.get("storage")
        node = params.get("node")

        if not storage_name:
            raise ValueError("storage is required")

        if node:
            # Get storage from specific node (includes usage info)
            storage_list = self._proxmox.nodes(node).storage.get()
            for s in storage_list:
                if s.get("storage") == storage_name:
                    return serialize_storage(s)
            raise ValueError(f"Storage {storage_name} not found on node {node}")
        else:
            # Get storage config from cluster
            storage_config = self._proxmox.storage(storage_name).get()
            return {
                "storage": storage_name,
                "type": storage_config.get("type", ""),
                "content": storage_config.get("content", "").split(",")
                if storage_config.get("content")
                else [],
                "path": storage_config.get("path", ""),
                "nodes": storage_config.get("nodes", "").split(",")
                if storage_config.get("nodes")
                else [],
                "shared": storage_config.get("shared", 0) == 1,
                "config": dict(storage_config),
            }

    def _get_storage_content(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List contents of a storage pool."""
        storage_name = params.get("storage")
        node = params.get("node")
        content_type = params.get("content")

        if not storage_name or not node:
            raise ValueError("storage and node are required")

        content_params = {}
        if content_type:
            content_params["content"] = content_type

        contents = self._proxmox.nodes(node).storage(storage_name).content.get(**content_params)

        result = []
        for item in contents:
            result.append(
                {
                    "volid": item.get("volid"),
                    "content": item.get("content"),
                    "format": item.get("format", ""),
                    "size": item.get("size", 0),
                    "size_gb": round(item.get("size", 0) / (1024**3), 2),
                    "vmid": item.get("vmid"),
                    "notes": item.get("notes", ""),
                    "ctime": item.get("ctime"),
                }
            )

        return result

    def _get_storage_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get status and health for a storage pool."""
        storage_name = params.get("storage")
        node = params.get("node")

        if not storage_name or not node:
            raise ValueError("storage and node are required")

        # Get storage from node's storage list (includes status)
        storage_list = self._proxmox.nodes(node).storage.get()

        for s in storage_list:
            if s.get("storage") == storage_name:
                storage_info = serialize_storage(s)

                # Add RRD data if available
                try:
                    rrddata = (
                        self._proxmox.nodes(node)
                        .storage(storage_name)
                        .rrddata.get(timeframe="hour")
                    )
                    if rrddata:
                        latest = rrddata[-1]
                        storage_info["io"] = {
                            "read_bytes": latest.get("read", 0),
                            "write_bytes": latest.get("write", 0),
                        }
                except Exception:  # noqa: S110 -- intentional silent exception handling
                    pass

                return storage_info

        raise ValueError(f"Storage {storage_name} not found on node {node}")

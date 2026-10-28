# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Identity and size fields of a node row (``k8s.node.list``).

The migration inventory needs them: ``system_uuid`` (the node's BIOS UUID)
matches a node to the VM record of the same machine, so the machine is not
counted twice, and the capacity gives a bare-metal node its size.
"""

from __future__ import annotations

from typing import Any

__all__ = ["NODE_IDENTITY_PROPERTIES", "node_identity"]

#: Response-schema properties of the fields :func:`node_identity` returns.
NODE_IDENTITY_PROPERTIES: dict[str, Any] = {
    "system_uuid": {"type": ["string", "null"], "description": "BIOS UUID (systemUUID)."},
    "os_image": {"type": ["string", "null"], "description": "OS name and version."},
    "architecture": {"type": ["string", "null"], "description": "CPU architecture."},
    "cpu_capacity": {"type": ["string", "null"], "description": "CPU quantity, e.g. '4'."},
    "memory_capacity": {"type": ["string", "null"], "description": "Memory, e.g. '16Gi'."},
}


def _capacity(status: Any, resource: str) -> str | None:
    capacity = getattr(status, "capacity", None) if status is not None else None
    if not capacity:
        return None
    value = capacity.get(resource)
    return str(value) if value is not None else None


def node_identity(node: Any) -> dict[str, Any]:
    """Return the identity and size fields of a :class:`V1Node`."""
    status = node.status
    info = status.node_info if status is not None else None
    return {
        "system_uuid": info.system_uuid if info is not None else None,
        "os_image": info.os_image if info is not None else None,
        "architecture": info.architecture if info is not None else None,
        "cpu_capacity": _capacity(status, "cpu"),
        "memory_capacity": _capacity(status, "memory"),
    }

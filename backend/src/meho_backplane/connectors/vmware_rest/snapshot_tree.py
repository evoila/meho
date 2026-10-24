# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read a VM's snapshot tree into flat rows -- shared by the VM snapshot operations.

vCenter keeps the snapshots of a VM in the VIM property ``VirtualMachine.snapshot``
(``VirtualMachineSnapshotInfo``). Its ``rootSnapshotList`` is a tree of
``VirtualMachineSnapshotTree`` nodes, and ``currentSnapshot`` names the snapshot
the VM's present state builds on. The vCenter REST API has no snapshot paths.

:func:`snapshot_rows` turns that tree into one flat list in tree order (a parent
always comes before its children), and each row names its parent. The typed read
``vmware.snapshot.list`` returns these rows. The snapshot create and delete
composites (:mod:`.composites._snapshot`) use them to find a snapshot and to read
the tree again after their vCenter task.

This module imports nothing from the connector, so the typed read, the composites
and the composite schemas can all import it without an import cycle.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["SNAPSHOT_ROW_SCHEMA", "descendants", "snapshot_rows"]

#: One snapshot row, as :func:`snapshot_rows` builds it.
SNAPSHOT_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "snapshot_id": {
            "type": "string",
            "description": (
                "Snapshot moid, e.g. 'snapshot-12'. Pass it as snapshot_id to "
                "vmware.composite.vm.snapshot.delete or .revert."
            ),
        },
        "name": {"type": ["string", "null"], "description": "Snapshot name."},
        "description": {"type": ["string", "null"], "description": "Snapshot description."},
        "created": {
            "type": ["string", "null"],
            "description": "When the snapshot was taken (ISO 8601 date and time from vCenter).",
        },
        "power_state": {
            "type": ["string", "null"],
            "enum": ["poweredOn", "poweredOff", "suspended", None],
            "description": (
                "The VM power state saved in the snapshot. 'poweredOn': the snapshot holds the "
                "VM memory, and a revert brings the VM back running. 'poweredOff': disks only, "
                "and a revert leaves the VM powered off."
            ),
        },
        "quiesced": {
            "type": ["boolean", "null"],
            "description": "Whether VMware Tools quiesced the guest file systems for it.",
        },
        "current": {
            "type": "boolean",
            "description": (
                "True for vCenter's current snapshot: the one the VM's present state builds on."
            ),
        },
        "parent_snapshot_id": {
            "type": ["string", "null"],
            "description": "Moid of the parent snapshot; null at the root of the tree.",
        },
    },
    "required": ["snapshot_id", "current", "parent_snapshot_id"],
}


def _moref_value(ref: Any) -> str | None:
    """The moid of a VI-JSON ``ManagedObjectReference`` (or a bare moid), else ``None``."""
    if isinstance(ref, dict):
        value = ref.get("value")
        return value if isinstance(value, str) else None
    return ref if isinstance(ref, str) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def snapshot_rows(snapshot_info: Any) -> tuple[str | None, list[dict[str, Any]]]:
    """``(current snapshot id, rows)`` from the value of ``VirtualMachine.snapshot``.

    *snapshot_info* is the unwrapped property value. A VM without snapshots leaves
    the property unset, so ``None`` gives ``(None, [])``.
    """
    info = snapshot_info if isinstance(snapshot_info, dict) else {}
    current = _moref_value(info.get("currentSnapshot"))
    rows: list[dict[str, Any]] = []
    _walk(info.get("rootSnapshotList"), None, current, rows)
    return current, rows


def _walk(nodes: Any, parent: str | None, current: str | None, rows: list[dict[str, Any]]) -> None:
    """Append the rows of *nodes* and of their children, depth first."""
    # A list with one node arrives as a plain dict on the SOAP path of a single
    # ESXi host; VI-JSON always sends a list.
    if isinstance(nodes, dict):
        nodes = [nodes]
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        snapshot_id = _moref_value(node.get("snapshot"))
        if snapshot_id is not None:
            quiesced = node.get("quiesced")
            rows.append(
                {
                    "snapshot_id": snapshot_id,
                    "name": _text(node.get("name")),
                    "description": _text(node.get("description")),
                    "created": _text(node.get("createTime")),
                    "power_state": _text(node.get("state")),
                    "quiesced": quiesced if isinstance(quiesced, bool) else None,
                    "current": snapshot_id == current,
                    "parent_snapshot_id": parent,
                }
            )
        _walk(node.get("childSnapshotList"), snapshot_id or parent, current, rows)


def descendants(rows: list[dict[str, Any]], snapshot_id: str) -> list[dict[str, Any]]:
    """The rows below *snapshot_id* (children, their children, ...), in tree order.

    One pass is enough because :func:`snapshot_rows` puts every parent before its
    children.
    """
    below = {snapshot_id}
    out: list[dict[str, Any]] = []
    for row in rows:
        if row["parent_snapshot_id"] in below:
            below.add(row["snapshot_id"])
            out.append(row)
    return out

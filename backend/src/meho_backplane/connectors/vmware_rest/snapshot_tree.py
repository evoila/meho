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

__all__ = ["SNAPSHOT_ROW_SCHEMA", "descendants", "snapshot_rows", "snapshot_size"]

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


def _keys(disks: Any) -> list[int]:
    """The file keys of a ``VirtualMachineFileLayoutExDiskLayout[]``, chain by chain."""
    keys: list[int] = []
    for disk in disks if isinstance(disks, list) else []:
        chain = disk.get("chain") if isinstance(disk, dict) else None
        for link in chain if isinstance(chain, list) else []:
            file_keys = link.get("fileKey") if isinstance(link, dict) else None
            keys.extend(k for k in file_keys or [] if isinstance(k, int))
    return keys


def _remove_first(keys: list[int], key: int) -> None:
    if key in keys:
        keys.remove(key)


def _size(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _layouts(layout_ex: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``VirtualMachineFileLayoutExSnapshotLayout`` entries (a lone one may be a dict)."""
    layouts = layout_ex.get("snapshot")
    layouts = [layouts] if isinstance(layouts, dict) else layouts
    return [x for x in layouts if isinstance(x, dict)] if isinstance(layouts, list) else []


def _own_files(
    layouts: list[dict[str, Any]], snapshot_id: str, parent_id: str | None
) -> list[int] | None:
    """The file keys of the snapshot without its parent's; ``None`` when it has no layout."""
    wanted: list[int] | None = None
    parent_files: list[int] = []
    for layout in layouts:
        layout_id = _moref_value(layout.get("key"))
        if layout_id == snapshot_id:
            data_key = layout.get("dataKey")
            wanted = [data_key] if isinstance(data_key, int) else []
            wanted.extend(_keys(layout.get("disk")))
        elif parent_id is not None and layout_id == parent_id:
            parent_files.extend(_keys(layout.get("disk")))
    if wanted is not None:
        for key in parent_files:
            _remove_first(wanted, key)
    return wanted


def _running_files(layout_ex: dict[str, Any], layouts: list[dict[str, Any]]) -> list[int]:
    """The VM's disk files no snapshot has (the running delta disks)."""
    disk_files = _keys(layout_ex.get("disk"))
    for layout in layouts:
        for key in _keys(layout.get("disk")):
            _remove_first(disk_files, key)
    return disk_files


def snapshot_size(
    layout_ex: Any, snapshot_id: str, parent_id: str | None, *, current: bool
) -> int | None:
    """Bytes on disk of one snapshot, from ``VirtualMachine.layoutEx``; ``None`` when unknown.

    The same count as govc (``object.SnapshotSize`` in govmomi): the
    snapshot's ``.vmsn`` file and the disk files of its chains that its parent
    does not have; for a root snapshot, without the base disks. The current
    snapshot also counts the disk files no snapshot has yet (the running
    delta disks).
    """
    if not isinstance(layout_ex, dict) or not isinstance(layout_ex.get("file"), list):
        return None
    layouts = _layouts(layout_ex)
    wanted = _own_files(layouts, snapshot_id, parent_id)
    if wanted is None:
        return None
    by_key = {
        f["key"]: f
        for f in layout_ex["file"]
        if isinstance(f, dict) and isinstance(f.get("key"), int)
    }
    size = sum(
        _size(by_key.get(key, {}).get("size"))
        for key in wanted
        if parent_id is not None
        or by_key.get(key, {}).get("type") not in ("diskDescriptor", "diskExtent")
    )
    if current:
        size += sum(
            _size(by_key.get(k, {}).get("size")) for k in _running_files(layout_ex, layouts)
        )
    return size

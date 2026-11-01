# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.inventory.find`` typed op -- find vCenter objects by name, pattern or path.

Two ways, both through the VIM API on the connector's own session:

* by inventory path (``path``): one ``SearchIndex.FindByInventoryPath``. vCenter
  answers with the object, or with nothing. The path is the one govc and the
  vSphere Client show, e.g. ``<datacenter>/vm/<folder>/<vm-name>``.
* by name (``name``, exact) or name pattern (``name_pattern``): a container view
  of every managed entity below the root folder (see :mod:`.vim_view`), one
  paged read of ``name`` and ``parent``, and MEHO picks the matches. The same
  read gives the parents, so every match comes with its inventory path.

Each match is one flat row (kind, VIM type, moid, name, path), so a long answer
rides a result handle.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value
from meho_backplane.connectors.vmware_rest.vim_view import (
    VIEW_READ_CALLS,
    VimObject,
    moref_value,
    read_service_refs,
    read_view,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "INVENTORY_FIND_CALLS",
    "INVENTORY_GROUP_KEY",
    "INVENTORY_WHEN_TO_USE",
    "KIND_BY_VIM_TYPE",
    "TYPED_OPS",
    "VMWARE_INVENTORY_FIND_OP",
    "WHEN_TO_USE_BY_GROUP",
    "inventory_find_impl",
    "inventory_paths",
]

_log = structlog.get_logger(__name__)

OP_FIND_BY_INVENTORY_PATH: Final = "POST:/SearchIndex/{moId}/FindByInventoryPath"
#: The VIM calls of the operation.
INVENTORY_FIND_CALLS: Final[tuple[str, ...]] = (*VIEW_READ_CALLS, OP_FIND_BY_INVENTORY_PATH)

#: The kind MEHO reports for each VIM type. A type not in the map reports
#: ``other`` (its VIM type is in the row too).
KIND_BY_VIM_TYPE: Final[dict[str, str]] = {
    "VirtualMachine": "vm",
    "HostSystem": "host",
    "ClusterComputeResource": "cluster",
    "ComputeResource": "compute_resource",
    "Datastore": "datastore",
    "StoragePod": "datastore_cluster",
    "Network": "network",
    "DistributedVirtualPortgroup": "network",
    "OpaqueNetwork": "network",
    "VmwareDistributedVirtualSwitch": "distributed_switch",
    "Folder": "folder",
    "Datacenter": "datacenter",
    "ResourcePool": "resource_pool",
    "VirtualApp": "vapp",
}
_KINDS: Final = sorted(set(KIND_BY_VIM_TYPE.values()))

#: Longest parent chain followed for a path: a guard against a loop.
_MAX_DEPTH: Final = 64

INVENTORY_GROUP_KEY: Final = "vmware-inventory"
INVENTORY_WHEN_TO_USE: Final = (
    "Use to see what a vCenter holds. Reads only, no changes: vmware.vm.list lists "
    "every VM (or the VMs of one cluster, host or folder) with power state, host, "
    "cluster, guest OS, IP address, CPUs, memory, storage, VMware Tools status and "
    "snapshot count; vmware.inventory.find finds VMs, hosts, clusters, datastores, "
    "networks, folders and other objects by name, name pattern or inventory path and "
    "returns their moid and path. Both read the VIM API directly, with no catalog "
    "ingest. Use the moids they return with the 'vm', 'host' or 'cluster' operations."
)


def _segment(name: str) -> str:
    """A name as one inventory path segment: a '/' in it is written ``%2f``."""
    return name.replace("%", "%25").replace("/", "%2f")


def inventory_paths(objects: list[VimObject], root_folder: str) -> dict[str, str]:
    """``{moid: inventory path}`` for every object whose parents lead to the root folder."""
    by_moid = {obj.moid: obj for obj in objects}
    paths: dict[str, str] = {}
    for obj in objects:
        parts: list[str] = []
        current: VimObject | None = obj
        for _ in range(_MAX_DEPTH):
            if current is None:
                break
            name = current.props.get("name")
            parts.append(_segment(name) if isinstance(name, str) else current.moid)
            parent = moref_value(current.props.get("parent"))
            if parent is None or parent == root_folder:
                paths[obj.moid] = "/".join(reversed(parts))
                break
            current = by_moid.get(parent)
    return paths


def _row(mo_type: str, moid: str, name: str | None, path: str | None) -> dict[str, Any]:
    return {
        "kind": KIND_BY_VIM_TYPE.get(mo_type, "other"),
        "vim_type": mo_type,
        "id": moid,
        "name": name,
        "path": path,
    }


async def _find_by_path(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    search_index: str,
    path: str,
) -> list[dict[str, Any]]:
    found = await connector._post_vmomi_json(
        target,
        f"/SearchIndex/{search_index}/FindByInventoryPath",
        operator=operator,
        json={"inventoryPath": path},
    )
    ref = unwrap_vim_value(found)
    moid = moref_value(ref)
    if moid is None or not isinstance(ref, dict):
        return []
    clean = path.strip("/")
    name = clean.rsplit("/", 1)[-1].replace("%2f", "/").replace("%25", "%")
    return [_row(str(ref.get("type") or ""), moid, name or None, clean)]


def _name_matches(name: Any, params: dict[str, Any]) -> bool:
    if not isinstance(name, str):
        return False
    if isinstance(params.get("name"), str):
        return bool(name == params["name"])
    pattern = str(params["name_pattern"])
    return fnmatch.fnmatchcase(name.casefold(), pattern.casefold())


async def inventory_find_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.inventory.find``.

    Returns ``{match_count, matches}``; the matches are sorted by path. No
    match is an empty list, not an error.
    """
    refs = await read_service_refs(connector, target, operator)
    path = params.get("path")
    if isinstance(path, str):
        rows = await _find_by_path(connector, target, operator, refs.search_index, path)
    else:
        objects = await read_view(
            connector,
            target,
            operator,
            view_manager=refs.view_manager,
            container=("Folder", refs.root_folder),
            types=(),
            prop_type="ManagedEntity",
            path_set=("name", "parent"),
        )
        paths = inventory_paths(objects, refs.root_folder)
        rows = [
            _row(obj.mo_type, obj.moid, obj.props.get("name"), paths.get(obj.moid))
            for obj in objects
            if _name_matches(obj.props.get("name"), params)
        ]
    kind = params.get("type")
    if isinstance(kind, str):
        rows = [row for row in rows if row["kind"] == kind]
    rows.sort(key=lambda r: (r["path"] or "", r["id"]))
    _log.info("vmware_inventory_find_read", target=target.name, match_count=len(rows))
    return {"match_count": len(rows), "matches": rows}


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "Exact object name (upper and lower case count). Give one of name, "
            "name_pattern and path.",
        },
        "name_pattern": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name pattern: '*' is any text, '?' one character; upper and lower case are "
                "the same. E.g. 'esx-*'. Give one of name, name_pattern and path."
            ),
        },
        "path": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Inventory path, e.g. '<datacenter>/vm/<folder>/<vm-name>' or "
                "'<datacenter>/host/<cluster>'. Write a '/' inside a name as '%2f'. Give one "
                "of name, name_pattern and path."
            ),
        },
        "type": {
            "type": "string",
            "enum": _KINDS,
            "description": "Only objects of this kind, e.g. 'vm', 'host' or 'datastore'.",
        },
    },
    "oneOf": [
        {"required": ["name"]},
        {"required": ["name_pattern"]},
        {"required": ["path"]},
    ],
    "additionalProperties": False,
    "examples": [{"name": "<vm-name>", "type": "vm"}],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "match_count": {"type": "integer", "description": "Number of rows in matches."},
        "matches": {
            "type": "array",
            "description": "The objects found, sorted by path.",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [*_KINDS, "other"],
                        "description": "What the object is, e.g. 'vm' or 'cluster'.",
                    },
                    "vim_type": {
                        "type": "string",
                        "description": "The VIM type, e.g. 'VirtualMachine'.",
                    },
                    "id": {"type": "string", "description": "The moid, e.g. 'vm-42'."},
                    "name": {"type": ["string", "null"], "description": "Object name."},
                    "path": {
                        "type": ["string", "null"],
                        "description": "Inventory path from the datacenter down.",
                    },
                },
                "required": ["kind", "vim_type", "id", "name", "path"],
            },
        },
    },
    "required": ["match_count", "matches"],
}

VMWARE_INVENTORY_FIND_OP = VmwareTypedOp(
    op_id="vmware.inventory.find",
    handler_attr="inventory_find",
    summary="Find vCenter objects (VM, host, cluster, datastore, network, folder) by name or path.",
    description=(
        "Finds objects of any kind by exact name, name pattern or inventory path. Returns "
        "each match's kind, moid, name and inventory path. An optional type keeps one kind "
        "only. No match is an empty list. Read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=INVENTORY_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "inventory", "search"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call when you know an object's name or path and need its moid, its kind or "
            "where it sits in the inventory. Most operations take a moid, not a name."
        ),
        "parameter_hints": {
            "name": "The exact name, e.g. a VM name.",
            "name_pattern": "A pattern like 'web-*' when you know part of the name.",
            "path": "A full inventory path from the datacenter, as govc prints it.",
            "type": "Set it when names repeat across kinds, e.g. 'vm' or 'datastore'.",
        },
        "output_shape": (
            "{match_count, matches: [{kind, vim_type, id, name, path}]}. A long list comes "
            "back as a result handle: read it with result_query."
        ),
        "next_step": (
            "Pass the id (moid) to the next operation, e.g. vmware.vm.info for a VM. Two "
            "matches with one name are two objects: tell them apart by path."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_INVENTORY_FIND_OP,)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {INVENTORY_GROUP_KEY: INVENTORY_WHEN_TO_USE}

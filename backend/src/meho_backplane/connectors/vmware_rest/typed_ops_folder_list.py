# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.folder.list`` typed op: the inventory folders of a vCenter.

One flat row per folder: id, name, type, the datacenter it is in, and its
parent. Filters: folder type, datacenters, parent folders and exact names.

It is a ``source_kind="typed"`` bound method on :class:`VmwareRestConnector`,
like ``vmware.host.usage``: it reads on the connector session, so it works on
a fresh install with no spec ingest. A long list is stored behind a result
handle by the dispatcher, like every other set-shaped result.

Three reads:

1. ``GET /vcenter/folder`` with the filters. This read must work; its error
   is the error of the operation. vCenter answers with at most 1000 folders
   and refuses a filter that matches more.
2. The datacenter of each folder. The folder summary has no datacenter, so
   the op lists the datacenters (``GET /vcenter/datacenter``) and lists the
   folders again once per datacenter, with the same filters and that
   datacenter. A folder found there is in that datacenter.
3. The parent of each folder: the ``parent`` property of the ``Folder``
   objects, read with one ``PropertyCollector.RetrievePropertiesEx`` (every
   page). The summary has no parent either.

Reads 2 and 3 are best effort: when one fails, its fields are ``null`` and
``read_note`` says which read failed. The folder list itself stays. Read 3
can also fail for single folders inside a successful answer: vCenter then
puts the ``parent`` property in that object's ``missingSet`` with a fault
(for example no permission). Such a folder gets ``null`` parent fields and
``read_note`` names the fault type, so it does not look like a folder with
no parent. Only the fault type name is reported, never the fault text, which
can echo a private identifier (#3708).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value
from meho_backplane.connectors.vmware_rest.vim_body import (
    fault_type_name,
    retrieve_properties_body,
    unwrap_vim_value,
)
from meho_backplane.connectors.vmware_rest.vim_retrieve import retrieve_all_pages

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "FOLDER_LIST_GROUP_KEY",
    "FOLDER_LIST_WHEN_TO_USE",
    "FOLDER_TYPES",
    "VMWARE_FOLDER_LIST_OP",
    "folder_list_impl",
]

_log = structlog.get_logger(__name__)

#: Spec-relative REST paths; ``mount_op_path`` puts them on ``/api`` or ``/rest``.
FOLDER_LIST_PATH: Final = "/vcenter/folder"
DATACENTER_LIST_PATH: Final = "/vcenter/datacenter"

#: ``Vcenter.Folder.Type`` of the vCenter REST API.
FOLDER_TYPES: Final = ("DATACENTER", "DATASTORE", "HOST", "NETWORK", "VIRTUAL_MACHINE")

_FOLDER_MO_TYPE: Final = "Folder"
_PROP_PARENT: Final = "parent"

#: Operation parameter -> ``Vcenter.Folder.FilterSpec`` field. Written in the
#: legacy ``filter.*`` form; ``adapt_op_query`` drops the prefix on ``/api``.
_FILTER_KEYS: Final = {
    "type": "filter.type",
    "names": "filter.names",
    "parent_folders": "filter.parent_folders",
    "datacenters": "filter.datacenters",
}

FOLDER_LIST_GROUP_KEY: Final = "vmware-folders"
FOLDER_LIST_WHEN_TO_USE: Final = (
    "Use to list the inventory folders of a vCenter: VM, host, datastore, network "
    "and datacenter folders, with the datacenter and the parent of each. The right "
    "op to find a folder id before you create, move or delete something in a folder. "
    "Read-only."
)


def _folder_query(params: dict[str, Any], datacenter: str | None = None) -> dict[str, Any]:
    """The ``filter.*`` query of one folder listing, from the op parameters."""
    query: dict[str, Any] = {}
    for param, key in _FILTER_KEYS.items():
        value = params.get(param)
        if value:
            query[key] = value
    if datacenter is not None:
        query["filter.datacenters"] = [datacenter]
    return query


async def _get_list(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    path: str,
    query: dict[str, Any],
) -> list[dict[str, Any]]:
    """One REST list call on the target's mount; the rows that are objects."""
    mounted = await connector.mount_op_path(target, path, operator)
    adapted = await connector.adapt_op_query(target, query, operator)
    payload = _unwrap_value(
        await connector._get_json(target, mounted, operator=operator, params=adapted)
    )
    if not isinstance(payload, list):
        raise RuntimeError(
            f"vmware.folder.list: expected a list from GET {path!r}, got {type(payload).__name__}"
        )
    return [row for row in payload if isinstance(row, dict)]


async def _datacenter_of_each_folder(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, tuple[str, Any]]:
    """Folder id -> (datacenter id, datacenter name), from one listing per datacenter."""
    dc_query = {"filter.datacenters": params["datacenters"]} if params.get("datacenters") else {}
    datacenters = await _get_list(connector, operator, target, DATACENTER_LIST_PATH, dc_query)
    found: dict[str, tuple[str, Any]] = {}
    for dc in datacenters:
        dc_id = dc.get("datacenter")
        if not isinstance(dc_id, str):
            continue
        query = _folder_query(params, datacenter=dc_id)
        for row in await _get_list(connector, operator, target, FOLDER_LIST_PATH, query):
            if isinstance(row.get("folder"), str):
                found[row["folder"]] = (dc_id, dc.get("name"))
    return found


async def _parent_of_each_folder(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    folder_ids: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """``(parents, unreadable)`` of the asked folders.

    *parents* is folder id -> parent reference ``{type, value}``; a folder
    without a parent is left out. *unreadable* is folder id -> fault type
    name for each folder whose ``parent`` vCenter could not read: the HTTP
    call succeeded, but the property is in that object's ``missingSet``
    with a fault instead of in ``propSet``.
    """
    body = retrieve_properties_body(_FOLDER_MO_TYPE, folder_ids, (_PROP_PARENT,))
    result = await retrieve_all_pages(connector, target, operator, body)
    parents: dict[str, dict[str, Any]] = {}
    unreadable: dict[str, str] = {}
    for obj in result["objects"]:
        ref = obj.get("obj")
        moid = ref.get("value") if isinstance(ref, dict) else None
        if not isinstance(moid, str):
            continue
        for prop in obj.get("propSet") or []:
            if not isinstance(prop, dict) or prop.get("name") != _PROP_PARENT:
                continue
            parent = unwrap_vim_value(prop.get("val"))
            if isinstance(parent, dict) and isinstance(parent.get("value"), str):
                parents[moid] = parent
        for miss in obj.get("missingSet") or []:
            if isinstance(miss, dict) and miss.get("path") == _PROP_PARENT:
                unreadable[moid] = fault_type_name(miss.get("fault")) or "unknown fault"
    return parents, unreadable


def _failed(read: str, exc: Exception) -> str:
    return f"{read} failed with {type(exc).__name__}: {exc}"


def _unreadable_note(unreadable: dict[str, str]) -> str:
    """One note for the folders whose parent vCenter could not read, by fault type."""
    faults = ", ".join(sorted(set(unreadable.values())))
    count = len(unreadable)
    return f"the parent read failed for {count} folder(s) with {faults}"


async def folder_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.folder.list``.

    Returns ``{"folders": [{id, name, type, datacenter_id, datacenter_name,
    parent_id, parent_type}], "read_note"}``. See the module docstring for the
    three reads.
    """
    summaries = await _get_list(
        connector, operator, target, FOLDER_LIST_PATH, _folder_query(params)
    )
    folder_ids = [row["folder"] for row in summaries if isinstance(row.get("folder"), str)]

    notes: list[str] = []
    datacenters: dict[str, tuple[str, Any]] | None = None
    parents: dict[str, dict[str, Any]] | None = None
    if folder_ids:
        try:
            datacenters = await _datacenter_of_each_folder(connector, operator, target, params)
        except (httpx.HTTPError, RuntimeError) as exc:
            notes.append(_failed("the datacenter read", exc))
        try:
            parents, unreadable = await _parent_of_each_folder(
                connector, operator, target, folder_ids
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            notes.append(_failed("the parent read", exc))
        else:
            if unreadable:
                notes.append(_unreadable_note(unreadable))

    folders: list[dict[str, Any]] = []
    for row in summaries:
        folder_id = row.get("folder")
        if not isinstance(folder_id, str):
            continue
        dc_id, dc_name = (datacenters or {}).get(folder_id, (None, None))
        parent = (parents or {}).get(folder_id) or {}
        folders.append(
            {
                "id": folder_id,
                "name": row.get("name"),
                "type": row.get("type"),
                "datacenter_id": dc_id,
                "datacenter_name": dc_name,
                "parent_id": parent.get("value"),
                "parent_type": parent.get("type"),
            }
        )
    _log.info(
        "vmware_folder_list_read",
        target=target.name,
        folder_count=len(folders),
        complete=not notes,
    )
    return {"folders": folders, "read_note": "; ".join(notes) or None}


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------


def _id_list(what: str) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 128},
        "minItems": 1,
        "uniqueItems": True,
        "description": what,
    }


_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": list(FOLDER_TYPES),
            "description": (
                "Only folders of this type: VIRTUAL_MACHINE (VMs and templates), HOST "
                "(hosts and clusters), DATASTORE, NETWORK, or DATACENTER (folders that hold "
                "datacenters). Leave out for all types."
            ),
        },
        "datacenters": _id_list(
            "Only folders in these datacenters, by datacenter id (for example "
            "datacenter-3). Leave out for all datacenters."
        ),
        "parent_folders": _id_list(
            "Only folders directly inside these folders, by folder id (for example "
            "group-v4). Leave out for any parent."
        ),
        "names": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 255},
            "minItems": 1,
            "uniqueItems": True,
            "description": (
                "Only folders with one of these names. The match is exact (vCenter does "
                "not match parts of a name). Leave out for any name."
            ),
        },
    },
    "additionalProperties": False,
    "examples": [{}, {"type": "VIRTUAL_MACHINE", "datacenters": ["<datacenter-id>"]}],
}

_FOLDER_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Folder id (managed object id), e.g. group-v4."},
        "name": {"type": ["string", "null"], "description": "Name of the folder."},
        "type": {
            "type": ["string", "null"],
            "enum": [*FOLDER_TYPES, None],
            "description": (
                "What the folder can hold: VMs, hosts, datastores, networks or datacenters."
            ),
        },
        "datacenter_id": {
            "type": ["string", "null"],
            "description": (
                "Id of the datacenter the folder is in. null for folders above the "
                "datacenters (the root folder, DATACENTER folders) or when the read failed."
            ),
        },
        "datacenter_name": {
            "type": ["string", "null"],
            "description": "Name of that datacenter; null like datacenter_id.",
        },
        "parent_id": {
            "type": ["string", "null"],
            "description": (
                "Id of the parent object; null for the root folder or when the read failed "
                "(for the whole list, or for this folder: see read_note)."
            ),
        },
        "parent_type": {
            "type": ["string", "null"],
            "description": (
                "Type of the parent object: Folder, or Datacenter for the top folders of a "
                "datacenter."
            ),
        },
    },
    "required": ["id", "name", "type", "datacenter_id", "parent_id"],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "folders": {
            "type": "array",
            "description": "One row per folder, in the order vCenter returns them.",
            "items": _FOLDER_ROW_SCHEMA,
        },
        "read_note": {
            "type": ["string", "null"],
            "description": (
                "Which extra read (datacenter or parent) failed, or for how many folders "
                "vCenter could not read the parent and with which fault; null when all worked."
            ),
        },
    },
    "required": ["folders", "read_note"],
}

VMWARE_FOLDER_LIST_OP = VmwareTypedOp(
    op_id="vmware.folder.list",
    handler_attr="folder_list",
    summary="List the inventory folders of a vCenter, with datacenter and parent.",
    description=(
        "Returns one row per folder: id, name, type (VM, host, datastore, network or "
        "datacenter folder), the datacenter it is in and its parent. Filters: type, "
        "datacenters, parent folders and exact names. Reads the vSphere API directly, so "
        "it needs no catalog ingest. Read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=FOLDER_LIST_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "folder", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see the folders of a vCenter, or to find the id of a folder by its "
            "name, type or datacenter. Folder writes and VM creates take a folder id."
        ),
        "parameter_hints": {
            "type": "VIRTUAL_MACHINE for VM folders, HOST for host and cluster folders.",
            "datacenters": "Datacenter ids, e.g. ['datacenter-3'].",
            "parent_folders": "Folder ids, e.g. ['group-v4']; lists the folders right inside.",
            "names": "Exact folder names; no wildcards.",
        },
        "output_shape": (
            "{folders: [{id, name, type: DATACENTER|DATASTORE|HOST|NETWORK|VIRTUAL_MACHINE, "
            "datacenter_id, datacenter_name, parent_id, parent_type: Folder|Datacenter}], "
            "read_note}. A long list comes back as a result handle; read it with "
            "result_query. When the datacenter or parent read failed, those fields are null "
            "and read_note says which read. When vCenter could not read the parent of some "
            "folders (for example no permission), their parent fields are null and read_note "
            "says how many and why; a null parent with a null read_note is the root folder."
        ),
        "next_step": (
            "Use the folder id in vmware.composite.folder.create (as the parent) or "
            "vmware.composite.folder.delete. vCenter returns at most 1000 folders: if it "
            "refuses the call, narrow it with type or datacenters."
        ),
    },
)

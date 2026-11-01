# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.snapshot.list`` typed op -- the snapshots of one VM, or of every VM.

The vCenter REST API has no snapshot paths, so this read uses the VIM API. It is
a ``source_kind="typed"`` bound method on :class:`VmwareRestConnector`, so it
works on a fresh install with no catalog ingest.

* One VM (``vm`` set): one ``PropertyCollector.RetrievePropertiesEx`` of the
  VM's ``name``, ``snapshot`` and ``layoutEx`` properties, the same read path
  ``vmware.vm.info`` uses. A missing VM is a ``not_found`` result. A
  ``snapshot`` property vCenter could not read (a ``missingSet`` entry, for
  example no permission) fails the call: an empty list would wrongly say "this
  VM has no snapshots".
* Every VM (``vm`` left out): a container view of all VMs (see
  :mod:`.vim_view`) reads ``name`` and ``snapshot``; then ``layoutEx`` is read
  for the VMs that have snapshots only. A VM whose snapshots could not be read
  is left out and counted in ``read_note``.

The snapshot tree comes back as a flat list in tree order (see
:mod:`~meho_backplane.connectors.vmware_rest.snapshot_tree`). Each row also has
its VM, its age in days and its size on disk (from ``layoutEx``, counted like
govc; ``null`` when vCenter does not report the files). ``older_than_days``
keeps only the snapshots taken longer ago than that. A long list rides a
result handle like any other list.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.base import ConnectorResourceNotFoundError, ConnectorUpstreamError
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.snapshot_tree import (
    SNAPSHOT_ROW_SCHEMA,
    snapshot_rows,
    snapshot_size,
)
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value
from meho_backplane.connectors.vmware_rest.vim_body import (
    retrieve_properties_body,
    unwrap_vim_value,
)
from meho_backplane.connectors.vmware_rest.vim_view import (
    VIEW_READ_CALLS,
    read_props,
    read_service_refs,
    read_view,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "SNAPSHOT_LIST_CALLS",
    "SNAPSHOT_LIST_GROUP_KEY",
    "SNAPSHOT_LIST_WHEN_TO_USE",
    "TYPED_OPS",
    "VMWARE_SNAPSHOT_LIST_OP",
    "WHEN_TO_USE_BY_GROUP",
    "snapshot_list_impl",
]

_log = structlog.get_logger(__name__)

#: PropertyCollector ``RetrievePropertiesEx`` on the singleton ``propertyCollector``
#: moId (carried in the path, so the body is only the method arguments).
_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
#: The VIM calls of the operation (the one-VM read is one of them).
SNAPSHOT_LIST_CALLS: Final = VIEW_READ_CALLS
_VIRTUAL_MACHINE_MO_TYPE = "VirtualMachine"
_PROP_NAME = "name"
_PROP_SNAPSHOT = "snapshot"
_PROP_LAYOUT_EX = "layoutEx"
_SECONDS_PER_DAY: Final = 86400

#: Errors of the best-effort ``layoutEx`` read of the all-VMs list.
_SIZE_READ_ERRORS: Final = (httpx.HTTPError, RuntimeError, ConnectorUpstreamError)

SNAPSHOT_LIST_GROUP_KEY = "vmware-snapshot-list"


def _now() -> datetime:
    """The current time; tests replace it."""
    return datetime.now(UTC)


def _vm_object(retrieve_result: Any, vm_moid: str) -> tuple[dict[str, Any], set[str]] | None:
    """``(properties, unreadable paths)`` of the VM in the answer; ``None`` when it is absent."""
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    for obj in objects if isinstance(objects, list) else []:
        if not isinstance(obj, dict):
            continue
        ref = obj.get("obj")
        if isinstance(ref, dict) and ref.get("value") != vm_moid:
            continue
        props = {
            entry["name"]: unwrap_vim_value(entry.get("val"))
            for entry in obj.get("propSet") or []
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
        missing = {
            entry["path"]
            for entry in obj.get("missingSet") or []
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        return props, missing
    return None


def _age_days(created: Any, now: datetime) -> int | None:
    """Whole days since *created* (an ISO 8601 time from vCenter); ``None`` when unknown."""
    if not isinstance(created, str):
        return None
    try:
        taken = datetime.fromisoformat(created)
    except ValueError:
        return None
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=UTC)
    return int((now - taken).total_seconds() // _SECONDS_PER_DAY)


def _older_than(created: Any, now: datetime, days: int) -> bool:
    if not isinstance(created, str):
        return False
    try:
        taken = datetime.fromisoformat(created)
    except ValueError:
        return False
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=UTC)
    return (now - taken).total_seconds() > days * _SECONDS_PER_DAY


def _vm_rows(
    vm: str, props: dict[str, Any], now: datetime, older_than_days: int | None
) -> tuple[str | None, list[dict[str, Any]]]:
    """``(current snapshot id, rows)`` of one VM, with VM, age, memory and size."""
    name = props.get(_PROP_NAME)
    layout_ex = props.get(_PROP_LAYOUT_EX)
    current, rows = snapshot_rows(props.get(_PROP_SNAPSHOT))
    out: list[dict[str, Any]] = []
    for row in rows:
        if older_than_days is not None and not _older_than(row["created"], now, older_than_days):
            continue
        out.append(
            {
                "vm": vm,
                "vm_name": name if isinstance(name, str) else None,
                **row,
                "age_days": _age_days(row["created"], now),
                "memory": None
                if row["power_state"] is None
                else row["power_state"] != "poweredOff",
                "size_bytes": snapshot_size(
                    layout_ex, row["snapshot_id"], row["parent_snapshot_id"], current=row["current"]
                ),
            }
        )
    return current, out


async def _one_vm(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    vm_moid: str,
    older_than_days: int | None,
) -> dict[str, Any]:
    result = await connector._post_vmomi_json(
        target,
        _RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=retrieve_properties_body(
            _VIRTUAL_MACHINE_MO_TYPE, [vm_moid], (_PROP_NAME, _PROP_SNAPSHOT, _PROP_LAYOUT_EX)
        ),
        promote_managed_object_not_found=True,
    )
    found = _vm_object(result, vm_moid)
    if found is None:
        raise ConnectorResourceNotFoundError([vm_moid], f"vmware.snapshot.list: no VM {vm_moid!r}")
    props, missing = found
    if _PROP_SNAPSHOT in missing:
        raise RuntimeError(
            f"vmware.snapshot.list: vCenter could not read the snapshots of VM {vm_moid!r} "
            "(missing permission or an unreadable VM)"
        )
    current, rows = _vm_rows(vm_moid, props, _now(), older_than_days)
    name = props.get(_PROP_NAME)
    return {
        "vm": vm_moid,
        "vm_name": name if isinstance(name, str) else None,
        "current_snapshot_id": current,
        "snapshot_count": len(rows),
        "read_note": None,
        "snapshots": rows,
    }


async def _all_vms(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    older_than_days: int | None,
) -> dict[str, Any]:
    refs = await read_service_refs(connector, target, operator)
    vms = await read_view(
        connector,
        target,
        operator,
        view_manager=refs.view_manager,
        container=("Folder", refs.root_folder),
        types=(_VIRTUAL_MACHINE_MO_TYPE,),
        prop_type=_VIRTUAL_MACHINE_MO_TYPE,
        path_set=(_PROP_NAME, _PROP_SNAPSHOT),
    )
    unreadable = sum(1 for vm in vms if _PROP_SNAPSHOT in vm.missing)
    with_snapshots = [
        vm for vm in vms if _PROP_SNAPSHOT not in vm.missing and vm.props.get(_PROP_SNAPSHOT)
    ]
    notes: list[str] = []
    if unreadable:
        notes.append(f"the snapshots of {unreadable} VM(s) could not be read; they are left out")
    layouts: dict[str, Any] = {}
    try:
        read = await read_props(
            connector,
            target,
            operator,
            mo_type=_VIRTUAL_MACHINE_MO_TYPE,
            moids=sorted(vm.moid for vm in with_snapshots),
            path_set=(_PROP_LAYOUT_EX,),
        )
        layouts = {moid: obj.props.get(_PROP_LAYOUT_EX) for moid, obj in read.items()}
    except _SIZE_READ_ERRORS as exc:
        _log.info("vmware_snapshot_list_sizes_not_read", error=type(exc).__name__)
        notes.append(f"snapshot sizes could not be read ({type(exc).__name__})")
    now = _now()
    rows: list[dict[str, Any]] = []
    for vm in with_snapshots:
        props = {**vm.props, _PROP_LAYOUT_EX: layouts.get(vm.moid)}
        rows.extend(_vm_rows(vm.moid, props, now, older_than_days)[1])
    rows.sort(key=lambda r: ((r["vm_name"] or "").casefold(), r["vm"]))
    return {
        "vm": None,
        "vm_name": None,
        "current_snapshot_id": None,
        "snapshot_count": len(rows),
        "read_note": "; ".join(notes) or None,
        "snapshots": rows,
    }


async def snapshot_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.snapshot.list`` -- one VM, or every VM.

    Returns ``{vm, vm_name, current_snapshot_id, snapshot_count, read_note,
    snapshots}``; ``vm``, ``vm_name`` and ``current_snapshot_id`` are ``null``
    for the list of every VM.
    """
    older = params.get("older_than_days")
    older_than_days = older if isinstance(older, int) and not isinstance(older, bool) else None
    vm_moid = params.get("vm")
    if isinstance(vm_moid, str):
        out = await _one_vm(connector, operator, target, vm_moid, older_than_days)
    else:
        out = await _all_vms(connector, operator, target, older_than_days)
    _log.info(
        "vmware_snapshot_list_read",
        target=target.name,
        vm=out["vm"],
        snapshot_count=out["snapshot_count"],
    )
    return out


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Managed-object ID of one VM, e.g. 'vm-42' (vmware.vm.info finds it from the "
                "VM name). Leave it out to list the snapshots of every VM."
            ),
        },
        "older_than_days": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Only the snapshots taken more than this many days ago, e.g. 7 for the "
                "snapshots older than a week."
            ),
        },
    },
    "additionalProperties": False,
    "examples": [{"vm": "<vm-id>"}, {"older_than_days": 7}],
}

_LIST_ROW_SCHEMA: dict[str, Any] = {
    **SNAPSHOT_ROW_SCHEMA,
    "properties": {
        "vm": {"type": "string", "description": "Moid of the VM the snapshot belongs to."},
        "vm_name": {"type": ["string", "null"], "description": "Name of that VM."},
        **SNAPSHOT_ROW_SCHEMA["properties"],
        "age_days": {
            "type": ["integer", "null"],
            "description": "Whole days since the snapshot was taken.",
        },
        "memory": {
            "type": ["boolean", "null"],
            "description": "True when the snapshot holds the VM memory (it was taken running).",
        },
        "size_bytes": {
            "type": ["integer", "null"],
            "description": (
                "Space the snapshot takes on the datastore, in bytes, counted like govc; null "
                "when vCenter does not report the VM's files."
            ),
        },
    },
    "required": [*SNAPSHOT_ROW_SCHEMA["required"], "vm", "age_days", "size_bytes"],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {
            "type": ["string", "null"],
            "description": "Moid of the VM read; null for the list of every VM.",
        },
        "vm_name": {"type": ["string", "null"], "description": "VM name; null for every VM."},
        "current_snapshot_id": {
            "type": ["string", "null"],
            "description": "Moid of the one VM's current snapshot; null when none or every VM.",
        },
        "snapshot_count": {"type": "integer", "description": "Number of rows in snapshots."},
        "read_note": {
            "type": ["string", "null"],
            "description": "Set when some snapshots or sizes could not be read.",
        },
        "snapshots": {
            "type": "array",
            "items": _LIST_ROW_SCHEMA,
            "description": (
                "The snapshots, VM by VM, each VM in tree order: a parent comes before its "
                "children."
            ),
        },
    },
    "required": ["vm", "current_snapshot_id", "snapshot_count", "snapshots"],
}

#: Curated ``when_to_use`` blurb for the snapshot-list group.
SNAPSHOT_LIST_WHEN_TO_USE = (
    "Use to see snapshots: of one VM, or of every VM in one call, optionally only those "
    "older than N days. Each row has the VM, the snapshot id, name, description, time "
    "taken, age in days, whether it holds memory, quiesced or not, its parent, whether "
    "it is current and its size on disk. vCenter's REST API has no snapshot paths; this "
    "reads the VIM API, with no catalog ingest. Read-only. To take, delete, delete all, "
    "revert or consolidate, use the 'vm' group: vmware.composite.vm.snapshot.create, "
    ".delete, .delete_all and .revert, and vmware.composite.vm.disks.consolidate."
)

VMWARE_SNAPSHOT_LIST_OP = VmwareTypedOp(
    op_id="vmware.snapshot.list",
    handler_attr="snapshot_list",
    summary="List the snapshots of one VM or of every VM, with age and size; filter by age.",
    description=(
        "Returns snapshots as a flat list: of one VM (by moid) or of every VM. Each row has "
        "the VM, id, name, time taken, age in days, memory or not, quiesced, parent, current "
        "and size on disk. older_than_days keeps old ones only. Read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=SNAPSHOT_LIST_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vm", "snapshot"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which snapshots a VM has (before you take, delete or revert one), "
            "or leave vm out to find old or large snapshots across all VMs."
        ),
        "parameter_hints": {
            "vm": "VM moid, e.g. 'vm-42'. Call vmware.vm.info with the VM name to get it.",
            "older_than_days": "E.g. 7 for snapshots older than a week.",
        },
        "output_shape": (
            "{vm, vm_name, current_snapshot_id, snapshot_count, read_note, snapshots: [{vm, "
            "vm_name, snapshot_id, name, description, created, power_state, quiesced, "
            "current, parent_snapshot_id, age_days, memory, size_bytes}]}. A long list "
            "comes back as a result handle: read it with result_query."
        ),
        "next_step": (
            "Pass a snapshot_id to vmware.composite.vm.snapshot.delete or .revert; to drop "
            "every snapshot of a VM, call vmware.composite.vm.snapshot.delete_all. Old "
            "snapshots slow the VM and fill the datastore: suggest deleting the ones nobody "
            "needs."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_SNAPSHOT_LIST_OP,)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {SNAPSHOT_LIST_GROUP_KEY: SNAPSHOT_LIST_WHEN_TO_USE}

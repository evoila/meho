# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.vm.list`` typed op -- every VM of a vCenter, one row each, in one call.

The vCenter REST list (``GET /api/vcenter/vm``) stops at 4,000 VMs and has no
guest OS, IP address, storage, host or snapshot data. So this read uses the
VIM API, the way govc and PowerCLI do (see :mod:`.vim_view`):

1. a container view of ``VirtualMachine`` below the root folder, or below the
   one cluster, host or folder the caller names;
2. one paged ``RetrievePropertiesEx`` of the VM properties of every row;
3. the names of the hosts, of their clusters and of the VM folders, read for
   the hosts and folders of the listed VMs only. This read is best-effort:
   when it fails the names are ``null`` and ``read_note`` says why.

The power state and the name pattern filters run in MEHO, after the read.
The rows are flat, so a long list rides a result handle (read it with
``result_query``).
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.connectors.base import ConnectorUpstreamError
from meho_backplane.connectors.vmware_rest.snapshot_tree import snapshot_rows
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp
from meho_backplane.connectors.vmware_rest.typed_ops_inventory_find import INVENTORY_GROUP_KEY
from meho_backplane.connectors.vmware_rest.vim_view import (
    VIEW_READ_CALLS,
    VimObject,
    moref_value,
    read_props,
    read_service_refs,
    read_view,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "VMWARE_VM_LIST_OP",
    "VM_LIST_CALLS",
    "VM_LIST_PROPERTIES",
    "vm_list_impl",
]

_log = structlog.get_logger(__name__)

#: The VIM calls of the operation (all from :mod:`.vim_view`).
VM_LIST_CALLS: Final = VIEW_READ_CALLS

_VM: Final = "VirtualMachine"
_CLUSTER: Final = "ClusterComputeResource"

#: The VM properties read for every row.
_PROP_SNAPSHOT: Final = "snapshot"

VM_LIST_PROPERTIES: Final[tuple[str, ...]] = (
    "name",
    "parent",
    "summary.runtime.powerState",
    "summary.runtime.host",
    "summary.config.guestFullName",
    "summary.config.numCpu",
    "summary.config.memorySizeMB",
    "summary.config.template",
    "summary.guest.ipAddress",
    "summary.guest.toolsStatus",
    "summary.storage.committed",
    "summary.storage.uncommitted",
    _PROP_SNAPSHOT,
)

#: The parameter that names the scope, and the VIM type of its moid.
_SCOPES: Final[dict[str, str]] = {
    "cluster": _CLUSTER,
    "host": "HostSystem",
    "folder": "Folder",
}

#: Errors of the best-effort name read.
_NAME_READ_ERRORS: Final = (httpx.HTTPError, RuntimeError, ConnectorUpstreamError)


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _row(vm: VimObject) -> dict[str, Any]:
    """One output row from the VM's properties (names are filled in later).

    ``snapshot_count`` is ``None`` when vCenter could not read the ``snapshot``
    property (it is in the object's ``missingSet``): a failed read must not
    look like a VM without snapshots.
    """
    p = vm.props
    committed = _int(p.get("summary.storage.committed"))
    uncommitted = _int(p.get("summary.storage.uncommitted"))
    provisioned = None if committed is None else committed + (uncommitted or 0)
    template = p.get("summary.config.template")
    parent = p.get("parent")
    folder = (
        moref_value(parent) if isinstance(parent, dict) and parent.get("type") == "Folder" else None
    )
    _current, snapshots = snapshot_rows(p.get("snapshot"))
    snapshot_count = None if _PROP_SNAPSHOT in vm.missing else len(snapshots)
    return {
        "vm": vm.moid,
        "name": _text(p.get("name")),
        "power_state": _text(p.get("summary.runtime.powerState")),
        "host": moref_value(p.get("summary.runtime.host")),
        "host_name": None,
        "cluster": None,
        "cluster_name": None,
        "folder": folder,
        "folder_name": None,
        "guest_os": _text(p.get("summary.config.guestFullName")),
        "ip_address": _text(p.get("summary.guest.ipAddress")),
        "cpu_count": _int(p.get("summary.config.numCpu")),
        "memory_mb": _int(p.get("summary.config.memorySizeMB")),
        "used_storage_bytes": committed,
        "provisioned_storage_bytes": provisioned,
        "tools_status": _text(p.get("summary.guest.toolsStatus")),
        "snapshot_count": snapshot_count,
        "template": template if isinstance(template, bool) else None,
    }


def _scope(params: dict[str, Any], root_folder: str) -> tuple[str, str]:
    for key, mo_type in _SCOPES.items():
        value = params.get(key)
        if isinstance(value, str) and value:
            return mo_type, value
    return "Folder", root_folder


def _matches(row: dict[str, Any], params: dict[str, Any]) -> bool:
    power_state = params.get("power_state")
    if isinstance(power_state, str) and row["power_state"] != power_state:
        return False
    pattern = params.get("name_pattern")
    if isinstance(pattern, str):
        name = row["name"]
        return isinstance(name, str) and fnmatch.fnmatchcase(name.casefold(), pattern.casefold())
    return True


def _name(obj: VimObject | None) -> str | None:
    return None if obj is None else _text(obj.props.get("name"))


async def _fill_names(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    rows: list[dict[str, Any]],
) -> str | None:
    """Fill host, cluster and folder names into *rows*; the error note when it failed."""
    hosts = sorted({r["host"] for r in rows if r["host"]})
    folders = sorted({r["folder"] for r in rows if r["folder"]})
    try:
        host_objs = await read_props(
            connector,
            target,
            operator,
            mo_type="HostSystem",
            moids=hosts,
            path_set=("name", "parent"),
        )
        cluster_of = {
            moid: ref["value"]
            for moid, obj in host_objs.items()
            if isinstance(ref := obj.props.get("parent"), dict)
            and ref.get("type") == _CLUSTER
            and isinstance(ref.get("value"), str)
        }
        cluster_objs = await read_props(
            connector,
            target,
            operator,
            mo_type=_CLUSTER,
            moids=sorted(set(cluster_of.values())),
            path_set=("name",),
        )
        folder_objs = await read_props(
            connector, target, operator, mo_type="Folder", moids=folders, path_set=("name",)
        )
    except _NAME_READ_ERRORS as exc:
        _log.info("vmware_vm_list_names_not_read", error=type(exc).__name__)
        return f"host, cluster and folder names could not be read ({type(exc).__name__})"
    for row in rows:
        host = row["host"]
        if host:
            row["host_name"] = _name(host_objs.get(host))
            row["cluster"] = cluster_of.get(host)
            row["cluster_name"] = _name(cluster_objs.get(row["cluster"] or ""))
        if row["folder"]:
            row["folder_name"] = _name(folder_objs.get(row["folder"]))
    return None


async def vm_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.vm.list``.

    Returns ``{vm_count, read_note, vms}``; the rows are sorted by name. An
    unknown cluster, host or folder gives a ``not_found`` result.
    """
    refs = await read_service_refs(connector, target, operator)
    vms = await read_view(
        connector,
        target,
        operator,
        view_manager=refs.view_manager,
        container=_scope(params, refs.root_folder),
        types=(_VM,),
        prop_type=_VM,
        path_set=VM_LIST_PROPERTIES,
    )
    rows = [row for row in (_row(vm) for vm in vms) if _matches(row, params)]
    rows.sort(key=lambda r: ((r["name"] or "").casefold(), r["vm"]))
    notes: list[str] = []
    if rows:
        name_note = await _fill_names(connector, target, operator, rows)
        if name_note is not None:
            notes.append(name_note)
    unread_snapshots = sum(1 for r in rows if r["snapshot_count"] is None)
    if unread_snapshots:
        notes.append(
            f"the snapshot property of {unread_snapshots} VM(s) could not be read; "
            "their snapshot_count is null"
        )
    _log.info(
        "vmware_vm_list_read",
        target=target.name,
        vm_count=len(rows),
        unread_snapshots=unread_snapshots,
    )
    return {"vm_count": len(rows), "read_note": "; ".join(notes) or None, "vms": rows}


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------

_MOID_SCOPE_RULE = "Give at most one of cluster, host and folder."

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Only the VMs of this cluster, by moid, e.g. 'domain-c7' (from "
                "vmware.cluster.list). " + _MOID_SCOPE_RULE
            ),
        },
        "host": {
            "type": "string",
            "minLength": 1,
            "description": "Only the VMs on this ESXi host, by moid, e.g. 'host-12'. "
            + _MOID_SCOPE_RULE,
        },
        "folder": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Only the VMs in this VM folder and its sub-folders, by moid, e.g. "
                "'group-v3' (from vmware.folder.list). " + _MOID_SCOPE_RULE
            ),
        },
        "power_state": {
            "type": "string",
            "enum": ["poweredOn", "poweredOff", "suspended"],
            "description": "Only the VMs in this power state.",
        },
        "name_pattern": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Only the VMs whose name fits this pattern; '*' is any text, '?' one "
                "character; upper and lower case are the same. E.g. 'web-*'."
            ),
        },
    },
    "not": {
        "anyOf": [
            {"required": ["cluster", "host"]},
            {"required": ["cluster", "folder"]},
            {"required": ["host", "folder"]},
        ]
    },
    "additionalProperties": False,
    "examples": [{"cluster": "<cluster-id>", "power_state": "poweredOn"}],
}

_VM_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {"type": "string", "description": "VM moid, e.g. 'vm-42'."},
        "name": {"type": ["string", "null"], "description": "VM name."},
        "power_state": {
            "type": ["string", "null"],
            "enum": ["poweredOn", "poweredOff", "suspended", None],
            "description": "Power state.",
        },
        "host": {"type": ["string", "null"], "description": "Moid of the ESXi host it runs on."},
        "host_name": {"type": ["string", "null"], "description": "Name of that host."},
        "cluster": {
            "type": ["string", "null"],
            "description": "Moid of the host's cluster; null for a host outside a cluster.",
        },
        "cluster_name": {"type": ["string", "null"], "description": "Name of that cluster."},
        "folder": {"type": ["string", "null"], "description": "Moid of the VM's folder."},
        "folder_name": {"type": ["string", "null"], "description": "Name of that folder."},
        "guest_os": {
            "type": ["string", "null"],
            "description": "Guest OS as configured, e.g. 'Ubuntu Linux (64-bit)'.",
        },
        "ip_address": {
            "type": ["string", "null"],
            "description": "Main guest IP address; null without VMware Tools or when off.",
        },
        "cpu_count": {"type": ["integer", "null"], "description": "Number of virtual CPUs."},
        "memory_mb": {"type": ["integer", "null"], "description": "Memory in MB."},
        "used_storage_bytes": {
            "type": ["integer", "null"],
            "description": "Datastore space the VM uses now, in bytes.",
        },
        "provisioned_storage_bytes": {
            "type": ["integer", "null"],
            "description": "Space used plus space the VM may still grow into, in bytes.",
        },
        "tools_status": {
            "type": ["string", "null"],
            "enum": ["toolsNotInstalled", "toolsNotRunning", "toolsOld", "toolsOk", None],
            "description": "VMware Tools status.",
        },
        "snapshot_count": {
            "type": ["integer", "null"],
            "description": (
                "Number of snapshots of the VM; null when vCenter could not read the "
                "snapshot property (read_note says so)."
            ),
        },
        "template": {
            "type": ["boolean", "null"],
            "description": "True for a template (it cannot be powered on).",
        },
    },
    "required": ["vm", "name", "power_state", "snapshot_count"],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm_count": {"type": "integer", "description": "Number of rows in vms."},
        "read_note": {
            "type": ["string", "null"],
            "description": (
                "Set when the host, cluster and folder names could not be read, or when the "
                "snapshot property of some VMs could not be read."
            ),
        },
        "vms": {"type": "array", "items": _VM_ROW_SCHEMA, "description": "One row per VM."},
    },
    "required": ["vm_count", "vms"],
}

VMWARE_VM_LIST_OP = VmwareTypedOp(
    op_id="vmware.vm.list",
    handler_attr="vm_list",
    summary="List all VMs with power state, host, cluster, guest OS, IP, CPU, memory, storage.",
    description=(
        "Returns one row per VM of the vCenter, or of one cluster, host or folder: power "
        "state, host, cluster, folder, guest OS, IP address, CPUs, memory, used and "
        "provisioned storage, VMware Tools status and snapshot count. Read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=INVENTORY_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vm", "inventory", "report"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call for a VM inventory report, to find VMs by state or name, or to get VM "
            "moids for other operations. One call reads every VM, also thousands."
        ),
        "parameter_hints": {
            "cluster": "Cluster moid, not the name; vmware.cluster.list gives it.",
            "host": "Host moid, e.g. 'host-12'.",
            "folder": "VM folder moid; vmware.folder.list gives it.",
            "name_pattern": "A pattern like 'db-*'. For an exact name, use the name itself.",
        },
        "output_shape": (
            "{vm_count, read_note, vms: [{vm, name, power_state, host, host_name, cluster, "
            "cluster_name, folder, folder_name, guest_os, ip_address, cpu_count, memory_mb, "
            "used_storage_bytes, provisioned_storage_bytes, tools_status, snapshot_count, "
            "template}]}. A long list comes back as a result handle: read the rows with "
            "result_query (filter, sort, pick fields)."
        ),
        "next_step": (
            "For one VM's details, call vmware.vm.info with its moid. For VMs with "
            "snapshots, call vmware.snapshot.list. To change a VM, use the 'vm' group, "
            "e.g. vmware.composite.vm.power."
        ),
    },
)

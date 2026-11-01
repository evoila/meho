# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.snapshot.list`` typed op -- the snapshots of one VM.

The vCenter REST API has no snapshot paths, so this read uses the VIM API: one
``PropertyCollector.RetrievePropertiesEx`` of the VM's ``name`` and ``snapshot``
properties, the same read path ``vmware.vm.info`` uses. It is a
``source_kind="typed"`` bound method on :class:`VmwareRestConnector`, so it works
on a fresh install with no catalog ingest.

The snapshot tree comes back as a flat list in tree order (see
:mod:`~meho_backplane.connectors.vmware_rest.snapshot_tree`), so a long list can
ride a result handle like any other list.

A missing VM is a ``not_found`` result. A ``snapshot`` property vCenter could not
read (a ``missingSet`` entry, for example no permission) fails the call: an
empty list would wrongly say "this VM has no snapshots".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike
from meho_backplane.connectors.vmware_rest.snapshot_tree import SNAPSHOT_ROW_SCHEMA, snapshot_rows
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp, _unwrap_value
from meho_backplane.connectors.vmware_rest.vim_body import (
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
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
_VIRTUAL_MACHINE_MO_TYPE = "VirtualMachine"
_PROP_NAME = "name"
_PROP_SNAPSHOT = "snapshot"

SNAPSHOT_LIST_GROUP_KEY = "vmware-snapshot-list"


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


async def snapshot_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.snapshot.list`` -- the snapshot tree of one VM.

    One ``RetrievePropertiesEx`` of the VM's ``name`` and ``snapshot``, mounted by
    :meth:`VmwareRestConnector._post_vmomi_json` on the VI-JSON base. Returns
    ``{vm, vm_name, current_snapshot_id, snapshot_count, snapshots}``.
    """
    vm_moid = params["vm"]
    result = await connector._post_vmomi_json(
        target,
        _RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=retrieve_properties_body(
            _VIRTUAL_MACHINE_MO_TYPE, [vm_moid], (_PROP_NAME, _PROP_SNAPSHOT)
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
    current, rows = snapshot_rows(props.get(_PROP_SNAPSHOT))
    name = props.get(_PROP_NAME)
    _log.info("vmware_snapshot_list_read", target=target.name, vm=vm_moid, snapshot_count=len(rows))
    return {
        "vm": vm_moid,
        "vm_name": name if isinstance(name, str) else None,
        "current_snapshot_id": current,
        "snapshot_count": len(rows),
        "snapshots": rows,
    }


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
                "Managed-object ID of the VM, e.g. 'vm-42'. vmware.vm.info finds it from the "
                "VM name."
            ),
        },
    },
    "required": ["vm"],
    "additionalProperties": False,
    "examples": [{"vm": "<vm-id>"}],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm": {"type": "string", "description": "Managed-object ID of the VM read."},
        "vm_name": {"type": ["string", "null"], "description": "VM name."},
        "current_snapshot_id": {
            "type": ["string", "null"],
            "description": "Moid of vCenter's current snapshot; null when the VM has none.",
        },
        "snapshot_count": {"type": "integer", "description": "Number of snapshots of the VM."},
        "snapshots": {
            "type": "array",
            "items": SNAPSHOT_ROW_SCHEMA,
            "description": "The snapshots in tree order: a parent comes before its children.",
        },
    },
    "required": ["vm", "current_snapshot_id", "snapshot_count", "snapshots"],
}

#: Curated ``when_to_use`` blurb for the snapshot-list group.
SNAPSHOT_LIST_WHEN_TO_USE = (
    "Use to see the snapshots of one VM: for each snapshot its id, name, description, "
    "the time it was taken, the power state saved in it, whether it was quiesced, its "
    "parent, and which one is current. vCenter's REST API has no snapshot paths; this "
    "reads the VM's snapshot tree through the VIM API, with no catalog ingest. Read-only. "
    "To take, delete or revert a snapshot, use the 'vm' group: "
    "vmware.composite.vm.snapshot.create, .delete and .revert."
)

VMWARE_SNAPSHOT_LIST_OP = VmwareTypedOp(
    op_id="vmware.snapshot.list",
    handler_attr="snapshot_list",
    summary="List one VM's snapshots: id, name, time taken, saved power state, parent, current.",
    description=(
        "Returns the snapshots of one VM (by moid) as a flat list in tree order: id, name, "
        "description, time taken, the power state saved in it, quiesced or not, its parent "
        "and whether it is current. An unknown VM gives a not_found result. Read-only."
    ),
    parameter_schema=_PARAMETER_SCHEMA,
    response_schema=_RESPONSE_SCHEMA,
    group_key=SNAPSHOT_LIST_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vm", "snapshot"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which snapshots a VM has: before you take, delete or revert one, "
            "or to check that a new snapshot is there."
        ),
        "parameter_hints": {
            "vm": "VM moid, e.g. 'vm-42'. Call vmware.vm.info with the VM name to get it.",
        },
        "output_shape": (
            "{vm, vm_name, current_snapshot_id, snapshot_count, snapshots: [{snapshot_id, "
            "name, description, created, power_state, quiesced, current, "
            "parent_snapshot_id}]}. The rows are in tree order: a parent comes before its "
            "children."
        ),
        "next_step": (
            "Pass a snapshot_id to vmware.composite.vm.snapshot.delete or .revert. To take a "
            "new snapshot, call vmware.composite.vm.snapshot.create. Old snapshots slow the "
            "VM and fill the datastore: suggest deleting the ones nobody needs."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (VMWARE_SNAPSHOT_LIST_OP,)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {SNAPSHOT_LIST_GROUP_KEY: SNAPSHOT_LIST_WHEN_TO_USE}

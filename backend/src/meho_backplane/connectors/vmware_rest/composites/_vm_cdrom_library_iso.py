# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Mount an ISO from a content library on a VM, and unmount it again.

* ``vmware.composite.vm.cdrom.mount_library_iso`` (``caution``) --
  ``POST /vcenter/iso/image?action=mount`` with ``{library_item, vm}``.
  vCenter adds a new CD drive to the VM, backed by the ISO file of the
  library item, and answers with the id of that drive. MEHO then reads the
  drive back. The library item is named by id (``library_item``) or by name
  (``library_item_name``, optionally inside one library ``library_name``).
  A name that fits no item or more than one item is refused before anything
  is sent.
* ``vmware.composite.vm.cdrom.unmount_library_iso`` (``caution``) --
  ``POST /vcenter/iso/image?action=unmount`` with ``{vm, cdrom}``. The
  ``cdrom`` is the drive id the mount returned.

Both wait for approval (``requires_approval=True``). The VM is named only by
its id: after approval the change must run on the VM the approver saw. The
same holds for a library item named by name: the approval preview looks the
name up and shows the item id, and the approved run mounts exactly that id
(read back with :func:`~meho_backplane.operations._preview.approved_preview`)
instead of looking the name up again. When the preview showed no id (the
name fit no item or more than one, or the read failed), the approved run
sends nothing (``item_not_pinned``). A write that policy holds saves the id
on its approval row as well, so its resume mounts the same item. Both
call vSphere REST on the connector session through the shared write seam
(:func:`._write._write_sub_op`), so they need no catalog ingest and work on
vCenter 8 and 9. vCenter answers HTTP 400 when it refuses the change (for
example a library item with no ``.iso`` file) and HTTP 404 when the VM, the
item or the drive does not exist. The answer is then ``refused`` or
``not_found`` with vCenter's reason; nothing changed.

The parameters, answers and AI guidance live here, so the shared registry
in ``_register.py`` only splats :data:`VM_CDROM_LIBRARY_ISO_COMPOSITES`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_FIND_LIBRARY,
    _OP_FIND_LIBRARY_ITEM,
    _find_content_library_ids,
    _parse_vsphere_error,
    _unwrap_value,
    _vsphere_fault_detail,
    _write_sub_op,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_hardware import (
    CDROM_ROW_SCHEMA,
    VM_MOID_PATTERN,
    check_vm_moid,
    read_cdrom_rows,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    approved_preview,
    preview_failure_reason,
    register_preview_builder,
)
from meho_backplane.operations.composite import composite_resume_state

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "VM_CDROM_LIBRARY_ISO_COMPOSITES",
    "vm_cdrom_mount_library_iso_composite",
    "vm_cdrom_unmount_library_iso_composite",
]

OP_ID_MOUNT_LIBRARY_ISO: Final = "vmware.composite.vm.cdrom.mount_library_iso"
OP_ID_UNMOUNT_LIBRARY_ISO: Final = "vmware.composite.vm.cdrom.unmount_library_iso"

_OP_MOUNT_ISO: Final = "POST:/vcenter/iso/image?action=mount"
_OP_UNMOUNT_ISO: Final = "POST:/vcenter/iso/image?action=unmount"
_OP_LIST_VM_CDROMS: Final = "GET:/vcenter/vm/{vm}/hardware/cdrom"
_OP_GET_VM_CDROM: Final = "GET:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}"

#: Every REST call of each change. The reconcile test checks them against the
#: pinned vCenter 8.0 and 9.0 API files, and ``_governed_subops`` lists them
#: for the grant set.
SUB_OPS_MOUNT_LIBRARY_ISO: Final[tuple[str, ...]] = (
    _OP_FIND_LIBRARY,
    _OP_FIND_LIBRARY_ITEM,
    _OP_MOUNT_ISO,
    _OP_LIST_VM_CDROMS,
    _OP_GET_VM_CDROM,
)
SUB_OPS_UNMOUNT_LIBRARY_ISO: Final[tuple[str, ...]] = (_OP_UNMOUNT_ISO,)

_ID_PATTERN: Final = r"^[A-Za-z0-9-]+$"


def _vendor_answer(envelope: dict[str, Any], exc: httpx.HTTPError) -> dict[str, Any] | None:
    """Map vCenter HTTP 400 to ``refused`` and 404 to ``not_found``; else ``None``."""
    status, error_type = _parse_vsphere_error(exc)
    if status == 400:
        word, text = "refused", "vCenter refused the change, nothing changed"
    elif status == 404:
        word, text = "not_found", "vCenter did not find the VM, item or drive, nothing changed"
    else:
        return None
    return {
        **envelope,
        "status": word,
        "error_type": error_type,
        "guidance": f"{text}: {_vsphere_fault_detail(exc)}",
    }


# ---------------------------------------------------------------------------
# Library item lookup
# ---------------------------------------------------------------------------


async def _find_one(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    spec: dict[str, Any],
    what: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Return ``(id, None)`` for exactly one match, else ``(None, failure)``."""
    ids = await _find_content_library_ids(connector, target, operator, op_id=op_id, spec=spec)
    if len(ids) == 1:
        return ids[0], None
    if not ids:
        return None, {
            "status": f"{what}_not_found",
            "guidance": f"No {what} is named {spec['name']!r}. Nothing was sent.",
        }
    return None, {
        "status": f"ambiguous_{what}",
        "candidates": ids,
        "guidance": (
            f"The name {spec['name']!r} fits {len(ids)} {what} entries. Nothing was sent. "
            "Call again with library_item, the id of the item you mean."
        ),
    }


def _pinned_library_item() -> tuple[str | None, dict[str, Any] | None]:
    """The item id an approval pinned, if this run follows one.

    ``(id, None)`` when the approval preview of this change, or the approval
    row of its held write, saved the id. ``(None, failure)`` when the change
    was approved but its preview showed no id. ``(None, None)`` when no
    approval is involved, so the caller looks the name up live.
    """
    preview = approved_preview(OP_ID_MOUNT_LIBRARY_ISO)
    if preview is not None:
        pinned = preview.get("library_item")
        if isinstance(pinned, str) and pinned:
            return pinned, None
        return None, {
            "status": "item_not_pinned",
            "guidance": (
                "The approved call names the item by name, but the approval preview "
                "showed no item id, so MEHO does not look the name up again. Nothing "
                "was sent. Call again with library_item, the id of the item you mean."
            ),
        }
    state = composite_resume_state(OP_ID_MOUNT_LIBRARY_ISO) or {}
    saved = state.get("library_item")
    if isinstance(saved, str) and saved:
        return saved, None
    return None, None


async def _resolve_library_item(
    connector: VmwareRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve the item id: as given, as an approval pinned it, or by one name lookup."""
    item_id = params.get("library_item")
    if isinstance(item_id, str) and item_id:
        return item_id, None
    pinned, failure = _pinned_library_item()
    if pinned is not None or failure is not None:
        return pinned, failure
    spec: dict[str, Any] = {"name": params["library_item_name"]}
    library_name = params.get("library_name")
    if isinstance(library_name, str) and library_name:
        library_id, failure = await _find_one(
            connector,
            target,
            operator,
            op_id=_OP_FIND_LIBRARY,
            spec={"name": library_name},
            what="library",
        )
        if failure is not None:
            return None, failure
        spec["library_id"] = library_id
    return await _find_one(
        connector, target, operator, op_id=_OP_FIND_LIBRARY_ITEM, spec=spec, what="item"
    )


async def _cdroms_or_empty(
    connector: VmwareRestConnector, operator: Operator, target: Any, vm: str
) -> list[dict[str, Any]]:
    try:
        return await read_cdrom_rows(connector, operator, target, vm)
    except httpx.HTTPError:
        return []


# ---------------------------------------------------------------------------
# vm.cdrom.mount_library_iso
# ---------------------------------------------------------------------------


async def vm_cdrom_mount_library_iso_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Mount a content-library ISO on a VM. Op-id: ``vm.cdrom.mount_library_iso``."""
    vm = check_vm_moid(params["vm"])
    envelope: dict[str, Any] = {
        "vm": vm,
        "library_item": None,
        "cdrom": None,
        "cdrom_info": None,
        "candidates": None,
        "error_type": None,
        "guidance": None,
    }
    item_id, failure = await _resolve_library_item(connector, target, operator, params)
    if failure is not None:
        return {**envelope, **failure}
    envelope["library_item"] = item_id
    try:
        gate, payload = await _write_sub_op(
            connector,
            target,
            operator,
            _OP_MOUNT_ISO,
            {"library_item": item_id, "vm": vm},
            resume_state={"library_item": item_id},
        )
    except httpx.HTTPError as exc:
        answer = _vendor_answer(envelope, exc)
        if answer is None:
            raise
        return answer
    if gate is not None:
        return gate
    cdrom_id = _unwrap_value(payload)
    if not isinstance(cdrom_id, str):
        return {
            **envelope,
            "status": "mounted",
            "guidance": "vCenter mounted the ISO but sent no drive id. Read it with "
            "vmware.vm.hardware.show.",
        }
    rows = await _cdroms_or_empty(connector, operator, target, vm)
    row = next((r for r in rows if r["cdrom"] == cdrom_id), None)
    return {**envelope, "status": "mounted", "cdrom": cdrom_id, "cdrom_info": row}


async def _mount_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the VM, the resolved library item, and the drives it has now."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    try:
        vm = check_vm_moid(ctx.params["vm"])
        item_id, failure = await _resolve_library_item(
            connector, ctx.target, ctx.operator, ctx.params
        )
        current = await read_cdrom_rows(connector, ctx.operator, ctx.target, vm)
    except Exception as exc:
        return {
            "summary": "MEHO could not read the VM or the library item.",
            "read_error": preview_failure_reason(exc),
        }
    if failure is not None:
        return {"summary": f"The call will send nothing: {failure['guidance']}", **failure}
    return {
        "summary": (
            f"Add a new CD drive to {vm} with the ISO of library item {item_id} "
            f"({len(current)} drive(s) now)."
        ),
        "vm": vm,
        "library_item": item_id,
        "current_cdroms": current,
    }


# ---------------------------------------------------------------------------
# vm.cdrom.unmount_library_iso
# ---------------------------------------------------------------------------


async def vm_cdrom_unmount_library_iso_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Unmount a library ISO drive. Op-id: ``vm.cdrom.unmount_library_iso``."""
    vm = check_vm_moid(params["vm"])
    cdrom = params["cdrom"]
    envelope: dict[str, Any] = {"vm": vm, "cdrom": cdrom, "error_type": None, "guidance": None}
    try:
        gate, _ = await _write_sub_op(
            connector, target, operator, _OP_UNMOUNT_ISO, {"vm": vm, "cdrom": cdrom}
        )
    except httpx.HTTPError as exc:
        answer = _vendor_answer(envelope, exc)
        if answer is None:
            raise
        return answer
    if gate is not None:
        return gate
    return {**envelope, "status": "unmounted"}


async def _unmount_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the drive the call will take off the VM."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    cdrom = ctx.params.get("cdrom")
    try:
        vm = check_vm_moid(ctx.params["vm"])
        current = await read_cdrom_rows(connector, ctx.operator, ctx.target, vm)
    except Exception as exc:
        return {
            "summary": "MEHO could not read the VM, so it cannot show the drive.",
            "cdrom": cdrom,
            "read_error": preview_failure_reason(exc),
        }
    row = next((r for r in current if r["cdrom"] == cdrom), None)
    summary = (
        f"Unmount the ISO in CD drive {cdrom} of {vm}."
        if row is not None
        else f"{vm} has no CD drive {cdrom}; vCenter will answer not found."
    )
    return {"summary": summary, "vm": vm, "cdrom": cdrom, "cdrom_info": row}


register_preview_builder(OP_ID_MOUNT_LIBRARY_ISO, _mount_preview)
register_preview_builder(OP_ID_UNMOUNT_LIBRARY_ISO, _unmount_preview)


# ---------------------------------------------------------------------------
# Schemas, AI guidance and registration rows
# ---------------------------------------------------------------------------

_VM_ID_PROPERTY: Final[dict[str, Any]] = {
    "vm": {
        "type": "string",
        "pattern": VM_MOID_PATTERN,
        "description": (
            "VM id, such as 'vm-42'. Get it from vmware.vm.hardware.show or vmware.vm.list. "
            "A change takes no VM name, so after approval it runs on the VM the approver saw."
        ),
    },
}

MOUNT_LIBRARY_ISO_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **_VM_ID_PROPERTY,
        "library_item": {
            "type": "string",
            "pattern": _ID_PATTERN,
            "description": (
                "Id of the content-library item that holds the ISO file. Give this "
                "or library_item_name, not both. The id pins the exact item."
            ),
        },
        "library_item_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name of the library item. MEHO looks it up; no match gives "
                "item_not_found, more than one gives ambiguous_item with the ids. "
                "Nothing is sent in both cases."
            ),
        },
        "library_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional library name, to look up library_item_name in one library only."
            ),
        },
    },
    "required": ["vm"],
    "oneOf": [{"required": ["library_item"]}, {"required": ["library_item_name"]}],
    "additionalProperties": False,
    "examples": [
        {"vm": "vm-42", "library_item": "0b6e3e0c-1f2a-4c55-9a51-2f6d7a8b9c01"},
        {"vm": "vm-42", "library_item_name": "<iso-item-name>", "library_name": "<library>"},
    ],
}

_NULLABLE_STR: Final = {"type": ["string", "null"]}
_VENDOR_FIELDS: Final[dict[str, Any]] = {
    "error_type": {**_NULLABLE_STR, "description": "vCenter error type on refused or not_found."},
    "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
}

MOUNT_LIBRARY_ISO_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "mounted",
                "refused",
                "not_found",
                "library_not_found",
                "ambiguous_library",
                "item_not_found",
                "ambiguous_item",
                "item_not_pinned",
            ],
            "description": (
                "mounted: a new CD drive holds the ISO; refused / not_found: vCenter said "
                "no, nothing changed; the other values: the name lookup failed, or the "
                "approval preview pinned no item id; nothing sent."
            ),
        },
        "vm": {"type": "string", "description": "VM id."},
        "library_item": {**_NULLABLE_STR, "description": "Id of the library item mounted."},
        "cdrom": {**_NULLABLE_STR, "description": "Id of the new CD drive. Keep it to unmount."},
        "cdrom_info": {
            **CDROM_ROW_SCHEMA,
            "type": ["object", "null"],
            "description": "The new drive as read back, or null.",
        },
        "candidates": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "The matching ids when a name fits more than one.",
        },
        **_VENDOR_FIELDS,
    },
    "required": ["status", "vm"],
}

UNMOUNT_LIBRARY_ISO_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **_VM_ID_PROPERTY,
        "cdrom": {
            "type": "string",
            "pattern": _ID_PATTERN,
            "description": (
                "Id of the CD drive, as the mount returned it (cdrom), such as '16002'. "
                "vmware.vm.hardware.show also lists it."
            ),
        },
    },
    "required": ["vm", "cdrom"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42", "cdrom": "16002"}],
}

UNMOUNT_LIBRARY_ISO_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["unmounted", "refused", "not_found"],
            "description": (
                "unmounted: vCenter took the drive off; refused / not_found: vCenter "
                "said no, nothing changed."
            ),
        },
        "vm": {"type": "string", "description": "VM id."},
        "cdrom": {"type": "string", "description": "Id of the CD drive."},
        **_VENDOR_FIELDS,
    },
    "required": ["status", "vm", "cdrom"],
}

MOUNT_LIBRARY_ISO_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to put an ISO file that is stored in a vCenter content library into a VM, "
        "for example a boot or kickstart ISO. vCenter adds a new CD drive for it. For an "
        "ISO file on a datastore, use vmware.composite.vm.cdrom.add instead."
    ),
    "preconditions": (
        "Read the VM first with vmware.vm.hardware.show to get its id (vm). The change "
        "takes the VM id, not a name. The library item must hold an .iso file. The "
        "change waits for approval."
    ),
    "parameter_hints": {
        "library_item": "The item id; best, because it pins the exact item.",
        "library_item_name": (
            "The item name, when you do not have the id. The approval preview looks it "
            "up and the approved run mounts the item the approver saw."
        ),
        "library_name": "Scopes the name lookup to one library.",
    },
    "output_shape": (
        "{status: mounted|refused|not_found|library_not_found|ambiguous_library|"
        "item_not_found|ambiguous_item|item_not_pinned, vm, library_item, cdrom, "
        "cdrom_info, candidates, error_type, guidance}."
    ),
    "next_step": (
        "Keep cdrom: vmware.composite.vm.cdrom.unmount_library_iso needs it. Not safe "
        "to run twice: a second call adds a second drive. On ambiguous_item or "
        "item_not_pinned, call again with one id (from candidates)."
    ),
}

UNMOUNT_LIBRARY_ISO_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to take a content-library ISO off a VM again, after "
        "vmware.composite.vm.cdrom.mount_library_iso. vCenter removes that CD drive."
    ),
    "preconditions": (
        "You need the VM id (vm) and the drive id (cdrom) the mount returned; "
        "vmware.vm.hardware.show also lists the drives. The change waits for approval."
    ),
    "output_shape": "{status: unmounted|refused|not_found, vm, cdrom, error_type, guidance}.",
    "next_step": (
        "A second call for the same drive gives not_found. On refused, read guidance. "
        "Check the result with vmware.vm.hardware.show."
    ),
}

#: The two registration rows, as ``_CompositeSpec`` keyword arguments.
VM_CDROM_LIBRARY_ISO_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": OP_ID_MOUNT_LIBRARY_ISO,
        "handler": vm_cdrom_mount_library_iso_composite,
        "summary": "Mount an ISO from a content library on a VM, as a new CD drive.",
        "description": (
            "Mounts the ISO of a content-library item on a VM, named by its VM id. vCenter "
            "adds a new CD drive for it; MEHO reads it back. The item is named by id or "
            "by name. Waits for approval."
        ),
        "parameter_schema": MOUNT_LIBRARY_ISO_PARAMETER_SCHEMA,
        "response_schema": MOUNT_LIBRARY_ISO_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "cdrom", "content-library", "iso"],
        "safety_level": "caution",
        "requires_approval": True,
        "llm_instructions": MOUNT_LIBRARY_ISO_LLM_INSTRUCTIONS,
    },
    {
        "op_id": OP_ID_UNMOUNT_LIBRARY_ISO,
        "handler": vm_cdrom_unmount_library_iso_composite,
        "summary": "Unmount a content-library ISO from a VM and remove its CD drive.",
        "description": (
            "Takes a content-library ISO off a VM, named by VM id and the CD drive id "
            "the mount returned. vCenter removes that drive. Waits for approval."
        ),
        "parameter_schema": UNMOUNT_LIBRARY_ISO_PARAMETER_SCHEMA,
        "response_schema": UNMOUNT_LIBRARY_ISO_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "cdrom", "content-library", "iso"],
        "safety_level": "caution",
        "requires_approval": True,
        "llm_instructions": UNMOUNT_LIBRARY_ISO_LLM_INSTRUCTIONS,
    },
)

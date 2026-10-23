# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two VM changes: add a CD drive, and unmount the VMware Tools installer.

* ``vmware.composite.vm.cdrom.add`` (``dangerous``) --
  ``POST /vcenter/vm/{vm}/hardware/cdrom``. Adds one CD drive, empty (client
  device) or with an ISO file from a datastore. It reads the new drive back.
* ``vmware.composite.vm.tools.installer.unmount`` (``caution``) --
  ``POST /vcenter/vm/{vm}/tools/installer?action=disconnect``. Reads
  ``GET /vcenter/vm/{vm}/tools/installer`` first: when the installer is not
  mounted, nothing is sent (``not_mounted``). Then it reads the state again.

Both wait for approval (``requires_approval=True``). The VM is named only by
its id (``vm``), not by name: after approval the change must run on the VM the
approver saw, and a name looked up again could point to another VM. Both call
vSphere REST on the connector session through the shared write seam
(:func:`._write._write_sub_op`), so they need no catalog ingest and work on
vCenter 8 and 9. vCenter refuses some requests with HTTP 400 (for example an
IDE drive on a running VM, or the installer of a VM that is off). The answer
is then ``refused`` with vCenter's reason; nothing changed.

The parameters, answers and AI guidance live here, so the shared registry
in ``_register.py`` only splats :data:`VM_CDROM_TOOLS_COMPOSITES`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composites._write import (
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
    read_tools_installer_mounted,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "VM_CDROM_TOOLS_COMPOSITES",
    "vm_cdrom_add_composite",
    "vm_tools_installer_unmount_composite",
]

OP_ID_VM_CDROM_ADD: Final = "vmware.composite.vm.cdrom.add"
OP_ID_VM_TOOLS_INSTALLER_UNMOUNT: Final = "vmware.composite.vm.tools.installer.unmount"

_OP_LIST_VM_CDROMS: Final = "GET:/vcenter/vm/{vm}/hardware/cdrom"
_OP_GET_VM_CDROM: Final = "GET:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}"
_OP_CREATE_VM_CDROM: Final = "POST:/vcenter/vm/{vm}/hardware/cdrom"
_OP_GET_TOOLS_INSTALLER: Final = "GET:/vcenter/vm/{vm}/tools/installer"
_OP_DISCONNECT_TOOLS_INSTALLER: Final = "POST:/vcenter/vm/{vm}/tools/installer?action=disconnect"

#: Every REST call of each change (reads and the one write). The reconcile
#: test checks them against the pinned vCenter 8.0 and 9.0 API files, and
#: ``_governed_subops`` lists them for the grant set.
SUB_OPS_VM_CDROM_ADD: Final[tuple[str, ...]] = (
    _OP_LIST_VM_CDROMS,
    _OP_GET_VM_CDROM,
    _OP_CREATE_VM_CDROM,
)
SUB_OPS_VM_TOOLS_INSTALLER_UNMOUNT: Final[tuple[str, ...]] = (
    _OP_GET_TOOLS_INSTALLER,
    _OP_DISCONNECT_TOOLS_INSTALLER,
)


def _refused(envelope: dict[str, Any], exc: httpx.HTTPError) -> dict[str, Any] | None:
    """Map a vCenter HTTP 400 to ``refused``; ``None`` for any other fault."""
    status, error_type = _parse_vsphere_error(exc)
    if status != 400:
        return None
    return {
        **envelope,
        "status": "refused",
        "error_type": error_type,
        "guidance": f"vCenter refused the change, nothing changed: {_vsphere_fault_detail(exc)}",
    }


# ---------------------------------------------------------------------------
# vm.cdrom.add
# ---------------------------------------------------------------------------


def _cdrom_create_spec(params: dict[str, Any]) -> dict[str, Any]:
    """Build ``Cdrom.CreateSpec`` (top-level body on ``/api``) from the parameters."""
    iso_file = params.get("iso_file")
    spec: dict[str, Any] = {
        "backing": (
            {"type": "ISO_FILE", "iso_file": iso_file}
            if isinstance(iso_file, str)
            else {"type": "CLIENT_DEVICE"}
        ),
        "start_connected": params.get("start_connected", isinstance(iso_file, str)),
    }
    for key in ("type", "allow_guest_control"):
        if params.get(key) is not None:
            spec[key] = params[key]
    return spec


async def vm_cdrom_add_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Add one CD drive to a VM. Op-id: ``vmware.composite.vm.cdrom.add``."""
    vm = check_vm_moid(params["vm"])
    spec = _cdrom_create_spec(params)
    envelope: dict[str, Any] = {
        "vm": vm,
        "requested": spec,
        "cdrom": None,
        "cdrom_info": None,
        "error_type": None,
        "guidance": None,
    }
    try:
        gate, payload = await _write_sub_op(
            connector, target, operator, _OP_CREATE_VM_CDROM, {"vm": vm, **spec}
        )
    except httpx.HTTPError as exc:
        refused = _refused(envelope, exc)
        if refused is None:
            raise
        return refused
    if gate is not None:
        return gate
    cdrom_id = _unwrap_value(payload)
    if not isinstance(cdrom_id, str):
        return {
            **envelope,
            "status": "added",
            "guidance": "vCenter added the drive but sent no id. Read it with "
            "vmware.vm.hardware.show.",
        }
    row = next(
        (
            r
            for r in await _cdroms_or_empty(connector, operator, target, vm)
            if r["cdrom"] == cdrom_id
        ),
        None,
    )
    return {**envelope, "status": "added", "cdrom": cdrom_id, "cdrom_info": row}


async def _cdroms_or_empty(
    connector: VmwareRestConnector, operator: Operator, target: Any, vm: str
) -> list[dict[str, Any]]:
    try:
        return await read_cdrom_rows(connector, operator, target, vm)
    except httpx.HTTPError:
        return []


async def _vm_cdrom_add_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the VM, the drive to add, and the drives it has now."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    spec = _cdrom_create_spec(ctx.params)
    try:
        vm = check_vm_moid(ctx.params["vm"])
        current = await read_cdrom_rows(connector, ctx.operator, ctx.target, vm)
    except Exception as exc:
        return {
            "summary": "MEHO could not read the VM, so it cannot show its CD drives.",
            "requested": spec,
            "read_error": preview_failure_reason(exc),
        }
    return {
        "summary": f"Add a CD drive to {vm} ({len(current)} drive(s) now).",
        "vm": vm,
        "requested": spec,
        "current_cdroms": current,
    }


# ---------------------------------------------------------------------------
# vm.tools.installer.unmount
# ---------------------------------------------------------------------------


async def vm_tools_installer_unmount_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Unmount the VMware Tools installer. Op-id: ``vm.tools.installer.unmount``."""
    vm = check_vm_moid(params["vm"])
    before = await read_tools_installer_mounted(connector, operator, target, vm)
    envelope: dict[str, Any] = {
        "vm": vm,
        "mounted_before": before,
        "mounted_after": before,
        "error_type": None,
        "guidance": None,
    }
    if before is False:
        return {**envelope, "status": "not_mounted", "guidance": "Nothing to do; nothing sent."}
    try:
        gate, _ = await _write_sub_op(
            connector, target, operator, _OP_DISCONNECT_TOOLS_INSTALLER, {"vm": vm}
        )
    except httpx.HTTPError as exc:
        refused = _refused(envelope, exc)
        if refused is None:
            raise
        return refused
    if gate is not None:
        return gate
    try:
        after = await read_tools_installer_mounted(connector, operator, target, vm)
    except httpx.HTTPError:
        after = None
    if after is True:
        return {
            **envelope,
            "status": "still_mounted",
            "mounted_after": True,
            "guidance": "vCenter took the request, but the installer still reads as mounted. "
            "The guest may hold the drive; check the VM console.",
        }
    guidance = (
        None
        if after is False
        else "vCenter took the request, but MEHO could not read the state again. Check "
        "with vmware.vm.hardware.show."
    )
    return {**envelope, "status": "unmounted", "mounted_after": after, "guidance": guidance}


async def _vm_tools_installer_unmount_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver whether the installer is mounted now."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    try:
        vm = check_vm_moid(ctx.params["vm"])
        mounted = await read_tools_installer_mounted(connector, ctx.operator, ctx.target, vm)
    except Exception as exc:
        return {
            "summary": "MEHO could not read the VM, so it cannot show the installer state.",
            "read_error": preview_failure_reason(exc),
        }
    summary = (
        f"Unmount the VMware Tools installer from {vm}."
        if mounted
        else f"The installer is not mounted on {vm}; the call will change nothing."
    )
    return {
        "summary": summary,
        "vm": vm,
        "installer_mounted": mounted,
    }


register_preview_builder(OP_ID_VM_CDROM_ADD, _vm_cdrom_add_preview)
register_preview_builder(OP_ID_VM_TOOLS_INSTALLER_UNMOUNT, _vm_tools_installer_unmount_preview)


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

VM_CDROM_ADD_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **_VM_ID_PROPERTY,
        "iso_file": {
            "type": "string",
            "minLength": 1,
            "pattern": r"^\[[^\]]+\] \S.*$(?!\n)",
            "description": (
                "Datastore path of an ISO file, such as '[datastore1] iso/ubuntu.iso'. "
                "Omit for an empty drive (client device backing)."
            ),
        },
        "type": {
            "type": "string",
            "enum": ["IDE", "SATA"],
            "description": (
                "Controller kind. Omit to let vCenter pick the default of the guest OS. "
                "An IDE drive can only be added while the VM is off; SATA also while on."
            ),
        },
        "start_connected": {
            "type": "boolean",
            "description": (
                "Connect the drive when the VM powers on. Default: true with iso_file, "
                "false without."
            ),
        },
        "allow_guest_control": {
            "type": "boolean",
            "description": "Let the guest connect and disconnect the drive. Default false.",
        },
    },
    "required": ["vm"],
    "additionalProperties": False,
    "examples": [
        {"vm": "vm-42", "iso_file": "[<datastore-name>] iso/<file-name>.iso"},
        {"vm": "vm-42", "type": "SATA"},
    ],
}

_NULLABLE_STR: Final = {"type": ["string", "null"]}

VM_CDROM_ADD_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["added", "refused"],
            "description": "added: the drive exists; refused: vCenter said no, nothing changed.",
        },
        "vm": {"type": "string", "description": "VM id."},
        "requested": {"type": "object", "description": "The CreateSpec MEHO sent."},
        "cdrom": {**_NULLABLE_STR, "description": "Id of the new drive."},
        "cdrom_info": {
            **CDROM_ROW_SCHEMA,
            "type": ["object", "null"],
            "description": "The new drive as read back, or null.",
        },
        "error_type": {**_NULLABLE_STR, "description": "vCenter error type on refused."},
        "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
    },
    "required": ["status", "vm"],
}

VM_TOOLS_INSTALLER_UNMOUNT_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": _VM_ID_PROPERTY,
    "required": ["vm"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42"}],
}

_NULLABLE_BOOL: Final = {"type": ["boolean", "null"]}

VM_TOOLS_INSTALLER_UNMOUNT_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["unmounted", "not_mounted", "still_mounted", "refused"],
            "description": (
                "unmounted: the installer was taken out; not_mounted: it was not in, "
                "nothing sent; still_mounted: sent, but it still reads as mounted; "
                "refused: vCenter said no, nothing changed."
            ),
        },
        "vm": {"type": "string", "description": "VM id."},
        "mounted_before": {**_NULLABLE_BOOL, "description": "Installer mounted before."},
        "mounted_after": {**_NULLABLE_BOOL, "description": "Installer mounted after."},
        "error_type": {**_NULLABLE_STR, "description": "vCenter error type on refused."},
        "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
    },
    "required": ["status", "vm"],
}

VM_CDROM_ADD_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to give a VM one more CD drive, empty or with an ISO file from a datastore. "
        "To put an ISO into a drive the VM already has, use vmware.composite.vm.device.cdrom "
        "with action update instead."
    ),
    "preconditions": (
        "Read the VM first with vmware.vm.hardware.show to get its id (vm) and see the "
        "drives it has. The change takes the VM id, not a name. An IDE "
        "drive needs the VM powered off; a suspended VM takes no new drive. The ISO file "
        "must exist on a datastore the VM's host can see. The change waits for approval."
    ),
    "parameter_hints": {
        "iso_file": "'[<datastore-name>] <folder>/<file>.iso'; omit for an empty drive.",
        "type": "IDE or SATA; omit for the guest OS default.",
    },
    "output_shape": (
        "{status: added|refused, vm, requested, cdrom, cdrom_info: {cdrom, label, "
        "type, backing_type, iso_file, state, connected, start_connected, ...}, "
        "error_type, guidance}."
    ),
    "next_step": (
        "Not safe to run twice: a second call adds a second drive. On refused, read "
        "guidance, fix the cause (for example power the VM off for IDE) and try again. "
        "Check the result with vmware.vm.hardware.show."
    ),
}

VM_TOOLS_INSTALLER_UNMOUNT_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to take the VMware Tools installer ISO out of a VM's CD drive, for example "
        "after a Tools install or upgrade, or when the drive is needed for another ISO."
    ),
    "preconditions": (
        "The VM must be powered on; vCenter refuses the call for a VM that is off. "
        "Read the VM first with vmware.vm.hardware.show: it gives the VM id (vm) and "
        "whether the installer is mounted (tools_installer_mounted). The change takes "
        "the VM id, not a name. The change waits for approval."
    ),
    "output_shape": (
        "{status: unmounted|not_mounted|still_mounted|refused, vm, mounted_before, "
        "mounted_after, error_type, guidance}."
    ),
    "next_step": (
        "Safe to run twice: when the installer is not mounted, nothing is sent. On "
        "refused, read guidance (often the VM is off). Check with vmware.vm.hardware.show."
    ),
}

#: The two registration rows, as ``_CompositeSpec`` keyword arguments.
VM_CDROM_TOOLS_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": OP_ID_VM_CDROM_ADD,
        "handler": vm_cdrom_add_composite,
        "summary": "Add a CD drive to a VM, empty or with an ISO file from a datastore.",
        "description": (
            "Adds one CD drive to a VM, named by its VM id: empty (client device) or with "
            "an ISO file from a datastore, on IDE or SATA. Reads the new drive back. vCenter "
            "refuses an IDE drive on a running VM. Waits for approval."
        ),
        "parameter_schema": VM_CDROM_ADD_PARAMETER_SCHEMA,
        "response_schema": VM_CDROM_ADD_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "hardware", "cdrom"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VM_CDROM_ADD_LLM_INSTRUCTIONS,
    },
    {
        "op_id": OP_ID_VM_TOOLS_INSTALLER_UNMOUNT,
        "handler": vm_tools_installer_unmount_composite,
        "summary": "Unmount the VMware Tools installer from a VM's CD drive.",
        "description": (
            "Takes the VMware Tools installer out of a VM's CD drive, named by its VM id. "
            "Reads the installer state first and sends nothing when it is not mounted. The "
            "VM must be powered on. Waits for approval."
        ),
        "parameter_schema": VM_TOOLS_INSTALLER_UNMOUNT_PARAMETER_SCHEMA,
        "response_schema": VM_TOOLS_INSTALLER_UNMOUNT_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "vmware-tools", "cdrom"],
        "safety_level": "caution",
        "requires_approval": True,
        "llm_instructions": VM_TOOLS_INSTALLER_UNMOUNT_LLM_INSTRUCTIONS,
    },
)

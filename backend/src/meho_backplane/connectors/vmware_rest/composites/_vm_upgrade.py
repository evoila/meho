# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two VM upgrades: the virtual hardware version, and VMware Tools.

* ``vmware.composite.vm.hardware.upgrade`` (``dangerous``) --
  ``POST /vcenter/vm/{vm}/hardware?action=upgrade``. Reads
  ``GET /vcenter/vm/{vm}/hardware`` first: a VM already at the asked version
  gets nothing (``unchanged``), and an older version than now is refused
  before anything is sent. The VM must be powered off. Reads the version
  again after the call.
* ``vmware.composite.vm.tools.upgrade`` (``dangerous``), two modes:

  * ``upgrade`` -- ``POST /vcenter/vm/{vm}/tools?action=upgrade``. Reads
    ``GET /vcenter/vm/{vm}/tools`` first and sends nothing when Tools is
    already current. vCenter starts the upgrade in the running guest; the
    answer reads the Tools state again.
  * ``mount_installer`` -- ``POST /vcenter/vm/{vm}/tools/installer?action=connect``
    (the REST form of the VIM ``MountToolsInstaller``). Sends nothing when the
    installer is already mounted.

Both wait for approval. The VM is named only by its id. A vCenter HTTP 400
(for example a VM in the wrong power state) is ``refused`` with vCenter's
reason; nothing changed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.composites._vm_change_common import (
    VM_ID_PROPERTY,
    refused,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _read_sub_op,
    _unwrap_value,
    _write_sub_op,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_hardware import (
    check_vm_moid,
    read_tools_installer_mounted,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "SUB_OPS_VM_HARDWARE_UPGRADE",
    "SUB_OPS_VM_TOOLS_UPGRADE",
    "COMPOSITES",
    "vm_hardware_upgrade_composite",
    "vm_tools_upgrade_composite",
]

OP_ID_VM_HARDWARE_UPGRADE: Final = "vmware.composite.vm.hardware.upgrade"
OP_ID_VM_TOOLS_UPGRADE: Final = "vmware.composite.vm.tools.upgrade"

_OP_GET_HARDWARE: Final = "GET:/vcenter/vm/{vm}/hardware"
_OP_UPGRADE_HARDWARE: Final = "POST:/vcenter/vm/{vm}/hardware?action=upgrade"
_OP_GET_TOOLS: Final = "GET:/vcenter/vm/{vm}/tools"
_OP_UPGRADE_TOOLS: Final = "POST:/vcenter/vm/{vm}/tools?action=upgrade"
_OP_GET_TOOLS_INSTALLER: Final = "GET:/vcenter/vm/{vm}/tools/installer"
_OP_CONNECT_TOOLS_INSTALLER: Final = "POST:/vcenter/vm/{vm}/tools/installer?action=connect"

#: Every REST call of each change. The reconcile test checks them against the
#: pinned vCenter 8.0 and 9.0 API files, and ``_governed_subops`` lists them.
SUB_OPS_VM_HARDWARE_UPGRADE: Final[tuple[str, ...]] = (_OP_GET_HARDWARE, _OP_UPGRADE_HARDWARE)
SUB_OPS_VM_TOOLS_UPGRADE: Final[tuple[str, ...]] = (
    _OP_GET_TOOLS,
    _OP_UPGRADE_TOOLS,
    _OP_GET_TOOLS_INSTALLER,
    _OP_CONNECT_TOOLS_INSTALLER,
)

#: Both upgrade calls can run for minutes on vCenter before they answer.
_UPGRADE_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=900.0, write=30.0, pool=10.0)

_HW_VERSION_RE: Final = re.compile(r"VMX_([0-9]+)")


def _hw_number(version: Any) -> int | None:
    match = _HW_VERSION_RE.fullmatch(version) if isinstance(version, str) else None
    return int(match.group(1)) if match else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


async def _read(
    connector: VmwareRestConnector, target: Any, operator: Operator, op_id: str, vm: str
) -> dict[str, Any]:
    raw = _unwrap_value(await _read_sub_op(connector, target, operator, op_id, {"vm": vm}))
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# vm.hardware.upgrade
# ---------------------------------------------------------------------------


def _hardware_view(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": _str(raw.get("version")),
        "upgrade_policy": _str(raw.get("upgrade_policy")),
        "upgrade_status": _str(raw.get("upgrade_status")),
    }


def _hardware_precheck(envelope: dict[str, Any], wanted: str | None) -> dict[str, Any] | None:
    """``unchanged`` / ``invalid_request`` before any write, or ``None`` to go on."""
    now = _hw_number(envelope["before"]["version"])
    asked = _hw_number(wanted)
    if asked is None or now is None:
        return None
    if asked == now:
        return {
            **envelope,
            "status": "unchanged",
            "after": envelope["before"],
            "guidance": f"The VM already has hardware version {wanted}; nothing was sent.",
        }
    if asked < now:
        return {
            **envelope,
            "status": "invalid_request",
            "guidance": f"{wanted} is older than the VM's version "
            f"{envelope['before']['version']}; a hardware version cannot go down.",
        }
    return None


async def vm_hardware_upgrade_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Upgrade a VM's hardware version. Op-id: ``vmware.composite.vm.hardware.upgrade``."""
    vm = check_vm_moid(params["vm"])
    wanted = params.get("version")
    before = _hardware_view(await _read(connector, target, operator, _OP_GET_HARDWARE, vm))
    envelope: dict[str, Any] = {
        "vm": vm,
        "requested_version": wanted,
        "before": before,
        "after": None,
        "error_type": None,
        "guidance": None,
    }
    early = _hardware_precheck(envelope, wanted)
    if early is not None:
        return early
    body = {"version": wanted} if isinstance(wanted, str) else {}
    try:
        gate, _ = await _write_sub_op(
            connector,
            target,
            operator,
            _OP_UPGRADE_HARDWARE,
            {"vm": vm, **body},
            timeout=_UPGRADE_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        answer = refused(envelope, exc)
        if answer is None:
            raise
        if answer["error_type"] == "ALREADY_IN_DESIRED_STATE":
            return {**answer, "status": "unchanged", "after": before}
        return answer
    if gate is not None:
        return gate
    after = _hardware_view(await _read(connector, target, operator, _OP_GET_HARDWARE, vm))
    raised = (_hw_number(after["version"]) or 0) > (_hw_number(before["version"]) or 0)
    if not raised:
        return {
            **envelope,
            "status": "partial",
            "after": after,
            "guidance": "vCenter took the request, but the version reads back unchanged. "
            "Read it again with vmware.vm.hardware.show later.",
        }
    return {**envelope, "status": "upgraded", "after": after}


async def _vm_hardware_upgrade_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the hardware version now and the version asked for."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    wanted = ctx.params.get("version") or "the newest version this host supports"
    try:
        vm = check_vm_moid(ctx.params["vm"])
        now = _hardware_view(await _read(connector, ctx.target, ctx.operator, _OP_GET_HARDWARE, vm))
    except Exception as exc:
        return {
            "summary": "MEHO could not read the VM, so it cannot show its hardware version.",
            "read_error": preview_failure_reason(exc),
        }
    return {
        "summary": f"Upgrade {vm} from hardware version {now['version']} to {wanted}. "
        "This cannot be undone.",
        "vm": vm,
        "current": now,
        "requested_version": ctx.params.get("version"),
    }


# ---------------------------------------------------------------------------
# vm.tools.upgrade
# ---------------------------------------------------------------------------


def _tools_view(raw: dict[str, Any]) -> dict[str, Any]:
    number = raw.get("version_number")
    return {
        "version": _str(raw.get("version")),
        "version_number": number if isinstance(number, int) else None,
        "version_status": _str(raw.get("version_status")),
        "run_state": _str(raw.get("run_state")),
        "install_type": _str(raw.get("install_type")),
        "upgrade_policy": _str(raw.get("upgrade_policy")),
    }


async def _post(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    envelope: dict[str, Any],
    op_id: str,
    body: dict[str, Any],
) -> dict[str, Any] | OperationResult | None:
    """Send one write; an early answer (gate or ``refused``), or ``None`` when sent."""
    try:
        gate, _ = await _write_sub_op(
            connector,
            target,
            operator,
            op_id,
            {"vm": envelope["vm"], **body},
            timeout=_UPGRADE_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        answer = refused(envelope, exc)
        if answer is None:
            raise
        return answer
    return gate


async def _tools_upgrade(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    envelope: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any] | OperationResult:
    vm = envelope["vm"]
    before = _tools_view(await _read(connector, target, operator, _OP_GET_TOOLS, vm))
    envelope["before"] = before
    if before["version_status"] == "CURRENT":
        return {
            **envelope,
            "status": "unchanged",
            "after": before,
            "guidance": "VMware Tools is already current; nothing was sent.",
        }
    options = params.get("command_line_options")
    body = {"command_line_options": options} if isinstance(options, str) else {}
    early = await _post(connector, target, operator, envelope, _OP_UPGRADE_TOOLS, body)
    if early is not None:
        return early
    after = _tools_view(await _read(connector, target, operator, _OP_GET_TOOLS, vm))
    if after["version_status"] == "CURRENT":
        return {**envelope, "status": "upgraded", "after": after}
    return {
        **envelope,
        "status": "upgrade_started",
        "after": after,
        "guidance": "vCenter started the upgrade in the guest. Check tools_status with "
        "vmware.vm.info until it is toolsOk.",
    }


async def _tools_mount_installer(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    envelope: dict[str, Any],
) -> dict[str, Any] | OperationResult:
    vm = envelope["vm"]
    mounted = await read_tools_installer_mounted(connector, operator, target, vm)
    envelope["before"] = {"installer_mounted": mounted}
    if mounted is True:
        return {
            **envelope,
            "status": "unchanged",
            "after": envelope["before"],
            "guidance": "The Tools installer is already mounted; nothing was sent.",
        }
    early = await _post(connector, target, operator, envelope, _OP_CONNECT_TOOLS_INSTALLER, {})
    if early is not None:
        return early
    try:
        after = await read_tools_installer_mounted(connector, operator, target, vm)
    except httpx.HTTPError:
        after = None
    return {
        **envelope,
        "status": "installer_mounted",
        "after": {"installer_mounted": after},
        "guidance": "Run the installer in the guest, then unmount it with "
        "vmware.composite.vm.tools.installer.unmount.",
    }


async def vm_tools_upgrade_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Upgrade VMware Tools, or mount its installer. Op-id: ``vm.tools.upgrade``."""
    vm = check_vm_moid(params["vm"])
    mode = params.get("mode", "upgrade")
    envelope: dict[str, Any] = {
        "vm": vm,
        "mode": mode,
        "before": None,
        "after": None,
        "error_type": None,
        "guidance": None,
    }
    if mode == "mount_installer":
        if params.get("command_line_options") is not None:
            return {
                **envelope,
                "status": "invalid_request",
                "guidance": "command_line_options works only with mode upgrade; "
                "nothing was sent. Leave it out to mount the installer.",
            }
        return await _tools_mount_installer(connector, target, operator, envelope)
    return await _tools_upgrade(connector, target, operator, envelope, params)


async def _vm_tools_upgrade_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the Tools state now."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    mode = ctx.params.get("mode", "upgrade")
    try:
        vm = check_vm_moid(ctx.params["vm"])
        now = _tools_view(await _read(connector, ctx.target, ctx.operator, _OP_GET_TOOLS, vm))
    except Exception as exc:
        return {
            "summary": "MEHO could not read the VM, so it cannot show its VMware Tools state.",
            "read_error": preview_failure_reason(exc),
        }
    action = (
        "Mount the VMware Tools installer on"
        if mode == "mount_installer"
        else ("Upgrade VMware Tools on")
    )
    return {
        "summary": f"{action} {vm} (Tools now: {now['version_status']}).",
        "vm": vm,
        "mode": mode,
        "current": now,
    }


# ---------------------------------------------------------------------------
# Schemas, AI guidance and registration rows
# ---------------------------------------------------------------------------

_NULLABLE_STR: Final = {"type": ["string", "null"]}

VM_HARDWARE_UPGRADE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **VM_ID_PROPERTY,
        "version": {
            "type": "string",
            "pattern": r"^VMX_[0-9]{2}$",
            "description": (
                "Target hardware version, such as 'VMX_21' (ESXi 8.0 U2) or 'VMX_22' (ESXi "
                "9.0). Omit for the newest version the VM's host supports."
            ),
        },
    },
    "required": ["vm"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42"}, {"vm": "vm-42", "version": "VMX_21"}],
}

_HARDWARE_STATE: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "properties": {
        "version": {**_NULLABLE_STR, "description": "Hardware version, such as 'VMX_21'."},
        "upgrade_policy": {**_NULLABLE_STR, "description": "Scheduled upgrade policy."},
        "upgrade_status": {**_NULLABLE_STR, "description": "Scheduled upgrade status."},
    },
}

VM_HARDWARE_UPGRADE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["upgraded", "unchanged", "partial", "refused", "invalid_request"],
            "description": (
                "upgraded: the version went up; unchanged: already there, nothing sent; "
                "partial: sent, but the version reads back the same; refused: vCenter said "
                "no; invalid_request: MEHO sent nothing."
            ),
        },
        "vm": {"type": "string", "description": "VM id."},
        "requested_version": {**_NULLABLE_STR, "description": "The version asked for."},
        "before": {**_HARDWARE_STATE, "description": "Hardware version before."},
        "after": {**_HARDWARE_STATE, "description": "Hardware version read back after."},
        "error_type": {**_NULLABLE_STR, "description": "vCenter error type on refused."},
        "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
    },
    "required": ["status", "vm"],
}

VM_TOOLS_UPGRADE_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **VM_ID_PROPERTY,
        "mode": {
            "type": "string",
            "enum": ["upgrade", "mount_installer"],
            "description": (
                "upgrade (default): vCenter upgrades Tools in the running guest. "
                "mount_installer: put the Tools installer into the VM's CD drive to run by hand."
            ),
        },
        "command_line_options": {
            "type": "string",
            "maxLength": 1024,
            "description": "Options for the Tools installer, for mode upgrade only.",
        },
    },
    "required": ["vm"],
    "additionalProperties": False,
    "examples": [{"vm": "vm-42"}, {"vm": "vm-42", "mode": "mount_installer"}],
}

VM_TOOLS_UPGRADE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "upgraded",
                "upgrade_started",
                "installer_mounted",
                "unchanged",
                "refused",
                "invalid_request",
            ],
            "description": (
                "upgraded: Tools reads as current; upgrade_started: vCenter started it in the "
                "guest; installer_mounted: the installer is in the CD drive; unchanged: "
                "nothing to do, nothing sent; refused: vCenter said no; invalid_request: "
                "MEHO sent nothing."
            ),
        },
        "vm": {"type": "string", "description": "VM id."},
        "mode": {"type": "string", "description": "upgrade or mount_installer."},
        "before": {
            "type": ["object", "null"],
            "description": "Tools state before ({version, version_number, version_status, "
            "run_state, install_type, upgrade_policy}), or {installer_mounted} for "
            "mount_installer.",
        },
        "after": {
            "type": ["object", "null"],
            "description": "The same fields, read back after the call.",
        },
        "error_type": {**_NULLABLE_STR, "description": "vCenter error type on refused."},
        "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
    },
    "required": ["status", "vm"],
}

VM_HARDWARE_UPGRADE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to upgrade a VM's virtual hardware version (compatibility), to a given "
        "version or to the newest one its host supports, for example before using a "
        "feature that needs a newer version."
    ),
    "preconditions": (
        "The VM must be powered off. Read its version first with vmware.vm.hardware.show "
        "or this call's preview. Take a snapshot first: an upgrade cannot be undone, and "
        "older hosts can no longer run the VM. The change takes the VM id and waits for "
        "approval."
    ),
    "output_shape": (
        "{status: upgraded|unchanged|partial|refused|invalid_request, vm, "
        "requested_version, before: {version, upgrade_policy, upgrade_status}, after, "
        "error_type, guidance}."
    ),
    "next_step": (
        "Safe to run twice: a VM already at the version gets nothing. On refused, read "
        "guidance (often: power the VM off first). Then power the VM on with vm.power."
    ),
}

VM_TOOLS_UPGRADE_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to upgrade VMware Tools in a running VM whose Tools are out of date, or with "
        "mode mount_installer to put the Tools installer into the VM's CD drive so someone "
        "can install Tools by hand."
    ),
    "preconditions": (
        "The VM must be powered on. Check tools_status first with vmware.vm.info. An "
        "upgrade can restart the guest on some systems. Tools that the guest OS manages "
        "(open-vm-tools) cannot be upgraded by vCenter. The change takes the VM id and "
        "waits for approval."
    ),
    "output_shape": (
        "{status: upgraded|upgrade_started|installer_mounted|unchanged|refused|"
        "invalid_request, vm, mode, "
        "before, after, error_type, guidance}."
    ),
    "next_step": (
        "Safe to run twice: current Tools or a mounted installer get nothing. After "
        "upgrade_started, check vmware.vm.info until tools_status is toolsOk. After "
        "installer_mounted, unmount it with vmware.composite.vm.tools.installer.unmount."
    ),
}

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=OP_ID_VM_HARDWARE_UPGRADE,
        handler=vm_hardware_upgrade_composite,
        summary="Upgrade a powered-off VM's hardware version, to a given or the newest one.",
        description=(
            "Upgrades the virtual hardware version of a powered-off VM, named by its VM id, "
            "to a given version or the newest one its host supports. Sends nothing when the "
            "VM is already there. Reads the version back. Cannot be undone."
        ),
        parameter_schema=VM_HARDWARE_UPGRADE_PARAMETER_SCHEMA,
        response_schema=VM_HARDWARE_UPGRADE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "hardware", "upgrade"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_HARDWARE_UPGRADE_LLM_INSTRUCTIONS,
        preview=_vm_hardware_upgrade_preview,
    ),
    CompositeSpec(
        op_id=OP_ID_VM_TOOLS_UPGRADE,
        handler=vm_tools_upgrade_composite,
        summary="Upgrade VMware Tools in a running VM, or mount the Tools installer.",
        description=(
            "Upgrades VMware Tools in a running VM, named by its VM id, or mounts the Tools "
            "installer in its CD drive. Sends nothing when Tools is current or the installer "
            "is mounted. Reads the Tools state back."
        ),
        parameter_schema=VM_TOOLS_UPGRADE_PARAMETER_SCHEMA,
        response_schema=VM_TOOLS_UPGRADE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "vmware-tools", "upgrade"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_TOOLS_UPGRADE_LLM_INSTRUCTIONS,
        preview=_vm_tools_upgrade_preview,
    ),
)

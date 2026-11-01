# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.composite.vm.boot.set`` -- change the boot settings of one VM.

``dangerous``, waits for approval. Flow:

1. Read the boot settings now (:func:`read_boot_state`, the same read as
   ``vmware.vm.boot.show``). Refuse a request that cannot work (secure boot
   on a BIOS VM) and send nothing when every value already matches
   (``unchanged``).
2. Send only what differs, in this order:

   * secure boot **off**: VIM ``ReconfigVM_Task`` with
     ``bootOptions.efiSecureBootEnabled`` (first, so a switch to BIOS works);
   * ``PATCH /vcenter/vm/{vm}/hardware/boot``: firmware, boot delay, retry,
     retry delay, enter-setup flag;
   * ``PUT /vcenter/vm/{vm}/hardware/boot/device``: the boot order;
   * secure boot **on**: ``ReconfigVM_Task`` (last, after a switch to EFI).

3. Read the settings again and return them.

vCenter refuses a firmware or secure boot change on a powered-on VM. A REST
refusal (HTTP 400) is ``refused`` with vCenter's reason; ``applied`` lists the
steps that already ran. A ``ReconfigVM_Task`` fault raises with the vim fault
type and text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest._vm_settings_schemas import (
    BOOT_DEVICE_TYPES,
    BOOT_STATE_PROPERTIES,
)
from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.composites._vm_change_common import (
    RECONFIG_VM_TASK_CALL,
    VM_ID_PROPERTY,
    reconfigure_vm,
    refused,
    timeout_answer,
)
from meho_backplane.connectors.vmware_rest.composites._write import _write_sub_op
from meho_backplane.connectors.vmware_rest.typed_ops_vm_boot import (
    BOOT_VIM_CALLS,
    boot_order_row,
    read_boot_state,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_hardware import check_vm_moid
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = ["COMPOSITES", "SUB_OPS_VM_BOOT_SET", "vm_boot_set_composite"]

OP_ID_VM_BOOT_SET: Final = "vmware.composite.vm.boot.set"

_OP_GET_BOOT: Final = "GET:/vcenter/vm/{vm}/hardware/boot"
_OP_GET_BOOT_DEVICES: Final = "GET:/vcenter/vm/{vm}/hardware/boot/device"
_OP_PATCH_BOOT: Final = "PATCH:/vcenter/vm/{vm}/hardware/boot"
_OP_PUT_BOOT_DEVICES: Final = "PUT:/vcenter/vm/{vm}/hardware/boot/device"

#: Every call of the change: the REST reads and writes, the VIM read and the
#: ``ReconfigVM_Task``. The reconcile test checks them against the pinned API
#: files, and ``_governed_subops`` lists them for the grant set.
SUB_OPS_VM_BOOT_SET: Final[tuple[str, ...]] = (
    _OP_GET_BOOT,
    _OP_GET_BOOT_DEVICES,
    _OP_PATCH_BOOT,
    _OP_PUT_BOOT_DEVICES,
    *BOOT_VIM_CALLS,
    RECONFIG_VM_TASK_CALL,
)

#: parameter -> (``Boot.UpdateSpec`` field, field of the boot state).
_PATCH_FIELDS: Final[dict[str, tuple[str, str]]] = {
    "firmware": ("type", "firmware"),
    "delay_ms": ("delay", "delay_ms"),
    "retry": ("retry", "retry"),
    "retry_delay_ms": ("retry_delay", "retry_delay_ms"),
    "enter_setup_mode": ("enter_setup_mode", "enter_setup_mode"),
}


def _order_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """A boot order entry for the PUT body, without empty fields."""
    row = boot_order_row(entry)
    return {k: v for k, v in row.items() if v is not None}


def _same_entry(wanted: dict[str, Any], have: dict[str, Any]) -> bool:
    """Whether a boot order entry reads back as asked.

    A DISK entry asked without disks matches a DISK entry with any disks,
    because vCenter may fill in the disk ids itself.
    """
    if wanted.get("type") == "DISK" and "disks" not in wanted:
        have = {k: v for k, v in have.items() if k != "disks"}
    return wanted == have


def _plan(params: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    """What to send: ``{patch, order, secure_boot}``, each ``None`` when it already matches."""
    patch = {
        spec_field: params[param]
        for param, (spec_field, state_field) in _PATCH_FIELDS.items()
        if params.get(param) is not None and params[param] != before.get(state_field)
    }
    order: list[dict[str, Any]] | None = None
    if params.get("boot_order") is not None:
        wanted = [_order_entry(e) for e in params["boot_order"]]
        current = before.get("boot_order")
        have = None if current is None else [_order_entry(e) for e in current]
        same = (
            have is not None
            and len(wanted) == len(have)
            and all(_same_entry(w, h) for w, h in zip(wanted, have, strict=True))
        )
        order = None if same else wanted
    secure = params.get("efi_secure_boot")
    if secure is not None and secure == before.get("efi_secure_boot"):
        secure = None
    return {"patch": patch or None, "order": order, "secure_boot": secure}


def _problem(params: dict[str, Any], before: dict[str, Any]) -> str | None:
    """Why the request cannot work, or ``None``."""
    firmware = params.get("firmware") or before.get("firmware")
    if params.get("efi_secure_boot") is True and firmware != "EFI":
        return "EFI secure boot needs EFI firmware; pass firmware EFI too"
    if (
        params.get("firmware") == "BIOS"
        and before.get("efi_secure_boot") is True
        and params.get("efi_secure_boot") is not False
    ):
        return "BIOS firmware needs EFI secure boot off; pass efi_secure_boot false too"
    return None


def _matches(params: dict[str, Any], after: dict[str, Any]) -> bool:
    """Whether the settings read back as requested."""
    plan = _plan(params, after)
    return plan["patch"] is None and plan["order"] is None and plan["secure_boot"] is None


async def _secure_boot_step(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    envelope: dict[str, Any],
    enabled: bool,
) -> dict[str, Any] | OperationResult | None:
    """Run the secure boot ``ReconfigVM_Task``; an early answer, or ``None`` to go on."""
    vm = envelope["vm"]
    gate, task, timed_out = await reconfigure_vm(
        connector,
        target,
        operator,
        vm=vm,
        spec_fields={
            "bootOptions": {
                VIM_TYPE_NAME_KEY: "VirtualMachineBootOptions",
                "efiSecureBootEnabled": enabled,
            }
        },
        params={"efi_secure_boot": enabled},
        label="vm.boot.set",
    )
    if gate is not None:
        return gate
    envelope["task"] = task
    if timed_out:
        return timeout_answer(envelope, task or "", "Read the VM with vmware.vm.boot.show.")
    envelope["applied"].append("efi_secure_boot")
    return None


async def _rest_step(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    envelope: dict[str, Any],
    *,
    op_id: str,
    body: dict[str, Any],
    step: str,
) -> dict[str, Any] | OperationResult | None:
    """Run one REST write; an early answer (gate or ``refused``), or ``None`` to go on."""
    try:
        gate, _ = await _write_sub_op(
            connector, target, operator, op_id, {"vm": envelope["vm"], **body}
        )
    except httpx.HTTPError as exc:
        answer = refused(envelope, exc)
        if answer is None:
            raise
        return answer
    if gate is not None:
        return gate
    envelope["applied"].append(step)
    return None


async def _apply(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    envelope: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any] | OperationResult | None:
    """Send the planned writes in order; an early answer, or ``None`` when all ran."""
    early: dict[str, Any] | OperationResult | None = None
    if plan["secure_boot"] is False:
        early = await _secure_boot_step(connector, target, operator, envelope, False)
    if early is None and plan["patch"] is not None:
        early = await _rest_step(
            connector,
            target,
            operator,
            envelope,
            op_id=_OP_PATCH_BOOT,
            body=plan["patch"],
            step="boot_settings",
        )
    if early is None and plan["order"] is not None:
        early = await _rest_step(
            connector,
            target,
            operator,
            envelope,
            op_id=_OP_PUT_BOOT_DEVICES,
            body={"devices": plan["order"]},
            step="boot_order",
        )
    if early is None and plan["secure_boot"] is True:
        early = await _secure_boot_step(connector, target, operator, envelope, True)
    return early


async def vm_boot_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Change the boot settings of one VM. Op-id: ``vmware.composite.vm.boot.set``."""
    vm = check_vm_moid(params["vm"])
    before = await read_boot_state(connector, operator, target, vm, [])
    envelope: dict[str, Any] = {
        "vm": vm,
        "applied": [],
        "before": before,
        "after": None,
        "task": None,
        "error_type": None,
        "guidance": None,
    }
    problem = _problem(params, before)
    if problem is not None:
        return {**envelope, "status": "invalid_request", "guidance": problem}
    plan = _plan(params, before)
    if all(value is None for value in plan.values()):
        return {
            **envelope,
            "status": "unchanged",
            "after": before,
            "guidance": "The boot settings already match; nothing was sent.",
        }
    early = await _apply(connector, target, operator, envelope, plan)
    if early is not None:
        return early
    after = await read_boot_state(connector, operator, target, vm, [])
    if not _matches(params, after):
        return {
            **envelope,
            "status": "partial",
            "after": after,
            "guidance": "vCenter took the change, but some settings read back different. "
            "Compare before and after.",
        }
    return {**envelope, "status": "set", "after": after}


async def _vm_boot_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the boot settings now and what would change."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    try:
        vm = check_vm_moid(ctx.params["vm"])
        current = await read_boot_state(connector, ctx.operator, ctx.target, vm, [])
    except Exception as exc:
        return {
            "summary": "MEHO could not read the VM, so it cannot show its boot settings.",
            "read_error": preview_failure_reason(exc),
        }
    plan = _plan(ctx.params, current)
    changes = [name for name, value in plan.items() if value is not None]
    summary = (
        f"Change the boot settings of {vm}: {', '.join(changes)}."
        if changes
        else f"The boot settings of {vm} already match; the call will change nothing."
    )
    return {"summary": summary, "vm": vm, "current": current, "changes": plan}


# ---------------------------------------------------------------------------
# Schemas, AI guidance and the registration row
# ---------------------------------------------------------------------------

_BOOT_ORDER_INPUT: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": list(BOOT_DEVICE_TYPES),
            "description": "Boot device kind. Each kind once, except ETHERNET.",
        },
        "nic": {
            "type": "string",
            "pattern": r"^[0-9]+$",
            "description": "Network adapter id, such as '4000'; for ETHERNET only.",
        },
        "disks": {
            "type": "array",
            "items": {
                "type": "string",
                "pattern": r"^[0-9]+$",
                "description": "Disk id, such as '2000'.",
            },
            "description": "Disk ids in boot order; for DISK only.",
        },
    },
    "required": ["type"],
    "additionalProperties": False,
}

_CHANGE_FIELDS: Final[tuple[str, ...]] = (
    "firmware",
    "efi_secure_boot",
    "delay_ms",
    "retry",
    "retry_delay_ms",
    "enter_setup_mode",
    "boot_order",
)

VM_BOOT_SET_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **VM_ID_PROPERTY,
        "firmware": {
            "type": "string",
            "enum": ["BIOS", "EFI"],
            "description": "Firmware. The VM must be powered off. The guest OS may not boot "
            "after a switch.",
        },
        "efi_secure_boot": {
            "type": "boolean",
            "description": "EFI secure boot on or off. Needs EFI firmware and a powered-off VM.",
        },
        "delay_ms": {
            "type": "integer",
            "minimum": 0,
            "description": "Wait before the boot starts, in milliseconds.",
        },
        "retry": {"type": "boolean", "description": "Retry the boot after a failure."},
        "retry_delay_ms": {
            "type": "integer",
            "minimum": 0,
            "description": "Wait before a boot retry, in milliseconds.",
        },
        "enter_setup_mode": {
            "type": "boolean",
            "description": "Enter the firmware setup on the next boot (resets after it).",
        },
        "boot_order": {
            "type": "array",
            "items": _BOOT_ORDER_INPUT,
            "description": "Boot devices in order. An empty list means vCenter's default order.",
        },
    },
    "required": ["vm"],
    "anyOf": [{"required": [name]} for name in _CHANGE_FIELDS],
    "additionalProperties": False,
    "examples": [
        {"vm": "vm-42", "delay_ms": 5000, "enter_setup_mode": True},
        {"vm": "vm-42", "boot_order": [{"type": "CDROM"}, {"type": "DISK", "disks": ["2000"]}]},
        {"vm": "vm-42", "firmware": "EFI", "efi_secure_boot": True},
    ],
}

_NULLABLE_STR: Final = {"type": ["string", "null"]}

_BOOT_STATE: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "properties": BOOT_STATE_PROPERTIES,
}

VM_BOOT_SET_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["set", "unchanged", "partial", "refused", "invalid_request", "timeout"],
            "description": (
                "set: done and read back; unchanged: already as asked, nothing sent; partial: "
                "sent, but some settings read back different; refused: vCenter said no; "
                "invalid_request: MEHO sent nothing; timeout: the task did not finish in time."
            ),
        },
        "vm": {"type": "string", "description": "VM id."},
        "applied": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["efi_secure_boot", "boot_settings", "boot_order"],
                "description": "One step that ran.",
            },
            "description": "Steps that ran, in order. On refused, the steps before the refusal.",
        },
        "before": {**_BOOT_STATE, "description": "Boot settings before the change."},
        "after": {**_BOOT_STATE, "description": "Boot settings read back after the change."},
        "task": {**_NULLABLE_STR, "description": "The ReconfigVM_Task id, when one ran."},
        "error_type": {**_NULLABLE_STR, "description": "vCenter error type on refused."},
        "guidance": {**_NULLABLE_STR, "description": "What happened and what to do next."},
    },
    "required": ["status", "vm"],
}

VM_BOOT_SET_LLM_INSTRUCTIONS: Final[dict[str, Any]] = {
    "when_to_call": (
        "Call to change how a VM boots: boot delay, enter the firmware setup on the next "
        "boot, boot retry, the boot order (for example CD drive first to boot an ISO), and, "
        "with the VM powered off, the firmware (BIOS or EFI) and EFI secure boot."
    ),
    "preconditions": (
        "Read the VM first with vmware.vm.boot.show. The change takes the VM id, not a "
        "name. Firmware and secure boot changes need the VM powered off; secure boot needs "
        "EFI. Disk ids for the boot order come from vmware.vm.hardware.show. The change "
        "waits for approval."
    ),
    "parameter_hints": {
        "boot_order": "[{type: CDROM}, {type: DISK, disks: ['2000']}, {type: ETHERNET, "
        "nic: '4000'}]; [] restores vCenter's default order.",
        "firmware": "BIOS or EFI; a switch can stop the guest OS from booting.",
    },
    "output_shape": (
        "{status: set|unchanged|partial|refused|invalid_request|timeout, vm, applied, "
        "before, after (same fields as vmware.vm.boot.show), task, error_type, guidance}."
    ),
    "next_step": (
        "Safe to run twice: only settings that differ are sent. On refused, read guidance "
        "(often: power the VM off first) and try again. Check with vmware.vm.boot.show."
    ),
}

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=OP_ID_VM_BOOT_SET,
        handler=vm_boot_set_composite,
        summary="Change a VM's boot delay, setup flag, boot order, firmware or secure boot.",
        description=(
            "Changes the boot settings of a VM, named by its VM id: boot delay, enter setup "
            "on the next boot, boot retry, boot order and, with the VM off, firmware (BIOS or "
            "EFI) and EFI secure boot. Sends only what differs and reads the settings back."
        ),
        parameter_schema=VM_BOOT_SET_PARAMETER_SCHEMA,
        response_schema=VM_BOOT_SET_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "boot"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_BOOT_SET_LLM_INSTRUCTIONS,
        preview=_vm_boot_set_preview,
        classifier_other=True,
    ),
)

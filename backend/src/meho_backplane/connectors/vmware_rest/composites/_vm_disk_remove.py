# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.composite.vm.disk.remove`` -- take one virtual disk off a VM.

The change is ``destructive``: a second person always approves it, after a
preview. vSphere REST can remove a disk (``DELETE .../hardware/disk/{disk}``)
but always keeps the file, so MEHO uses the VIM API, like ``govc
device.remove``: one ``VirtualMachine.ReconfigVM_Task`` with a
``VirtualDeviceConfigSpec`` ``remove`` of the disk device, and
``fileOperation: destroy`` when ``delete_file`` is set. It runs as VI-JSON on
vCenter 8.0 U1 and newer, through the governed seam
(:func:`._write._write_vmomi_sub_op`), and the task is polled to its end.

It uses the plan-then-act seam of :mod:`._teardown`: one read-only plan (the
VM's ``config.hardware.device``) feeds both the approval preview and the
handler, so the approver sees the decision the handler makes. The preview
refuses (raises) when the VM or the disk does not exist, so such a call never
waits for approval. After the task the handler reads the devices again and
returns the disks the VM has left.

The approved run checks the disk again. vCenter can give a new disk the key
of a removed one, so a disk added while the request waited could carry the
approved key, and even the same file path, label and size. The disk's own
UUID (``backing.uuid`` of the VIM device) is the identity that a replacement
cannot copy. The preview stores it, and the handler compares the live disk's
UUID and file with the preview the approver saw
(:func:`~meho_backplane.operations._preview.approved_preview`). It refuses
before any change when they differ, or when the preview holds no UUID. A disk
whose backing has no UUID (a raw device mapping in physical mode, or a very old
file format) is refused at plan time: MEHO cannot check it after approval.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._teardown import (
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
    STATUS_STILL_PRESENT,
    TeardownPlan,
    blast_radius,
    envelope,
    timeout_envelope,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._vm_devices_schemas import (
    DISK_ID_PATTERN,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RECONFIG_VM_TASK,
    _OP_RETRIEVE_PROPERTIES,
    _PROP_CONFIG_HARDWARE_DEVICE,
    _VIM_DISK_SHARING_MULTI_WRITER,
    _VIRTUAL_DEVICE_CONFIG_SPEC_TYPE,
    _VIRTUAL_DISK_TYPE,
    _VIRTUAL_MACHINE_MO_TYPE,
    _VM_CONFIG_SPEC_TYPE,
    _coerce_int,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import VM_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task
from meho_backplane.operations._preview import approved_preview

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "OP_ID_VM_DISK_REMOVE",
    "plan_disk_remove",
    "vm_disk_remove_composite",
    "vm_disk_remove_preview",
]

OP_ID_VM_DISK_REMOVE: Final = "vmware.composite.vm.disk.remove"

#: The VIM calls (``_VIM_SUB_OPS_*`` names, so the vcenter.yaml sweep skips
#: them; the vi-json reconcile lane checks each path).
_VIM_SUB_OPS_VM_DISK_REMOVE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_RECONFIG_VM_TASK,
)

_PROP_POWER_STATE: Final = "runtime.powerState"
_VM_PROPS: Final = ["name", _PROP_POWER_STATE, _PROP_CONFIG_HARDWARE_DEVICE]

#: ``VirtualDeviceConfigSpecFileOperation`` value that deletes the backing file.
_FILE_OPERATION_DESTROY: Final = "destroy"

#: Irreversibility class when the VMDK file stays on the datastore.
_FILE_KEPT: Final = "reversible-file-kept"

STATUS_REMOVED: Final = "removed"
STATUS_NOT_FOUND: Final = "not_found"

#: The fields of the plan's ``object`` that say which disk it is: the disk's
#: UUID (``backing.uuid``) and its file. The approved run compares them with
#: the approval preview.
_DISK_IDENTITY_FIELDS: Final[tuple[str, ...]] = ("disk_uuid", "vmdk_file")

#: Wall-clock bound for the task poll -- the 600 s convention; module-global so
#: tests can zero it.
_TASK_TIMEOUT_SECONDS = 600.0


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _disks(devices: Any) -> list[dict[str, Any]]:
    """The ``VirtualDisk`` devices of a ``config.hardware.device`` list."""
    if not isinstance(devices, list):
        return []
    return [
        d for d in devices if isinstance(d, dict) and d.get(VIM_TYPE_NAME_KEY) == _VIRTUAL_DISK_TYPE
    ]


def _disk_row(device: dict[str, Any]) -> dict[str, Any]:
    info = device.get("deviceInfo")
    backing = device.get("backing")
    return {
        "disk": str(device.get("key")),
        "label": _str(info.get("label")) if isinstance(info, dict) else None,
        "capacity_bytes": _coerce_int(device.get("capacityInBytes")),
        "vmdk_file": _str(backing.get("fileName")) if isinstance(backing, dict) else None,
        "disk_uuid": _str(backing.get("uuid")) if isinstance(backing, dict) else None,
    }


async def plan_disk_remove(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> TeardownPlan:
    """Plan a disk removal (read-only).

    Both ids are checked before they reach a VIM path. An unknown VM or disk is
    a ``not_found`` refusal. With ``delete_file`` the VMDK file is the one child
    that goes with the disk. A disk without a UUID is refused: the approved run
    could not check that it is still the disk the approver saw. A shared
    (multi-writer) disk is refused with ``delete_file``: other VMs may still
    use the file.
    """
    vm = params["vm"]
    disk = str(params["disk"])
    delete_file = bool(params.get("delete_file", False))
    obj: dict[str, Any] = {"kind": "vm_disk", "vm": vm, "disk": disk, "delete_file": delete_file}
    problem = moid_problem(vm, VM_MOID_PATTERN, "vm") or moid_problem(disk, DISK_ID_PATTERN, "disk")
    if problem is not None:
        return TeardownPlan(obj, False, refusal=(STATUS_INVALID_REQUEST, problem))
    props = await read_object(
        connector, target, operator, mo_type=_VIRTUAL_MACHINE_MO_TYPE, moid=vm, props=_VM_PROPS
    )
    if props is None:
        return TeardownPlan(obj, False, refusal=(STATUS_NOT_FOUND, f"no VM {vm!r}"))
    obj["vm_name"] = _str(props.get("name"))
    obj["power_state"] = _str(props.get(_PROP_POWER_STATE))
    device = next(
        (d for d in _disks(props.get(_PROP_CONFIG_HARDWARE_DEVICE)) if str(d.get("key")) == disk),
        None,
    )
    if device is None:
        guidance = f"VM {vm!r} has no disk {disk!r}; list its disks with vmware.vm.hardware.show"
        return TeardownPlan(obj, False, refusal=(STATUS_NOT_FOUND, guidance))
    row = _disk_row(device)
    backing = device.get("backing")
    sharing = _str(backing.get("sharing")) if isinstance(backing, dict) else None
    obj.update(
        label=row["label"],
        capacity_bytes=row["capacity_bytes"],
        vmdk_file=row["vmdk_file"],
        disk_uuid=row["disk_uuid"],
        sharing=sharing,
        controller_key=_coerce_int(device.get("controllerKey")),
        unit_number=_coerce_int(device.get("unitNumber")),
    )
    plan = TeardownPlan(object=obj, present=True, context={"device": device})
    if row["disk_uuid"] is None:
        plan.blockers = [{"kind": "disk_without_uuid"}]
        plan.refusal = (
            STATUS_PRECONDITION_FAILED,
            f"disk {disk!r} of VM {vm!r} has no UUID, so MEHO could not check after approval "
            "that it is still the disk the approver saw; remove it in the vSphere Client",
        )
        return plan
    if delete_file:
        plan.children = [
            {"kind": "vmdk_file", "file": row["vmdk_file"], "capacity_bytes": row["capacity_bytes"]}
        ]
        if sharing == _VIM_DISK_SHARING_MULTI_WRITER:
            plan.blockers = [{"kind": "shared_disk", "sharing": sharing}]
            plan.refusal = (
                STATUS_PRECONDITION_FAILED,
                "the disk is shared (multi-writer), so other VMs may use its file; remove "
                "it without delete_file, and delete the file only when no VM uses it",
            )
    return plan


def _approved_disk_blocker(plan: TeardownPlan) -> dict[str, Any] | None:
    """The blocker when the live disk is not the one the approver saw, else ``None``.

    ``None`` on an ordinary dispatch (no approval involved) and when the UUID
    and the file of the disk match the approval preview. An approval whose
    preview shows no disk, or a disk without a UUID, counts as changed: nothing
    is removed on a preview MEHO cannot check.
    """
    preview = approved_preview(OP_ID_VM_DISK_REMOVE)
    if preview is None:
        return None
    seen = (preview.get("blast_radius") or {}).get("object")
    approved = (
        {name: seen.get(name) for name in _DISK_IDENTITY_FIELDS} if isinstance(seen, dict) else None
    )
    now = {name: plan.object.get(name) for name in _DISK_IDENTITY_FIELDS}
    if approved == now:
        return None
    return {"kind": "disk_changed", "approved": approved, "now": now}


def _remove_body(device: dict[str, Any], *, delete_file: bool) -> dict[str, Any]:
    """The ``ReconfigVM_Task`` body: one ``remove`` of the disk device read before."""
    change: dict[str, Any] = {
        VIM_TYPE_NAME_KEY: _VIRTUAL_DEVICE_CONFIG_SPEC_TYPE,
        "operation": "remove",
        "device": device,
    }
    if delete_file:
        change["fileOperation"] = _FILE_OPERATION_DESTROY
    return {"spec": {VIM_TYPE_NAME_KEY: _VM_CONFIG_SPEC_TYPE, "deviceChange": [change]}}


def _result(plan: TeardownPlan, status: str, **fields: Any) -> dict[str, Any]:
    remaining = fields.pop("remaining_disks", None)
    return {**envelope(plan, status, **fields), "remaining_disks": remaining}


def _disk_changed_result(plan: TeardownPlan, blocker: dict[str, Any]) -> dict[str, Any]:
    """The refusal of an approved run whose disk is not the one the approver saw."""
    plan.blockers = [blocker]
    approved = blocker["approved"] or {}
    guidance = (
        f"disk {plan.object['disk']!r} of VM {plan.object['vm']!r} is not the disk the approver "
        f"saw (approved UUID {approved.get('disk_uuid')!r} and file {approved.get('vmdk_file')!r}, "
        f"now UUID {plan.object['disk_uuid']!r} and file {plan.object['vmdk_file']!r}); nothing "
        "was sent. Preview the call again and ask for a new approval."
    )
    return _result(plan, STATUS_PRECONDITION_FAILED, guidance=guidance)


async def vm_disk_remove_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Remove one disk from a VM, and its file when asked.

    Op-id: ``vmware.composite.vm.disk.remove``. Plans first: a refusal
    (bad id, unknown VM or disk, disk without a UUID, shared disk with
    ``delete_file``) returns before any change. On the approved run, a disk that is not the one the
    approver saw is refused too (``precondition_failed``, blocker
    ``disk_changed``). Then the gated ``ReconfigVM_Task`` (a parked or denied
    gate is returned as is), polled to its end. A task fault raises with the
    fault type and text (``connector_error``, audited as failed). After the
    task the devices are read again: ``removed`` when the disk is gone.
    """
    plan = await plan_disk_remove(connector, target, operator, params)
    if plan.refusal is not None:
        status, guidance = plan.refusal
        return _result(plan, status, guidance=guidance)
    vm, disk = plan.object["vm"], plan.object["disk"]
    delete_file = bool(plan.object["delete_file"])
    blocker = _approved_disk_blocker(plan)
    if blocker is not None:
        return _disk_changed_result(plan, blocker)
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_RECONFIG_VM_TASK,
        vmomi_path=f"/{_VIRTUAL_MACHINE_MO_TYPE}/{vm}/ReconfigVM_Task",
        body=_remove_body(plan.context["device"], delete_file=delete_file),
        params={"vm": vm, "disk": disk, "delete_file": delete_file},
    )
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"vm.disk.remove: ReconfigVM_Task on vm {vm!r} disk {disk!r} faulted "
            f"({outcome.fault_type or 'unknown fault'}): "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return {
            **timeout_envelope(
                plan,
                outcome,
                method="ReconfigVM_Task",
                reread="read the VM's disks with vmware.vm.hardware.show",
            ),
            "remaining_disks": None,
        }
    after = await read_object(
        connector,
        target,
        operator,
        mo_type=_VIRTUAL_MACHINE_MO_TYPE,
        moid=vm,
        props=[_PROP_CONFIG_HARDWARE_DEVICE],
    )
    rows = [_disk_row(d) for d in _disks((after or {}).get(_PROP_CONFIG_HARDWARE_DEVICE))]
    task = {"task": outcome.task, "task_state": outcome.state, "remaining_disks": rows}
    if any(row["disk"] == disk for row in rows):
        guidance = "ReconfigVM_Task succeeded but the disk still reads back; read the VM again"
        return _result(plan, STATUS_STILL_PRESENT, guidance=guidance, **task)
    return _result(plan, STATUS_REMOVED, **task)


async def vm_disk_remove_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius of ``vm.disk.remove`` from the handler's own plan.

    Raises when the plan refuses (unknown VM or disk, bad id, disk without a
    UUID, shared disk with ``delete_file``), so such a call never waits for
    approval. Declines
    (``None``) without a live connector.
    """
    if ctx.connector_instance is None:
        return None
    plan = await plan_disk_remove(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {
            "vm": ctx.params.get("vm"),
            "disk": ctx.params.get("disk"),
            "delete_file": bool(ctx.params.get("delete_file", False)),
        },
    )
    if plan.refusal is not None:
        raise ValueError(f"vm.disk.remove refused: {plan.refusal[1]}")
    preview = blast_radius(plan)
    block = preview["blast_radius"]
    if plan.object["delete_file"]:
        block["summary"] = (
            f"Remove {plan.object['label'] or 'disk ' + plan.object['disk']} from "
            f"{plan.object['vm']} and delete its file. The data is gone for good."
        )
    else:
        block["irreversibility"] = _FILE_KEPT
        block["summary"] = (
            f"Remove {plan.object['label'] or 'disk ' + plan.object['disk']} from "
            f"{plan.object['vm']}. The file {plan.object['vmdk_file']} stays and can be "
            "attached again."
        )
    return preview

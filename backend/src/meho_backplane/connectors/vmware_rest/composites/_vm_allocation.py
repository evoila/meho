# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vm.resource_allocation.show`` / ``.set`` -- a VM's CPU + memory limit / reservation.

A VM's resource *allocation* (``VirtualMachineConfigInfo.cpuAllocation`` /
``memoryAllocation``, each a vim ``ResourceAllocationInfo``: ``limit`` /
``reservation`` / ``shares``) has no REST expression: the pinned
``vcenter.yaml`` ``hardware/cpu`` + ``hardware/memory`` resources carry the
vCPU count and memory size only, and ``vmware.vm.info`` does not project the
allocation either. The ingested vim-object ``ReconfigVM_Task`` binding is
routed under ``/api`` and 404s (#3534), so both ops ride the composites'
documented ``/sdk/vim25/{release}`` VI-JSON seam instead:

* ``show`` -- one ungated ``RetrievePropertiesEx`` read of ``name`` +
  ``config.cpuAllocation`` + ``config.memoryAllocation``.
* ``set`` -- the same read (the *before*), a pre-write validation, one
  ``ReconfigVM_Task`` whose ``VirtualMachineConfigSpec`` carries only the
  requested ``ResourceAllocationInfo`` fields (unset fields are left
  unchanged by vSphere; ``limit=-1`` means unlimited), gated through the
  shared :func:`._write._write_vmomi_sub_op` seam (#2254), polled to a
  terminal state, then re-read (the *after*).

The two vim methods are the same ``RetrievePropertiesEx`` / ``ReconfigVM_Task``
pair ``vm.disk.grow`` already reconciles against the pinned ``vi-json.yaml``,
so no new vim op_id is introduced.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RECONFIG_VM_TASK,
    _OP_RETRIEVE_PROPERTIES,
    _VM_CONFIG_SPEC_TYPE,
    _VMOMI_RETRIEVE_PROPERTIES_PATH,
    _coerce_int,
    _extract_single_prop,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    retrieve_properties_body,
)
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "read_vm_allocation",
    "vm_resource_allocation_set_composite",
    "vm_resource_allocation_show_composite",
]

#: vi-json sub-op manifest for ``vm.resource_allocation.set`` -- the config
#: read (before / after + the Task poll) and the gated reconfigure.
_VIM_SUB_OPS_VM_RESOURCE_ALLOCATION_SET: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_RECONFIG_VM_TASK,
)

_VIRTUAL_MACHINE_MO_TYPE: Final = "VirtualMachine"
_RESOURCE_ALLOCATION_INFO_TYPE: Final = "ResourceAllocationInfo"
_PROP_NAME: Final = "name"
_PROP_CPU_ALLOCATION: Final = "config.cpuAllocation"
_PROP_MEMORY_ALLOCATION: Final = "config.memoryAllocation"

#: ``limit`` value vSphere reads as "no limit".
UNLIMITED: Final = -1

# Default wall-clock bound for the ReconfigVM_Task poll -- the 600s
# convention; module-global so tests can zero it.
_RESOURCE_ALLOCATION_TASK_TIMEOUT_SECONDS = 600.0

#: request param -> (ConfigSpec allocation field, ResourceAllocationInfo field).
_PARAM_FIELDS: Final[dict[str, tuple[str, str]]] = {
    "cpu_limit_mhz": ("cpuAllocation", "limit"),
    "cpu_reservation_mhz": ("cpuAllocation", "reservation"),
    "memory_limit_mb": ("memoryAllocation", "limit"),
    "memory_reservation_mb": ("memoryAllocation", "reservation"),
}
#: ConfigSpec allocation field -> the response-envelope key.
_ENVELOPE_KEY: Final[dict[str, str]] = {
    "cpuAllocation": "cpu_allocation",
    "memoryAllocation": "memory_allocation",
}


def _allocation_view(raw: Any) -> dict[str, Any] | None:
    """Project a vim ``ResourceAllocationInfo`` to ``{limit, reservation, shares}``.

    ``limit`` ``-1`` = unlimited. ``shares`` is the ``SharesInfo``
    ``{level, shares}`` pair. ``None`` when the property was not returned.
    """
    if not isinstance(raw, dict):
        return None
    shares_raw = raw.get("shares")
    shares = (
        {"level": shares_raw.get("level"), "shares": _coerce_int(shares_raw.get("shares"))}
        if isinstance(shares_raw, dict)
        else None
    )
    return {
        "limit": _coerce_int(raw.get("limit")),
        "reservation": _coerce_int(raw.get("reservation")),
        "shares": shares,
    }


async def read_vm_allocation(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vm: str,
) -> dict[str, Any] | None:
    """Read ``{name, cpu_allocation, memory_allocation}`` for one VM, or ``None``.

    One ungated ``RetrievePropertiesEx`` on the VI-JSON seam. ``None`` when
    neither allocation came back (unknown moid / no readable config).
    Transport faults propagate (the dispatcher wraps ``connector_error``).
    """
    result = await connector._post_vmomi_json(
        target,
        _VMOMI_RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=retrieve_properties_body(
            _VIRTUAL_MACHINE_MO_TYPE,
            [vm],
            [_PROP_NAME, _PROP_CPU_ALLOCATION, _PROP_MEMORY_ALLOCATION],
        ),
    )
    cpu = _allocation_view(_extract_single_prop(result, _PROP_CPU_ALLOCATION))
    memory = _allocation_view(_extract_single_prop(result, _PROP_MEMORY_ALLOCATION))
    if cpu is None and memory is None:
        return None
    name = _extract_single_prop(result, _PROP_NAME)
    return {
        "name": name if isinstance(name, str) else None,
        "cpu_allocation": cpu,
        "memory_allocation": memory,
    }


def _vm_not_found(vm: str) -> dict[str, Any]:
    return {
        "status": "vm_not_found",
        "guidance": (
            f"no readable config.cpuAllocation / config.memoryAllocation on vm {vm!r}; "
            "confirm the VM moid (e.g. via vmware.vm.info or GET:/vcenter/vm)"
        ),
    }


async def vm_resource_allocation_show_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Return one VM's CPU (MHz) + memory (MB) limit / reservation / shares.

    Op-id: ``vmware.composite.vm.resource_allocation.show``. Read-only.
    """
    vm = params["vm"]
    current = await read_vm_allocation(connector, target, operator, vm=vm)
    if current is None:
        return {
            "vm": vm,
            "name": None,
            "cpu_allocation": None,
            "memory_allocation": None,
            **_vm_not_found(vm),
        }
    return {"status": "ok", "vm": vm, **current, "guidance": None}


def _requested(params: dict[str, Any]) -> dict[str, int]:
    """The allocation params actually passed (``None`` counts as absent)."""
    return {key: int(params[key]) for key in _PARAM_FIELDS if params.get(key) is not None}


def _projected(current: dict[str, Any], requested: dict[str, int]) -> dict[str, dict[str, Any]]:
    """Merge the requested fields over the current ``{limit, reservation}`` views."""
    merged: dict[str, dict[str, Any]] = {}
    for spec_field, env_key in _ENVELOPE_KEY.items():
        base = current.get(env_key) or {}
        merged[spec_field] = {
            "limit": base.get("limit"),
            "reservation": base.get("reservation"),
        }
    for param, (spec_field, info_field) in _PARAM_FIELDS.items():
        if param in requested:
            merged[spec_field][info_field] = requested[param]
    return merged


def _validation_error(
    requested: dict[str, int], projected: dict[str, dict[str, Any]]
) -> str | None:
    """Pre-write check: at least one field; a set limit must be >= the reservation."""
    if not requested:
        return (
            "pass at least one of cpu_limit_mhz, cpu_reservation_mhz, memory_limit_mb, "
            "memory_reservation_mb"
        )
    for param, value in requested.items():
        _, info_field = _PARAM_FIELDS[param]
        floor = UNLIMITED if info_field == "limit" else 0
        if value < floor:
            return (
                f"{param}={value} is out of range (limits: -1 = unlimited or >= 0; "
                "reservations: >= 0)"
            )
    for spec_field, unit in (("cpuAllocation", "MHz"), ("memoryAllocation", "MB")):
        limit = projected[spec_field]["limit"]
        reservation = projected[spec_field]["reservation"]
        if (
            isinstance(limit, int)
            and isinstance(reservation, int)
            and limit != UNLIMITED
            and limit < reservation
        ):
            return (
                f"the resulting {spec_field} limit ({limit} {unit}) would be below its "
                f"reservation ({reservation} {unit}); vSphere rejects that -- lower the "
                "reservation in the same call or pick a higher limit"
            )
    return None


def _build_reconfig_body(requested: dict[str, int]) -> dict[str, Any]:
    """A ``VirtualMachineConfigSpec`` carrying ONLY the requested allocation fields."""
    spec: dict[str, Any] = {VIM_TYPE_NAME_KEY: _VM_CONFIG_SPEC_TYPE}
    for param, value in requested.items():
        spec_field, info_field = _PARAM_FIELDS[param]
        alloc = spec.setdefault(spec_field, {VIM_TYPE_NAME_KEY: _RESOURCE_ALLOCATION_INFO_TYPE})
        alloc[info_field] = value
    return {"spec": spec}


def _precheck(
    envelope: dict[str, Any],
    requested: dict[str, int],
    current: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a pre-write refusal / no-op envelope, or ``None`` to proceed.

    Mutates *envelope* with ``name`` + ``before`` once the VM read resolved.
    """
    if not requested:
        return {**envelope, "status": "invalid_request", "guidance": _validation_error({}, {})}
    if current is None:
        return {**envelope, **_vm_not_found(envelope["vm"])}
    before = {
        "cpu_allocation": current["cpu_allocation"],
        "memory_allocation": current["memory_allocation"],
    }
    envelope.update(name=current["name"], before=before)
    problem = _validation_error(requested, _projected(current, requested))
    if problem is not None:
        return {**envelope, "status": "invalid_request", "guidance": problem}
    already = all(
        (current.get(_ENVELOPE_KEY[spec_field]) or {}).get(info_field) == requested[param]
        for param, (spec_field, info_field) in _PARAM_FIELDS.items()
        if param in requested
    )
    if already:
        return {
            **envelope,
            "status": "unchanged",
            "after": before,
            "guidance": "the VM's allocation already matches the request; no task was issued",
        }
    return None


def _task_outcome_envelope(envelope: dict[str, Any], outcome: Any) -> dict[str, Any] | None:
    """Map a faulted / timed-out Task to its envelope; ``None`` on success."""
    envelope.update(task=outcome.task, task_state=outcome.state)
    if outcome.state == TASK_STATE_ERROR:
        return {
            **envelope,
            "status": "task_failed",
            "error": outcome.error_message or "<no fault reported>",
            "guidance": (
                f"ReconfigVM_Task {outcome.task} faulted; the allocation is unchanged -- "
                "read the error, adjust the request, and retry"
            ),
        }
    if outcome.timed_out:
        return {
            **envelope,
            "status": "timeout",
            "guidance": (
                f"ReconfigVM_Task {outcome.task} did not reach a terminal state within "
                f"{int(_RESOURCE_ALLOCATION_TASK_TIMEOUT_SECONDS)}s; re-read with "
                "vmware.composite.vm.resource_allocation.show -- it may still complete"
            ),
        }
    return None


async def vm_resource_allocation_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Set / clear one VM's CPU + memory limit / reservation via ``ReconfigVM_Task``.

    Op-id: ``vmware.composite.vm.resource_allocation.set``.

    Flow: read the current allocation (``before``) -> refuse an empty or
    inconsistent request (``invalid_request``) and a no-op
    (``unchanged``) before any write -> gated ``ReconfigVM_Task`` carrying
    only the requested fields -> poll to terminal -> re-read (``after``).
    A policy gate returns its :class:`OperationResult` verbatim (nothing on
    the wire); a task fault returns ``status='task_failed'`` with the vim
    fault message; a poll timeout returns ``status='timeout'``.
    """
    vm = params["vm"]
    requested = _requested(params)
    envelope: dict[str, Any] = {
        "vm": vm,
        "name": None,
        "requested": requested,
        "before": None,
        "after": None,
        "task": None,
        "task_state": None,
        "error": None,
        "guidance": None,
    }
    current = await read_vm_allocation(connector, target, operator, vm=vm) if requested else None
    refusal = _precheck(envelope, requested, current)
    if refusal is not None:
        return refusal

    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_RECONFIG_VM_TASK,
        vmomi_path=f"/{_VIRTUAL_MACHINE_MO_TYPE}/{vm}/ReconfigVM_Task",
        body=_build_reconfig_body(requested),
        params={"vm": vm, **requested},
    )
    if gate is not None:
        return gate

    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_RESOURCE_ALLOCATION_TASK_TIMEOUT_SECONDS,
    )
    failed = _task_outcome_envelope(envelope, outcome)
    if failed is not None:
        return failed

    after_read = await read_vm_allocation(connector, target, operator, vm=vm)
    after = (
        {
            "cpu_allocation": after_read["cpu_allocation"],
            "memory_allocation": after_read["memory_allocation"],
        }
        if after_read is not None
        else None
    )
    return {**envelope, "status": "set", "after": after}

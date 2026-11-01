# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vm.cpu_memory.options.set`` -- turn CPU and memory hot add on or off, set shares.

One change for four VM settings: ``cpuHotAddEnabled``, ``memoryHotAddEnabled``
and the ``shares`` of ``cpuAllocation`` and ``memoryAllocation``. The vSphere
REST API has no field for shares, so the change uses the VIM API (VI-JSON on
``/sdk/vim25/{release}``) for all four, in one ``ReconfigVM_Task``. One task
means one approval and no half-done state between two calls.

Flow: one ungated ``RetrievePropertiesEx`` read (the *before*), a check of
the request, one gated ``ReconfigVM_Task`` that carries only the settings that
change, a poll of the task to its end, and a second read (the *after*).
vSphere changes a hot add setting only while the VM is powered off; the
change then answers ``requires_power_off`` and sends nothing. Shares change on
a running VM too.

The two VIM methods are the ``RetrievePropertiesEx`` / ``ReconfigVM_Task``
pair that ``vm.disk.grow`` and ``vm.resource_allocation.set`` already use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest.composites._vm_device_schemas import (
    VM_CPU_MEMORY_OPTIONS_SET_LLM_INSTRUCTIONS,
    VM_CPU_MEMORY_OPTIONS_SET_PARAMETER_SCHEMA,
    VM_CPU_MEMORY_OPTIONS_SET_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RECONFIG_VM_TASK,
    _OP_RETRIEVE_PROPERTIES,
    _VM_CONFIG_SPEC_TYPE,
    _VMOMI_RETRIEVE_PROPERTIES_PATH,
    _coerce_int,
    _extract_single_prop,
    _task_timeout_issue,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_hardware import check_vm_moid
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    retrieve_properties_body,
)
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task
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
    "VM_CPU_MEMORY_OPTIONS_COMPOSITES",
    "read_vm_cpu_memory_options",
    "vm_cpu_memory_options_set_composite",
]

OP_ID_VM_CPU_MEMORY_OPTIONS_SET: Final = "vmware.composite.vm.cpu_memory.options.set"

#: VIM calls of the change: the read (before, after, task poll) and the reconfigure.
VIM_SUB_OPS_VM_CPU_MEMORY_OPTIONS_SET: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_RECONFIG_VM_TASK,
)

RESOURCE_ALLOCATION_INFO_TYPE: Final = "ResourceAllocationInfo"
SHARES_INFO_TYPE: Final = "SharesInfo"
_VIRTUAL_MACHINE: Final = "VirtualMachine"
_POWERED_OFF: Final = "poweredOff"
_CUSTOM: Final = "custom"

#: Properties the read asks for (the reconcile test checks them in vi-json.yaml).
READ_PROPERTIES: Final[tuple[str, ...]] = (
    "name",
    "runtime.powerState",
    "config.cpuHotAddEnabled",
    "config.memoryHotAddEnabled",
    "config.cpuAllocation",
    "config.memoryAllocation",
)
#: request param -> ConfigSpec field, for the two hot add flags.
HOT_ADD_FIELDS: Final[dict[str, str]] = {
    "cpu_hot_add_enabled": "cpuHotAddEnabled",
    "memory_hot_add_enabled": "memoryHotAddEnabled",
}
#: envelope key -> (level param, shares param, ConfigSpec allocation field).
SHARES_FIELDS: Final[dict[str, tuple[str, str, str]]] = {
    "cpu_shares": ("cpu_shares_level", "cpu_shares", "cpuAllocation"),
    "memory_shares": ("memory_shares_level", "memory_shares", "memoryAllocation"),
}

# Wall-clock bound of the task poll; module-global so tests can lower it.
_TASK_TIMEOUT_SECONDS = 600.0


def _shares_view(allocation: Any) -> dict[str, Any] | None:
    """``{level, shares}`` of a ``ResourceAllocationInfo``, or ``None``."""
    if not isinstance(allocation, dict) or not isinstance(allocation.get("shares"), dict):
        return None
    shares = allocation["shares"]
    return {"level": shares.get("level"), "shares": _coerce_int(shares.get("shares"))}


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


async def read_vm_cpu_memory_options(
    connector: VmwareRestConnector, target: Any, operator: Operator, *, vm: str
) -> dict[str, Any] | None:
    """Read the power state, hot add flags and shares of one VM; ``None`` when not found."""
    try:
        result = await connector._post_vmomi_json(
            target,
            _VMOMI_RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=retrieve_properties_body(_VIRTUAL_MACHINE, [vm], list(READ_PROPERTIES)),
            promote_managed_object_not_found=True,
        )
    except ConnectorResourceNotFoundError:
        return None
    power_state = _extract_single_prop(result, "runtime.powerState")
    if power_state is None:
        return None
    name = _extract_single_prop(result, "name")
    return {
        "name": name if isinstance(name, str) else None,
        "power_state": power_state,
        "cpu_hot_add_enabled": _bool_or_none(
            _extract_single_prop(result, "config.cpuHotAddEnabled")
        ),
        "memory_hot_add_enabled": _bool_or_none(
            _extract_single_prop(result, "config.memoryHotAddEnabled")
        ),
        "cpu_shares": _shares_view(_extract_single_prop(result, "config.cpuAllocation")),
        "memory_shares": _shares_view(_extract_single_prop(result, "config.memoryAllocation")),
    }


def _requested(params: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """The asked settings as ``{field: value}``, or a reason the request is wrong."""
    wanted: dict[str, Any] = {
        key: params[key] for key in HOT_ADD_FIELDS if isinstance(params.get(key), bool)
    }
    for env_key, (level_key, shares_key, _) in SHARES_FIELDS.items():
        level, shares = params.get(level_key), params.get(shares_key)
        if shares is not None and level != _CUSTOM:
            return {}, f"{shares_key} needs {level_key}='custom'"
        if level == _CUSTOM and (not isinstance(shares, int) or shares < 1):
            return {}, f"{level_key}='custom' needs {shares_key} of 1 or more"
        if isinstance(level, str):
            wanted[env_key] = {"level": level, "shares": shares if level == _CUSTOM else None}
    if not wanted:
        return {}, (
            "pass at least one of cpu_hot_add_enabled, memory_hot_add_enabled, "
            "cpu_shares_level, memory_shares_level"
        )
    return wanted, None


def _matches(key: str, want: Any, current: dict[str, Any]) -> bool:
    have = current.get(key)
    if key in SHARES_FIELDS:
        if not isinstance(have, dict) or have.get("level") != want["level"]:
            return False
        return bool(want["level"] != _CUSTOM or have.get("shares") == want["shares"])
    return bool(have == want)


def _changes(wanted: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    return {key: want for key, want in wanted.items() if not _matches(key, want, current)}


def _needs_power_off(changes: dict[str, Any], current: dict[str, Any]) -> bool:
    return current.get("power_state") != _POWERED_OFF and any(k in HOT_ADD_FIELDS for k in changes)


def build_reconfig_body(changes: dict[str, Any]) -> dict[str, Any]:
    """A ``VirtualMachineConfigSpec`` with only the settings that change."""
    spec: dict[str, Any] = {VIM_TYPE_NAME_KEY: _VM_CONFIG_SPEC_TYPE}
    for key, field in HOT_ADD_FIELDS.items():
        if key in changes:
            spec[field] = changes[key]
    for env_key, (_, _, field) in SHARES_FIELDS.items():
        if env_key in changes:
            want = changes[env_key]
            spec[field] = {
                VIM_TYPE_NAME_KEY: RESOURCE_ALLOCATION_INFO_TYPE,
                "shares": {
                    VIM_TYPE_NAME_KEY: SHARES_INFO_TYPE,
                    "level": want["level"],
                    # Required by SharesInfo; vSphere ignores it unless level is custom.
                    "shares": want["shares"] if want["shares"] is not None else 0,
                },
            }
    return {"spec": spec}


def _settings(current: dict[str, Any] | None) -> dict[str, Any] | None:
    if current is None:
        return None
    return {key: current[key] for key in (*HOT_ADD_FIELDS, *SHARES_FIELDS)}


def _precheck(
    envelope: dict[str, Any], wanted: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any] | None:
    """A refusal or no-op answer before any write, or ``None`` to go on."""
    envelope.update(
        name=current["name"], power_state=current["power_state"], before=_settings(current)
    )
    changes = _changes(wanted, current)
    if not changes:
        return {
            **envelope,
            "status": "unchanged",
            "after": envelope["before"],
            "guidance": "The VM already has these settings; nothing sent.",
        }
    if _needs_power_off(changes, current):
        return {
            **envelope,
            "status": "requires_power_off",
            "guidance": "vSphere changes hot add only on a powered-off VM. Power the VM "
            "off and try again; nothing sent.",
        }
    return None


async def vm_cpu_memory_options_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Set CPU and memory hot add and shares. Op-id: ``vm.cpu_memory.options.set``."""
    vm = check_vm_moid(params["vm"])
    wanted, problem = _requested(params)
    envelope: dict[str, Any] = {
        "vm": vm,
        "name": None,
        "power_state": None,
        "requested": wanted,
        "before": None,
        "after": None,
        "task": None,
        "task_state": None,
        "guidance": None,
    }
    if problem is not None:
        return {**envelope, "status": "invalid_request", "guidance": f"{problem}; nothing sent."}
    current = await read_vm_cpu_memory_options(connector, target, operator, vm=vm)
    if current is None:
        return {
            **envelope,
            "status": "vm_not_found",
            "guidance": f"vCenter has no VM {vm}. Check the id with vmware.vm.info.",
        }
    stop = _precheck(envelope, wanted, current)
    if stop is not None:
        return stop
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_RECONFIG_VM_TASK,
        vmomi_path=f"/{_VIRTUAL_MACHINE}/{vm}/ReconfigVM_Task",
        body=build_reconfig_body(_changes(wanted, current)),
        params={"vm": vm, **wanted},
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
    envelope.update(task=outcome.task, task_state=outcome.state)
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"vm.cpu_memory.options.set: ReconfigVM_Task on vm {vm!r} failed "
            f"({outcome.fault_type or 'unknown fault'}): "
            f"{outcome.error_message or '<no fault text>'}"
        )
    if outcome.timed_out:
        return {
            **envelope,
            "status": "timeout",
            "guidance": "The task did not finish in time; it may still finish. Read the VM "
            "again with vmware.vm.info.",
            "issues": [_task_timeout_issue("ReconfigVM_Task", outcome.task)],
        }
    after = await read_vm_cpu_memory_options(connector, target, operator, vm=vm)
    envelope["after"] = _settings(after)
    if after is None or _changes(wanted, after):
        return {
            **envelope,
            "status": "partial",
            "guidance": "The task finished, but not every setting reads back as asked. "
            "Compare requested with after.",
        }
    return {**envelope, "status": "set"}


async def _vm_cpu_memory_options_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the settings now and the settings asked for."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    wanted, problem = _requested(ctx.params)
    try:
        vm = check_vm_moid(ctx.params["vm"])
        current = await read_vm_cpu_memory_options(connector, ctx.target, ctx.operator, vm=vm)
    except Exception as exc:
        return {
            "summary": "MEHO could not read the VM, so it cannot show its settings.",
            "requested": wanted,
            "read_error": preview_failure_reason(exc),
        }
    preview: dict[str, Any] = {
        "summary": f"Change CPU and memory hot add or shares of {vm}.",
        "vm": vm,
        "name": current["name"] if current else None,
        "power_state": current["power_state"] if current else None,
        "current": _settings(current),
        "requested": wanted,
    }
    if problem is not None:
        preview["problem"] = problem
    elif current is not None and _needs_power_off(_changes(wanted, current), current):
        preview["problem"] = "The VM is not powered off; the hot add change will be refused."
    return preview


register_preview_builder(OP_ID_VM_CPU_MEMORY_OPTIONS_SET, _vm_cpu_memory_options_set_preview)

#: The registration row, as ``_CompositeSpec`` keyword arguments.
VM_CPU_MEMORY_OPTIONS_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": OP_ID_VM_CPU_MEMORY_OPTIONS_SET,
        "handler": vm_cpu_memory_options_set_composite,
        "summary": "Turn CPU and memory hot add on or off, and set CPU and memory shares.",
        "description": (
            "Changes a VM's CPU and memory hot add and its CPU and memory shares in one "
            "vCenter task. Only the settings you pass change. Hot add changes need the VM "
            "powered off. Waits for approval."
        ),
        "parameter_schema": VM_CPU_MEMORY_OPTIONS_SET_PARAMETER_SCHEMA,
        "response_schema": VM_CPU_MEMORY_OPTIONS_SET_RESPONSE_SCHEMA,
        "group_key": "vm",
        "tags": ["composite", "write", "vm", "hardware", "vi-json"],
        "safety_level": "caution",
        "requires_approval": True,
        "llm_instructions": VM_CPU_MEMORY_OPTIONS_SET_LLM_INSTRUCTIONS,
    },
)

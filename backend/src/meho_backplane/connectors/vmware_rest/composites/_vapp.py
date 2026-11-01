# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared reads, views and write steps of the vApp operations.

A vApp is a resource pool that groups VMs and starts and stops them in a set
order. vCenter's REST API has no call for it, so the four operations use the
VIM API as VI-JSON, like the other changes in this package:

* ``vmware.composite.vapp.create`` -- ``ResourcePool.CreateVApp``;
* ``vmware.composite.vapp.power`` -- ``VirtualApp.PowerOnVApp_Task`` /
  ``PowerOffVApp_Task``;
* ``vmware.composite.vapp.start_order.set`` -- ``VirtualApp.UpdateVAppConfig``;
* ``vmware.composite.vapp.delete`` -- ``VirtualApp.Destroy_Task``.

Each write goes through the governed write seam
(:func:`._write._write_vmomi_sub_op`); a task is polled with
:func:`~meho_backplane.connectors.vmware_rest.vim_task.poll_vim_task`; after
the write MEHO reads the vApp again and returns its new state. A vendor fault
raises with its type and text, so the dispatcher records the call as failed.
The handlers are in :mod:`._vapp_change` and :mod:`._vapp_delete`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vmware_rest.composites._teardown import STATUS_TIMEOUT
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moref_values,
    read_many,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_DESTROY_VAPP_TASK,
    _OP_RETRIEVE_PROPERTIES,
    _VIRTUAL_MACHINE_MO_TYPE,
    _moref_value,
    _task_timeout_issue,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.vim_body import fault_message
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.vim_task import VimTaskResult

__all__ = [
    "ENTITY_FIELDS",
    "OP_CREATE_VAPP",
    "OP_POWER_OFF_VAPP_TASK",
    "OP_POWER_ON_VAPP_TASK",
    "OP_UPDATE_VAPP_CONFIG",
    "STATUS_NOT_CONFIRMED",
    "STATUS_NOT_FOUND",
    "VAPP_MO_TYPE",
    "child_pools",
    "entity_settings",
    "read_vapp",
    "run_task",
    "sync_write",
    "timeout_fields",
    "vapp_state",
    "vapp_view",
    "view_of",
    "vm_moids",
    "vm_rows",
]

VAPP_MO_TYPE: Final = "VirtualApp"

OP_CREATE_VAPP: Final = "POST:/ResourcePool/{moId}/CreateVApp"
OP_POWER_ON_VAPP_TASK: Final = "POST:/VirtualApp/{moId}/PowerOnVApp_Task"
OP_POWER_OFF_VAPP_TASK: Final = "POST:/VirtualApp/{moId}/PowerOffVApp_Task"
OP_UPDATE_VAPP_CONFIG: Final = "POST:/VirtualApp/{moId}/UpdateVAppConfig"

#: vi-json sub-op manifests (``_VIM_SUB_OPS_*``, so the vcenter.yaml sweep skips
#: them; the reconcile lane checks each path against the pinned vi-json.yaml).
#: Every one reads first (``RetrievePropertiesEx``, also the task poll); the
#: name reads follow continuation pages.
_VIM_SUB_OPS_VAPP_CREATE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    OP_CREATE_VAPP,
)
_VIM_SUB_OPS_VAPP_POWER: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    OP_POWER_ON_VAPP_TASK,
    OP_POWER_OFF_VAPP_TASK,
)
_VIM_SUB_OPS_VAPP_START_ORDER: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    OP_UPDATE_VAPP_CONFIG,
)
_VIM_SUB_OPS_VAPP_DELETE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_DESTROY_VAPP_TASK,
)

_PROP_ENTITY_CONFIG: Final = "vAppConfig.entityConfig"
#: ``summary`` is read whole: its declared type is ``ResourcePoolSummary``, and
#: ``vAppState`` lives on the ``VirtualAppSummary`` subtype only.
_VAPP_PROPS: Final = ["name", "summary", "vm", "resourcePool", _PROP_ENTITY_CONFIG]
_VM_PROPS: Final = ["name", "runtime.powerState"]

#: Wall-clock bound for the vApp task polls -- the 600 s convention; a vApp
#: power-on waits for every start delay. Module-global so tests can zero it.
_TASK_TIMEOUT_SECONDS = 600.0

STATUS_NOT_FOUND: Final = "not_found"
STATUS_NOT_CONFIRMED: Final = "not_confirmed"

#: ``VAppEntityConfigInfo`` field -> the name MEHO shows it under.
ENTITY_FIELDS: Final[dict[str, str]] = {
    "startOrder": "start_order",
    "startDelay": "start_delay",
    "waitingForGuest": "wait_for_guest",
    "startAction": "start_action",
    "stopAction": "stop_action",
    "stopDelay": "stop_delay",
}


async def read_vapp(
    connector: VmwareRestConnector, target: Any, operator: Operator, vapp: str
) -> dict[str, Any] | None:
    """The vApp's properties, or ``None`` when it does not exist.

    :func:`read_object` fails the call when vCenter could not read a property,
    so a missing permission never reads as "no VMs".
    """
    return await read_object(
        connector, target, operator, mo_type=VAPP_MO_TYPE, moid=vapp, props=_VAPP_PROPS
    )


def vapp_state(props: dict[str, Any]) -> str | None:
    summary = props.get("summary")
    state = summary.get("vAppState") if isinstance(summary, dict) else None
    return state if isinstance(state, str) else None


def vm_moids(props: dict[str, Any]) -> list[str]:
    return [moid for _mo_type, moid in moref_values(props.get("vm"))]


def child_pools(props: dict[str, Any]) -> list[tuple[str, str]]:
    """``[(type, moid)]`` of the resource pools and vApps inside the vApp."""
    return moref_values(props.get("resourcePool"))


def entity_settings(props: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{vm id: {start_order, ...}}`` from the vApp's ``entityConfig``."""
    entries = props.get(_PROP_ENTITY_CONFIG)
    out: dict[str, dict[str, Any]] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        moid = _moref_value(entry.get("key"))
        if moid is None:
            continue
        out[moid] = {name: entry.get(field) for field, name in ENTITY_FIELDS.items()}
    return out


async def vm_rows(
    connector: VmwareRestConnector, target: Any, operator: Operator, props: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    """``(rows, complete)``: one row per VM in the vApp, in start order.

    ``complete`` is ``False`` when vCenter did not fully answer the VM read.
    """
    moids = vm_moids(props)
    found, complete = await read_many(
        connector, target, operator, mo_type=_VIRTUAL_MACHINE_MO_TYPE, moids=moids, props=_VM_PROPS
    )
    settings = entity_settings(props)
    rows: list[dict[str, Any]] = []
    for moid in moids:
        vm = found.get(moid, {})
        name = vm.get("name")
        power = vm.get("runtime.powerState")
        row: dict[str, Any] = {
            "vm": moid,
            "name": name if isinstance(name, str) else None,
            "power_state": power if isinstance(power, str) else None,
        }
        row.update(settings.get(moid, dict.fromkeys(ENTITY_FIELDS.values())))
        rows.append(row)
    rows.sort(key=lambda r: (r["start_order"] is None, r["start_order"] or 0, r["vm"]))
    return rows, complete


async def vapp_view(
    connector: VmwareRestConnector, target: Any, operator: Operator, vapp: str
) -> dict[str, Any] | None:
    """``{vapp, name, state, vms}`` read now, or ``None`` when the vApp is gone."""
    props = await read_vapp(connector, target, operator, vapp)
    if props is None:
        return None
    return await view_of(connector, target, operator, vapp, props)


async def view_of(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vapp: str,
    props: dict[str, Any],
) -> dict[str, Any]:
    """``{vapp, name, state, vms}`` of the vApp read as *props*."""
    rows, _complete = await vm_rows(connector, target, operator, props)
    name = props.get("name")
    return {
        "vapp": vapp,
        "name": name if isinstance(name, str) else None,
        "state": vapp_state(props),
        "vms": rows,
    }


async def sync_write(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    vmomi_path: str,
    body: dict[str, Any],
    params: dict[str, Any],
) -> tuple[OperationResult | None, Any]:
    """Gate + issue one synchronous vim write (no task).

    A parked or denied gate comes back as ``(gate, None)`` with nothing sent.
    A vendor fault (HTTP 500 with the fault as body) raises ``RuntimeError``
    with the fault type and text.
    """
    try:
        return await _write_vmomi_sub_op(
            connector,
            target,
            operator,
            op_id=op_id,
            vmomi_path=vmomi_path,
            body=body,
            params=params,
        )
    except httpx.HTTPStatusError as exc:
        method = vmomi_path.rsplit("/", 1)[-1]
        raise RuntimeError(f"{params['op']}: {method} faulted: {_fault_text(exc)}") from exc


def _fault_text(exc: httpx.HTTPStatusError) -> str:
    try:
        body = exc.response.json()
    except ValueError:
        body = None
    text = fault_message(body) if isinstance(body, dict) else None
    return text or f"HTTP {exc.response.status_code}"


async def run_task(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    vmomi_path: str,
    body: dict[str, Any],
    params: dict[str, Any],
) -> tuple[OperationResult | None, VimTaskResult | None]:
    """Gate + issue one vim ``*_Task`` method and poll it.

    A parked or denied gate comes back as ``(gate, None)`` with nothing sent.
    A task fault raises (``connector_error``, audited as failed) with the
    fault type and text.
    """
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=op_id,
        vmomi_path=vmomi_path,
        body=body,
        params=params,
    )
    if gate is not None:
        return gate, None
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        method = vmomi_path.rsplit("/", 1)[-1]
        raise RuntimeError(
            f"{params['op']}: {method} on vApp {params.get('vapp')!r} faulted "
            f"({outcome.fault_type or 'unknown fault'}): "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    return None, outcome


def timeout_fields(outcome: VimTaskResult, method: str, reread: str) -> dict[str, Any]:
    return {
        "status": STATUS_TIMEOUT,
        "task": outcome.task,
        "task_state": outcome.state,
        "guidance": (
            f"{method} {outcome.task} did not finish within {int(_TASK_TIMEOUT_SECONDS)}s; "
            f"it may still finish. {reread}"
        ),
        "issues": [_task_timeout_issue(method, outcome.task)],
    }

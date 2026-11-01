# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``host.maintenance.enter`` -- start maintenance mode on one host (a vendor job).

Reads the host and its recent tasks, then sends ``EnterMaintenanceMode_Task``
with the vSAN data option through the governed VI-JSON seam. It answers
``accepted`` with the vCenter task id as soon as vCenter took the task; the
caller follows the task with ``vmware.task.info``. A maintenance-mode start
that still runs on the host is returned (``already_running``) and nothing new
is started. The task read follows every page of vCenter's answer; when it
still cannot give the state of every recent task, the start is refused
(``tasks_incomplete``) rather than sent on a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._host_power_common import (
    CONNECTED,
    TASK_INFO_OP_ID,
    HostState,
    host_id_problem,
    host_state,
    make_answer,
    not_connected,
    not_found,
    read_host,
    send_task,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moref_values,
    read_many,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_ENTER_MAINTENANCE_TASK,
    _OP_RETRIEVE_PROPERTIES,
    _VSAN_MODE_DEFAULT,
    _vsan_maintenance_spec,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = ["host_maintenance_enter_composite"]

#: vi-json sub-op manifest: the host and task reads (every page), and the one write.
_VIM_SUB_OPS_HOST_MAINTENANCE_ENTER: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_CONTINUE_RETRIEVE_PROPERTIES,
    _OP_ENTER_MAINTENANCE_TASK,
)

_PROP_RECENT_TASK: Final = "recentTask"
_TASK_MO_TYPE: Final = "Task"
#: ``TaskInfo.descriptionId`` of a maintenance-mode start (``<Type>.<method>``).
_ENTER_MAINTENANCE_DESCRIPTION_ID: Final = "HostSystem.enterMaintenanceMode"
_RUNNING_TASK_STATES: Final = frozenset({"queued", "running"})


@dataclass(frozen=True)
class _TaskCheck:
    """What the read of the host's recent tasks found."""

    #: The id of a maintenance-mode start that is still queued or running.
    running: str | None
    #: ``False`` when vCenter left a recent task, or its ``info``, out of the answer.
    complete: bool


async def _running_enter_task(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    recent: Any,
) -> _TaskCheck:
    """Look for a maintenance-mode start that still runs on the host.

    Reads every page of the task answer. A help, not a lock: two callers can
    both look before either starts, and a task that just ended is no longer
    running.
    """
    task_ids = [moid for kind, moid in moref_values(recent) if kind in ("", _TASK_MO_TYPE)]
    if not task_ids:
        return _TaskCheck(running=None, complete=True)
    infos, complete = await read_many(
        connector,
        target,
        operator,
        mo_type=_TASK_MO_TYPE,
        moids=task_ids,
        props=["info"],
        all_pages=True,
    )
    for task_id in task_ids:
        info = infos.get(task_id, {}).get("info")
        if not isinstance(info, dict):
            continue
        if (
            info.get("descriptionId") == _ENTER_MAINTENANCE_DESCRIPTION_ID
            and info.get("state") in _RUNNING_TASK_STATES
        ):
            return _TaskCheck(running=task_id, complete=complete)
    return _TaskCheck(running=None, complete=complete)


def _status_read(task: str) -> dict[str, Any]:
    return {"op_id": TASK_INFO_OP_ID, "params": {"task": task}}


def _enter_answer(
    status: str,
    host: str,
    state: HostState,
    vsan_mode: str,
    guidance: str,
    task: str | None = None,
) -> dict[str, Any]:
    answer = make_answer(status, host, state, guidance, task=task)
    answer["vsan_mode"] = vsan_mode
    answer["status_read"] = _status_read(task) if task is not None else None
    return answer


async def host_maintenance_enter_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Start maintenance mode on one host, with a vSAN data option.

    Op-id: ``vmware.composite.host.maintenance.enter``. Steps:

    1. Read the host and its recent tasks (every page). A host that does not
       exist, is not connected, or is already in maintenance mode gets an
       answer and nothing is sent. A maintenance-mode start that still runs
       on the host is returned as ``already_running``. When vCenter did not
       give the state of every recent task, the answer is
       ``tasks_incomplete`` and nothing is sent.
    2. Send ``EnterMaintenanceMode_Task`` with ``timeout`` and the
       ``HostMaintenanceSpec`` of the vSAN data option, through the governed
       vim seam. Answer ``accepted`` with the task id and the status read; no
       waiting.
    """
    host = params.get("host")
    vsan_mode = str(params.get("vsan_mode") or _VSAN_MODE_DEFAULT)
    problem = host_id_problem(host)
    if problem is not None:
        return _enter_answer("invalid_request", str(host), HostState(), vsan_mode, problem)
    assert isinstance(host, str)
    props = await read_host(connector, target, operator, host, extra=(_PROP_RECENT_TASK,))
    if props is None:
        return {**not_found(host), "vsan_mode": vsan_mode, "status_read": None}
    state = host_state(props)
    if state.connection_state != CONNECTED:
        return {**not_connected(host, state), "vsan_mode": vsan_mode, "status_read": None}
    if state.in_maintenance_mode is True:
        guidance = f"host {host!r} is already in maintenance mode. Nothing was sent."
        return _enter_answer("unchanged", host, state, vsan_mode, guidance)
    check = await _running_enter_task(connector, target, operator, props.get(_PROP_RECENT_TASK))
    if check.running is not None:
        guidance = (
            f"a maintenance-mode task ({check.running}) already runs on host {host!r}. Nothing "
            "new was started. Follow it with vmware.task.info."
        )
        return _enter_answer("already_running", host, state, vsan_mode, guidance, check.running)
    if not check.complete:
        guidance = (
            f"vCenter did not give the state of every recent task on host {host!r}, so MEHO "
            "cannot tell whether a maintenance-mode start already runs. Nothing was sent. "
            "Read vmware.tasks.recent for the host, then call again."
        )
        return _enter_answer("tasks_incomplete", host, state, vsan_mode, guidance)

    body = {
        "timeout": int(params.get("timeout_seconds") or 0),
        "maintenanceSpec": _vsan_maintenance_spec(vsan_mode),
    }
    gate, task = await send_task(
        connector, target, operator, host=host, op_id=_OP_ENTER_MAINTENANCE_TASK, body=body
    )
    if gate is not None:
        return gate
    assert task is not None
    guidance = (
        f"vCenter accepted the maintenance-mode task {task}; the host is not in maintenance "
        "mode yet. Read vmware.task.info with this task id until it ends, then read the host. "
        "Do not start again."
    )
    return _enter_answer("accepted", host, state, vsan_mode, guidance, task)

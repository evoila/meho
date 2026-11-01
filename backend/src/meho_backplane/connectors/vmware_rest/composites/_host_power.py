# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host reboot, shut down and standby -- four changes that wait for their task.

* ``host.reboot`` / ``host.shutdown`` -- ``RebootHost_Task`` /
  ``ShutdownHost_Task``. Refused for a host that is not in maintenance mode,
  unless ``force``.
* ``host.standby.enter`` / ``host.standby.exit`` --
  ``PowerDownHostToStandBy_Task`` / ``PowerUpHostFromStandBy_Task``.

Each reads the host, sends one governed ``HostSystem`` task method, waits
for the task, then reads the host again (best effort: a host that restarts
may not answer). A faulted task fails the call with the fault type and text.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._host_power_common import (
    CONNECTED,
    HostState,
    host_id_problem,
    host_state,
    make_answer,
    not_connected,
    not_found,
    read_host,
    refusal,
    send_task,
)
from meho_backplane.connectors.vmware_rest.composites._host_power_schemas import (
    OP_REBOOT,
    OP_SHUTDOWN,
    OP_STANDBY_ENTER,
    OP_STANDBY_EXIT,
    STANDBY_TIMEOUT_DEFAULT,
)
from meho_backplane.connectors.vmware_rest.composites._write import _OP_RETRIEVE_PROPERTIES
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.vim_task import VimTaskResult

__all__ = [
    "host_reboot_composite",
    "host_shutdown_composite",
    "host_standby_enter_composite",
    "host_standby_exit_composite",
]

_OP_REBOOT_TASK: Final = "POST:/HostSystem/{moId}/RebootHost_Task"
_OP_SHUTDOWN_TASK: Final = "POST:/HostSystem/{moId}/ShutdownHost_Task"
_OP_STANDBY_ENTER_TASK: Final = "POST:/HostSystem/{moId}/PowerDownHostToStandBy_Task"
_OP_STANDBY_EXIT_TASK: Final = "POST:/HostSystem/{moId}/PowerUpHostFromStandBy_Task"

#: vi-json sub-op manifests (``_VIM_SUB_OPS_*``): the host read and the one write.
_VIM_SUB_OPS_HOST_REBOOT: Final[tuple[str, ...]] = (_OP_RETRIEVE_PROPERTIES, _OP_REBOOT_TASK)
_VIM_SUB_OPS_HOST_SHUTDOWN: Final[tuple[str, ...]] = (_OP_RETRIEVE_PROPERTIES, _OP_SHUTDOWN_TASK)
_VIM_SUB_OPS_HOST_STANDBY_ENTER: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_STANDBY_ENTER_TASK,
)
_VIM_SUB_OPS_HOST_STANDBY_EXIT: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_STANDBY_EXIT_TASK,
)

_POWERED_ON: Final = "poweredOn"
_STANDBY: Final = "standBy"

#: Wall-clock bound of the reboot and shutdown task polls (the 600 s convention);
#: module-global so tests can zero it.
_TASK_TIMEOUT_SECONDS = 600.0
#: Extra seconds MEHO waits on top of the vendor's standby ``timeoutSec``.
_STANDBY_POLL_MARGIN_SECONDS = 60.0


@dataclass(frozen=True)
class _PowerChange:
    """What one waiting change sends and how it reads the host before."""

    label: str
    op_id: str
    done_guidance: str
    body: Callable[[dict[str, Any]], dict[str, Any]]
    precheck: Callable[[str, HostState, dict[str, Any]], dict[str, Any] | None]
    poll_seconds: Callable[[dict[str, Any]], float]


def _fixed_poll(_params: dict[str, Any]) -> float:
    return _TASK_TIMEOUT_SECONDS


def _standby_poll(params: dict[str, Any]) -> float:
    return float(_standby_timeout(params)) + _STANDBY_POLL_MARGIN_SECONDS


def _standby_timeout(params: dict[str, Any]) -> int:
    value = params.get("timeout_seconds")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return STANDBY_TIMEOUT_DEFAULT


def _needs_maintenance(
    host: str, state: HostState, params: dict[str, Any]
) -> dict[str, Any] | None:
    if state.connection_state != CONNECTED:
        return not_connected(host, state)
    if state.in_maintenance_mode is not True and not params.get("force", False):
        return make_answer(
            "not_in_maintenance",
            host,
            state,
            f"host {host!r} is not in maintenance mode. Nothing was sent. Put it in "
            "maintenance mode first (host.evacuate or host.maintenance.enter), or pass "
            "force: true to change it with VMs running.",
        )
    return None


def _standby_enter_check(
    host: str, state: HostState, params: dict[str, Any]
) -> dict[str, Any] | None:
    del params
    if state.power_state == _STANDBY:
        return make_answer("unchanged", host, state, f"host {host!r} is already in standby.")
    if state.connection_state != CONNECTED:
        return not_connected(host, state)
    return None


def _standby_exit_check(
    host: str, state: HostState, params: dict[str, Any]
) -> dict[str, Any] | None:
    del params
    if state.power_state == _POWERED_ON:
        guidance = f"host {host!r} is powered on, not in standby. Nothing was sent."
        return make_answer("unchanged", host, state, guidance)
    return None


_CHANGES: Final[dict[str, _PowerChange]] = {
    OP_REBOOT: _PowerChange(
        label="host.reboot",
        op_id=_OP_REBOOT_TASK,
        done_guidance=(
            "vCenter sent the reboot. The host restarts and is notResponding for some "
            "minutes; read it with vmware.host.usage until it is connected."
        ),
        body=lambda params: {"force": bool(params.get("force", False))},
        precheck=_needs_maintenance,
        poll_seconds=_fixed_poll,
    ),
    OP_SHUTDOWN: _PowerChange(
        label="host.shutdown",
        op_id=_OP_SHUTDOWN_TASK,
        done_guidance=(
            "vCenter sent the shutdown. The host powers off; MEHO cannot power it on again."
        ),
        body=lambda params: {"force": bool(params.get("force", False))},
        precheck=_needs_maintenance,
        poll_seconds=_fixed_poll,
    ),
    OP_STANDBY_ENTER: _PowerChange(
        label="host.standby.enter",
        op_id=_OP_STANDBY_ENTER_TASK,
        done_guidance="The host is in standby. Wake it with host.standby.exit.",
        body=lambda params: {
            "timeoutSec": _standby_timeout(params),
            "evacuatePoweredOffVms": bool(params.get("evacuate_powered_off_vms", False)),
        },
        precheck=_standby_enter_check,
        poll_seconds=_standby_poll,
    ),
    OP_STANDBY_EXIT: _PowerChange(
        label="host.standby.exit",
        op_id=_OP_STANDBY_EXIT_TASK,
        done_guidance="The host left standby and can run VMs again.",
        body=lambda params: {"timeoutSec": _standby_timeout(params)},
        precheck=_standby_exit_check,
        poll_seconds=_standby_poll,
    ),
}


def _task_failure(change: _PowerChange, host: str, outcome: VimTaskResult) -> RuntimeError:
    method = change.op_id.rsplit("/", 1)[-1]
    fault = f" ({outcome.fault_type})" if outcome.fault_type else ""
    return RuntimeError(
        f"{change.label}: {method} task {outcome.task} on host {host!r} faulted{fault}: "
        f"{outcome.error_message or '<no fault reported>'}"
    )


async def _read_after(
    connector: VmwareRestConnector, target: Any, operator: Operator, host: str
) -> tuple[HostState, list[dict[str, Any]]]:
    """Best-effort read of the host after the task: a host that restarts may not answer."""
    try:
        props = await read_host(connector, target, operator, host)
    except Exception as exc:
        message = (
            f"the host read after the task failed ({type(exc).__name__}); the current state "
            f"of host {host!r} is unavailable"
        )
        return HostState(), [issue("connector", "warning", message)]
    if props is None:
        return HostState(), [issue("connector", "warning", f"host {host!r} is no longer found")]
    return host_state(props), []


async def _run_power_change(
    change: _PowerChange,
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    host = params.get("host")
    problem = host_id_problem(host)
    if problem is not None:
        return refusal("invalid_request", str(host), problem)
    assert isinstance(host, str)
    props = await read_host(connector, target, operator, host)
    if props is None:
        return not_found(host)
    early = change.precheck(host, host_state(props), params)
    if early is not None:
        return early

    gate, task = await send_task(
        connector, target, operator, host=host, op_id=change.op_id, body=change.body(params)
    )
    if gate is not None:
        return gate
    poll_seconds = change.poll_seconds(params)
    outcome = await poll_vim_task(
        connector, target, operator, task=task, timeout_seconds=poll_seconds
    )
    if outcome.state == TASK_STATE_ERROR:
        raise _task_failure(change, host, outcome)
    state, issues = await _read_after(connector, target, operator, host)
    if outcome.timed_out:
        guidance = (
            f"MEHO stopped waiting after {int(poll_seconds)}s, but task {outcome.task} still "
            "runs on vCenter. The call did not fail. Follow it with vmware.task.info."
        )
        issues.insert(
            0,
            issue(
                "connector",
                "warning",
                f"task {outcome.task} on host {host!r} still runs after {int(poll_seconds)}s",
            ),
        )
        answer = make_answer("timeout", host, state, guidance, task=outcome.task)
    else:
        answer = make_answer("done", host, state, change.done_guidance, task=outcome.task)
    if issues:
        answer["issues"] = issues
    return answer


async def host_reboot_composite(
    *, operator: Operator, target: Any, params: dict[str, Any], connector: VmwareRestConnector
) -> dict[str, Any] | OperationResult:
    """Reboot one host (``RebootHost_Task``); in maintenance mode unless ``force``."""
    return await _run_power_change(
        _CHANGES[OP_REBOOT], operator=operator, target=target, params=params, connector=connector
    )


async def host_shutdown_composite(
    *, operator: Operator, target: Any, params: dict[str, Any], connector: VmwareRestConnector
) -> dict[str, Any] | OperationResult:
    """Shut down one host (``ShutdownHost_Task``); in maintenance mode unless ``force``."""
    return await _run_power_change(
        _CHANGES[OP_SHUTDOWN], operator=operator, target=target, params=params, connector=connector
    )


async def host_standby_enter_composite(
    *, operator: Operator, target: Any, params: dict[str, Any], connector: VmwareRestConnector
) -> dict[str, Any] | OperationResult:
    """Put one host in standby (``PowerDownHostToStandBy_Task``)."""
    return await _run_power_change(
        _CHANGES[OP_STANDBY_ENTER],
        operator=operator,
        target=target,
        params=params,
        connector=connector,
    )


async def host_standby_exit_composite(
    *, operator: Operator, target: Any, params: dict[str, Any], connector: VmwareRestConnector
) -> dict[str, Any] | OperationResult:
    """Wake one host from standby (``PowerUpHostFromStandBy_Task``)."""
    return await _run_power_change(
        _CHANGES[OP_STANDBY_EXIT],
        operator=operator,
        target=target,
        params=params,
        connector=connector,
    )

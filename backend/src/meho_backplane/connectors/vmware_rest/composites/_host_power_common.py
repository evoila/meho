# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared parts of the host power operations: the host read, the answer, the write.

Used by :mod:`._host_maintenance_enter` and :mod:`._host_power`. Every write
goes through the governed VI-JSON seam :func:`._write._write_vmomi_sub_op`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import read_object
from meho_backplane.connectors.vmware_rest.composites._write import (
    _HOST_SYSTEM_MO_TYPE,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.vim_task import task_moref_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "CONNECTED",
    "HOST_STATE_PROPS",
    "TASK_INFO_OP_ID",
    "HostState",
    "host_id_problem",
    "host_state",
    "make_answer",
    "not_connected",
    "not_found",
    "read_host",
    "refusal",
    "send_task",
]

#: The ``HostSystem`` properties every handler reads before (and after) the change.
HOST_STATE_PROPS: Final[tuple[str, ...]] = (
    "name",
    "runtime.connectionState",
    "runtime.powerState",
    "runtime.inMaintenanceMode",
    "runtime.standbyMode",
    "runtime.dasHostState",
)
#: ``HostSystemConnectionState`` value of a host vCenter can send a call to.
CONNECTED: Final = "connected"
#: The read that follows a vCenter task.
TASK_INFO_OP_ID: Final = "vmware.task.info"

#: A host moid goes into the URL path of the write; only these characters may.
_HOST_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class HostState:
    """The host fields every answer carries."""

    name: str | None = None
    connection_state: str | None = None
    power_state: str | None = None
    in_maintenance_mode: bool | None = None
    standby_mode: str | None = None
    ha_state: str | None = None

    def as_fields(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connection_state": self.connection_state,
            "power_state": self.power_state,
            "in_maintenance_mode": self.in_maintenance_mode,
            "standby_mode": self.standby_mode,
            "ha_state": self.ha_state,
        }


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def host_state(props: dict[str, Any]) -> HostState:
    das = props.get("runtime.dasHostState")
    maintenance = props.get("runtime.inMaintenanceMode")
    return HostState(
        name=_str_or_none(props.get("name")),
        connection_state=_str_or_none(props.get("runtime.connectionState")),
        power_state=_str_or_none(props.get("runtime.powerState")),
        in_maintenance_mode=maintenance if isinstance(maintenance, bool) else None,
        standby_mode=_str_or_none(props.get("runtime.standbyMode")),
        ha_state=_str_or_none(das.get("state")) if isinstance(das, dict) else None,
    )


async def read_host(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    host: str,
    *,
    extra: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """The host's state properties (plus *extra*); ``None`` when the host does not exist."""
    return await read_object(
        connector,
        target,
        operator,
        mo_type=_HOST_SYSTEM_MO_TYPE,
        moid=host,
        props=[*HOST_STATE_PROPS, *extra],
    )


def make_answer(
    status: str,
    host: str,
    state: HostState,
    guidance: str,
    *,
    task: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "host": host,
        **state.as_fields(),
        "task": task,
        "guidance": guidance,
    }


def refusal(status: str, host: str, guidance: str) -> dict[str, Any]:
    return make_answer(status, host, HostState(), guidance)


def host_id_problem(host: Any) -> str | None:
    if isinstance(host, str) and _HOST_ID_RE.fullmatch(host):
        return None
    return f"host must be a host moid such as host-21; got {host!r}. Nothing was sent."


def not_found(host: str) -> dict[str, Any]:
    return refusal(
        "not_found",
        host,
        f"vCenter has no host {host!r}. Nothing was sent. vmware.host.usage lists the hosts.",
    )


def not_connected(host: str, state: HostState) -> dict[str, Any]:
    return make_answer(
        "not_connected",
        host,
        state,
        f"host {host!r} is {state.connection_state or 'in an unknown state'}, not connected "
        "to vCenter. Nothing was sent. Reconnect the host first, then call again.",
    )


async def send_task(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    host: str,
    op_id: str,
    body: dict[str, Any],
) -> tuple[OperationResult | None, str | None]:
    """Gate and send one ``HostSystem`` task method: ``(gate, task id)``."""
    method = op_id.rsplit("/", 1)[-1]
    gate, payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=op_id,
        vmomi_path=f"/{_HOST_SYSTEM_MO_TYPE}/{host}/{method}",
        body=body,
        params={"host": host},
    )
    if gate is not None:
        return gate, None
    task = task_moref_value(_unwrap_value(payload))
    if task is None:
        raise RuntimeError(f"{method} on host {host!r} returned no task reference")
    return None, task

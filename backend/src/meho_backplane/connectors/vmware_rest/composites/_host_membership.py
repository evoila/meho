# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host membership changes: move, connect and disconnect a host (#4349).

vCenter has no REST path for these, so each one sends one VIM method as
VI-JSON through the governed write seam (``_write_vmomi_sub_op``):

* ``host.move`` -- ``ClusterComputeResource.MoveHostInto_Task``; the host must
  be in maintenance mode.
* ``host.connect`` -- ``HostSystem.ReconnectHost_Task`` with the login vCenter
  already has.
* ``host.disconnect`` -- ``HostSystem.DisconnectHost_Task``; the VMs keep
  running.

Each one reads the host first (nothing is sent when the host already is where
the call wants it), waits for the task, and reads the host again. A task fault
fails the call with the fault type and text. The add is in :mod:`._host_add`
and the remove in :mod:`._host_remove`. All five need a vCenter target: a
single ESXi host cannot add, move or remove itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moref_values,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _CLUSTER_COMPUTE_RESOURCE_MO_TYPE,
    _HOST_SYSTEM_MO_TYPE,
    _task_timeout_issue,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.host_target import (
    HOST_FLAVOR_ESXI,
    classify_host_target,
)
from meho_backplane.connectors.vmware_rest.typed_ops_task_info import TASK_INFO_OP_ID
from meho_backplane.connectors.vmware_rest.vim_body import vim_moref
from meho_backplane.connectors.vmware_rest.vim_task import (
    TASK_STATE_ERROR,
    VimTaskResult,
    poll_vim_task,
)
from meho_backplane.operations._preview import preview_failure_reason, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "OP_ID_HOST_CONNECT",
    "OP_ID_HOST_DISCONNECT",
    "OP_ID_HOST_MOVE",
    "HostState",
    "host_connect_composite",
    "host_disconnect_composite",
    "host_move_composite",
    "needs_vcenter",
    "read_host_state",
]

OP_ID_HOST_MOVE: Final = "vmware.composite.host.move"
OP_ID_HOST_CONNECT: Final = "vmware.composite.host.connect"
OP_ID_HOST_DISCONNECT: Final = "vmware.composite.host.disconnect"

# The governed vendor calls (``METHOD:/path`` of the pinned vi-json.yaml).
_OP_MOVE_HOST_INTO: Final = "POST:/ClusterComputeResource/{moId}/MoveHostInto_Task"
_OP_RECONNECT_HOST: Final = "POST:/HostSystem/{moId}/ReconnectHost_Task"
_OP_DISCONNECT_HOST: Final = "POST:/HostSystem/{moId}/DisconnectHost_Task"

_CONNECTED: Final = "connected"
_DISCONNECTED: Final = "disconnected"

_HOST_PROPS: Final = [
    "name",
    "parent",
    "runtime.connectionState",
    "runtime.inMaintenanceMode",
    "vm",
]

#: Wall-clock bound of the task polls (the 600 s convention); tests zero it.
_TASK_TIMEOUT_SECONDS = 600.0


# ---------------------------------------------------------------------------
# Shared reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostState:
    """One read of a host: the fields every membership answer shows."""

    host: str
    name: str | None
    connection_state: str | None
    in_maintenance_mode: bool | None
    parent: str | None
    parent_type: str | None
    vms: tuple[str, ...]

    def as_fields(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "name": self.name,
            "connection_state": self.connection_state,
            "in_maintenance_mode": self.in_maintenance_mode,
            "parent": self.parent,
            "parent_type": self.parent_type,
            "vm_count": len(self.vms),
        }


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


async def read_host_state(
    connector: VmwareRestConnector, target: Any, operator: Operator, host: str
) -> HostState | None:
    """Read the host's name, parent, connection and maintenance state and VMs.

    ``None`` when vCenter has no host with this id. A property vCenter could
    not read fails the call (:func:`read_object`).
    """
    props = await read_object(
        connector, target, operator, mo_type=_HOST_SYSTEM_MO_TYPE, moid=host, props=_HOST_PROPS
    )
    if props is None:
        return None
    parent = props.get("parent")
    in_mm = props.get("runtime.inMaintenanceMode")
    return HostState(
        host=host,
        name=_text(props.get("name")),
        connection_state=_text(props.get("runtime.connectionState")),
        in_maintenance_mode=in_mm if isinstance(in_mm, bool) else None,
        parent=_text(parent.get("value")) if isinstance(parent, dict) else None,
        parent_type=_text(parent.get("type")) if isinstance(parent, dict) else None,
        vms=tuple(moid for _type, moid in moref_values(props.get("vm"))),
    )


def needs_vcenter(target: Any) -> str | None:
    """Why the target cannot run a host membership change, or ``None``."""
    flavor, refusal = classify_host_target(target)
    if refusal is not None:
        return str(refusal.get("guidance") or "the target is not a vCenter")
    if flavor == HOST_FLAVOR_ESXI:
        return (
            "the target is a single ESXi host. Host membership changes run on the vCenter "
            "that manages the host; call again with the vCenter target. Nothing was sent."
        )
    return None


async def _run_task(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    vmomi_path: str,
    body: dict[str, Any],
    params: dict[str, Any],
    label: str,
) -> OperationResult | VimTaskResult:
    """Gate, send and poll one VIM ``*_Task``. A task fault raises with its type and text."""
    gate, payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=op_id,
        vmomi_path=vmomi_path,
        body=body,
        params=params,
    )
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(payload),
        timeout_seconds=_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        method = vmomi_path.rsplit("/", 1)[-1]
        fault = outcome.fault_type or "fault"
        raise RuntimeError(
            f"{label}: {method} faulted ({fault}): "
            f"{outcome.error_message or '<no fault text reported>'}"
        )
    return outcome


def _answer(
    status: str,
    state: HostState | None,
    host: str,
    *,
    task: str | None = None,
    guidance: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    fields = (
        state.as_fields()
        if state is not None
        else {
            "host": host,
            "name": None,
            "connection_state": None,
            "in_maintenance_mode": None,
            "parent": None,
            "parent_type": None,
            "vm_count": None,
        }
    )
    return {"status": status, **fields, **extra, "task": task, "guidance": guidance}


def _not_found(host: str, **extra: Any) -> dict[str, Any]:
    return _answer(
        "not_found",
        None,
        host,
        guidance=f"vCenter has no host {host!r}. Nothing was sent. Check the host id.",
        **extra,
    )


def _timed_out(
    state: HostState, outcome: VimTaskResult, method: str, **extra: Any
) -> dict[str, Any]:
    answer = _answer(
        "timeout",
        state,
        state.host,
        task=outcome.task,
        guidance=(
            f"MEHO stopped waiting for {method} {outcome.task}; it may still run. Read it with "
            f"{TASK_INFO_OP_ID}, then read the host again."
        ),
        **extra,
    )
    answer["issues"] = [_task_timeout_issue(method, outcome.task)]
    return answer


async def _after(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    before: HostState,
    outcome: VimTaskResult,
    done: str,
    is_done: Any,
    what: str,
    **extra: Any,
) -> dict[str, Any]:
    """Read the host again after the task: *done* when ``is_done(state)``, else not_verified."""
    after = await read_host_state(connector, target, operator, before.host)
    if after is not None and is_done(after):
        return _answer(done, after, before.host, task=outcome.task, **extra)
    return _answer(
        "not_verified",
        after or before,
        before.host,
        task=outcome.task,
        guidance=f"The task ended, but the host does not read back as {what}. Read it again.",
        **extra,
    )


# ---------------------------------------------------------------------------
# host.move
# ---------------------------------------------------------------------------


async def host_move_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Move a host in maintenance mode into a cluster (``MoveHostInto_Task``)."""
    host, cluster = params["host"], params["cluster"]
    refusal = needs_vcenter(target)
    if refusal is not None:
        return _answer("needs_vcenter", None, host, guidance=refusal, cluster=cluster)
    before = await read_host_state(connector, target, operator, host)
    if before is None:
        return _not_found(host, cluster=cluster)
    cluster_props = await read_object(
        connector,
        target,
        operator,
        mo_type=_CLUSTER_COMPUTE_RESOURCE_MO_TYPE,
        moid=cluster,
        props=["name"],
    )
    if cluster_props is None:
        return _answer(
            "cluster_not_found",
            before,
            host,
            guidance=f"vCenter has no cluster {cluster!r}. Nothing was sent.",
            cluster=cluster,
        )
    if before.parent == cluster:
        return _answer(
            "unchanged",
            before,
            host,
            guidance="The host already is in this cluster. Nothing was sent.",
            cluster=cluster,
        )
    if before.in_maintenance_mode is not True:
        return _answer(
            "not_in_maintenance_mode",
            before,
            host,
            guidance=(
                "The host is not in maintenance mode. Nothing was sent. Move its VMs away and "
                "put it into maintenance mode first (host.evacuate)."
            ),
            cluster=cluster,
        )
    outcome = await _run_task(
        connector,
        target,
        operator,
        op_id=_OP_MOVE_HOST_INTO,
        vmomi_path=f"/ClusterComputeResource/{cluster}/MoveHostInto_Task",
        body={"host": vim_moref(_HOST_SYSTEM_MO_TYPE, host)},
        params={"host": host, "cluster": cluster},
        label="host.move",
    )
    if isinstance(outcome, OperationResult):
        return outcome
    if outcome.timed_out:
        return _timed_out(before, outcome, "MoveHostInto_Task", cluster=cluster)
    return await _after(
        connector,
        target,
        operator,
        before=before,
        outcome=outcome,
        done="moved",
        is_done=lambda state: state.parent == cluster,
        what="in the cluster",
        cluster=cluster,
    )


# ---------------------------------------------------------------------------
# host.connect / host.disconnect
# ---------------------------------------------------------------------------


async def host_connect_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Reconnect a host with the login vCenter has (``ReconnectHost_Task``)."""
    return await _connection_change(connector, target, operator, params["host"], connect=True)


async def host_disconnect_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Disconnect a host from vCenter; its VMs keep running (``DisconnectHost_Task``)."""
    return await _connection_change(connector, target, operator, params["host"], connect=False)


async def _connection_change(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    host: str,
    *,
    connect: bool,
) -> dict[str, Any] | OperationResult:
    refusal = needs_vcenter(target)
    if refusal is not None:
        return _answer("needs_vcenter", None, host, guidance=refusal)
    before = await read_host_state(connector, target, operator, host)
    if before is None:
        return _not_found(host)
    wanted = _CONNECTED if connect else _DISCONNECTED
    if before.connection_state == wanted:
        return _answer(
            "unchanged", before, host, guidance=f"The host already is {wanted}. Nothing was sent."
        )
    if connect:
        method, vmomi_path = "ReconnectHost_Task", f"/HostSystem/{host}/ReconnectHost_Task"
    else:
        method, vmomi_path = "DisconnectHost_Task", f"/HostSystem/{host}/DisconnectHost_Task"
    outcome = await _run_task(
        connector,
        target,
        operator,
        op_id=_OP_RECONNECT_HOST if connect else _OP_DISCONNECT_HOST,
        vmomi_path=vmomi_path,
        body={},
        params={"host": host},
        label="host.connect" if connect else "host.disconnect",
    )
    if isinstance(outcome, OperationResult):
        return outcome
    if outcome.timed_out:
        return _timed_out(before, outcome, method)
    return await _after(
        connector,
        target,
        operator,
        before=before,
        outcome=outcome,
        done="connected" if connect else "disconnected",
        is_done=lambda state: state.connection_state == wanted,
        what=wanted,
    )


# ---------------------------------------------------------------------------
# Approval previews
# ---------------------------------------------------------------------------

_WHAT_HAPPENS: Final[dict[str, str]] = {
    OP_ID_HOST_MOVE: (
        "The host and every VM registered on it move into the cluster. The cluster's HA and "
        "DRS settings apply to them."
    ),
    OP_ID_HOST_CONNECT: (
        "vCenter connects to the host again and may update its agent. Settings changed on "
        "the host while it was away can be replaced by vCenter's settings."
    ),
    OP_ID_HOST_DISCONNECT: (
        "vCenter stops managing the host. Its VMs keep running, but vCenter cannot manage "
        "them and vSphere HA does not protect them until the host is connected again."
    ),
}


async def _host_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The host as it is now and what the change does. A failed read never blocks."""
    host = ctx.params.get("host")
    if not isinstance(host, str) or ctx.connector_instance is None:
        return None
    op_id = getattr(ctx.descriptor, "op_id", None)
    preview: dict[str, Any] = {"host": host}
    if isinstance(ctx.params.get("cluster"), str):
        preview["cluster"] = ctx.params["cluster"]
    try:
        state = await read_host_state(
            ctx.connector_instance,  # type: ignore[arg-type]
            ctx.target,
            ctx.operator,
            host,
        )
    except Exception as exc:
        preview["unavailable"] = [f"the host state: {preview_failure_reason(exc)}"]
    else:
        if state is None:
            preview["unavailable"] = ["the host state: vCenter has no host with this id"]
        else:
            preview.update(state.as_fields())
    if isinstance(op_id, str) and op_id in _WHAT_HAPPENS:
        preview["what_happens_next"] = _WHAT_HAPPENS[op_id]
    return preview


register_preview_builder(OP_ID_HOST_MOVE, _host_preview)
register_preview_builder(OP_ID_HOST_CONNECT, _host_preview)
register_preview_builder(OP_ID_HOST_DISCONNECT, _host_preview)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``host.add`` -- add an ESXi host to a cluster or a host folder (#4349).

This is a vendor job. ``ClusterComputeResource.AddHost_Task`` (into a cluster)
or ``Folder.AddStandaloneHost_Task`` (alone, into a host folder) returns a
task at once; vCenter then connects to the host, checks its certificate
against the thumbprint, installs its agent and adds it. The call answers
``accepted`` with the task id as soon as vCenter took the job, and the caller
follows it with ``vmware.task.info``. It never says the host is added.

Before the start it reads the cluster or folder, looks for a host with this
name in vCenter (``already_added``) and for an add task that is queued or runs
on the same cluster or folder (``already_running``, with that task's id). The
check is a help, not a lock: two callers can both check before either starts.

The host password comes from Vault (``password_secret_ref``) and goes only
into the vCenter request body. ``vmware.composite.host.add`` is pinned
``credential_write`` (``broadcast/events.py``), so the flight recorder keeps no
body and the broadcast keeps no params. A vendor text that holds the password
is replaced.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._host import _list_host_moids
from meho_backplane.connectors.vmware_rest.composites._host_membership import (
    needs_vcenter,
)
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moref_values,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _CLUSTER_COMPUTE_RESOURCE_MO_TYPE,
    _FOLDER_MO_TYPE,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.typed_ops_task_info import TASK_INFO_OP_ID
from meho_backplane.connectors.vmware_rest.typed_ops_tasks_recent import (
    _parse_task_info_results,
    build_task_info_retrieve_params,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import (
    hide_secrets,
    read_secret_param,
    unreachable_http_reason,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    fault_message,
    unwrap_vim_value,
)
from meho_backplane.connectors.vmware_rest.vim_retrieve import retrieve_all_pages
from meho_backplane.connectors.vmware_rest.vim_task import task_moref_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = ["OP_ID_HOST_ADD", "host_add_composite"]

OP_ID_HOST_ADD: Final = "vmware.composite.host.add"

# The governed vendor calls (``METHOD:/path`` of the pinned vi-json.yaml).
_OP_ADD_HOST: Final = "POST:/ClusterComputeResource/{moId}/AddHost_Task"
_OP_ADD_STANDALONE_HOST: Final = "POST:/Folder/{moId}/AddStandaloneHost_Task"

#: vCenter's names of the two add tasks (``TaskInfo.descriptionId``).
_ADD_TASK_KINDS: Final = frozenset({"ClusterComputeResource.addHost", "Folder.addStandaloneHost"})
_RUNNING_STATES: Final = frozenset({"queued", "running"})

_HOST_CONNECT_SPEC_TYPE: Final = "HostConnectSpec"
_DEFAULT_USER: Final = "root"


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _parent(params: dict[str, Any]) -> tuple[str, str, str]:
    """``(kind, moid, vim type)`` of the cluster or the host folder the call names."""
    if params.get("cluster"):
        return "cluster", str(params["cluster"]), _CLUSTER_COMPUTE_RESOURCE_MO_TYPE
    return "folder", str(params["folder"]), _FOLDER_MO_TYPE


def _add_plan(params: dict[str, Any]) -> dict[str, Any]:
    """What the add does, without the password: for the approver and the gate."""
    kind, parent, _mo_type = _parent(params)
    return {
        "action": "add the host to the cluster" if kind == "cluster" else "add the host alone",
        "parent": parent,
        "parent_type": kind,
        "host_name": params["host_name"],
        "port": params.get("port", 443),
        "user_name": params.get("user_name", _DEFAULT_USER),
        "ssl_thumbprint": params["ssl_thumbprint"],
        "password_source": "vault",
        "password_secret_ref": params.get("password_secret_ref"),
    }


def _connect_spec(params: dict[str, Any], password: str) -> dict[str, Any]:
    """The ``HostConnectSpec``; ``force`` is false, so a host of another vCenter is refused."""
    spec: dict[str, Any] = {
        VIM_TYPE_NAME_KEY: _HOST_CONNECT_SPEC_TYPE,
        "hostName": params["host_name"],
        "sslThumbprint": params["ssl_thumbprint"],
        "userName": params.get("user_name", _DEFAULT_USER),
        "password": password,
        "force": False,
    }
    if "port" in params:
        spec["port"] = params["port"]
    return spec


def _add_answer(
    status: str,
    params: dict[str, Any],
    guidance: str,
    *,
    parent_name: str | None = None,
    task: str | None = None,
    host: str | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    kind, parent, _mo_type = _parent(params)
    return {
        "status": status,
        "host_name": params["host_name"],
        "parent": parent,
        "parent_type": kind,
        "parent_name": parent_name,
        "task": task,
        "host": host,
        "status_read": (
            {"op_id": TASK_INFO_OP_ID, "params": {"task": task}} if task is not None else None
        ),
        "error_type": error_type,
        "error_message": error_message,
        "guidance": guidance,
    }


async def _running_add_task(
    connector: VmwareRestConnector, target: Any, operator: Operator, recent: Any
) -> str | None:
    """The id of an add task that is queued or runs on the parent, else ``None``.

    The task list is a read of many objects, so it follows every page: a
    running add on a later page must not start a second add.
    """
    task_ids = [moid for _type, moid in moref_values(recent)]
    if not task_ids:
        return None
    payload = await retrieve_all_pages(
        connector, target, operator, build_task_info_retrieve_params(task_ids)
    )
    for task, info in _parse_task_info_results(payload).items():
        if not isinstance(info, dict):
            continue
        if info.get("descriptionId") in _ADD_TASK_KINDS and info.get("state") in _RUNNING_STATES:
            return task
    return None


def _start_fault(exc: httpx.HTTPStatusError, secrets: tuple[str, ...]) -> tuple[str | None, str]:
    """``(fault type, text)`` of a refused start, with no text that holds the password."""
    try:
        body = exc.response.json()
    except ValueError:
        body = None
    fault = unwrap_vim_value(body) if isinstance(body, dict) else None
    fault_type = fault.get(VIM_TYPE_NAME_KEY) if isinstance(fault, dict) else None
    text = fault_message(fault) if isinstance(fault, dict) else None
    message = hide_secrets(text, secrets) or f"HTTP {exc.response.status_code}"
    return (fault_type if isinstance(fault_type, str) else None), message


def _start_unknown(params: dict[str, Any], parent_name: str | None, error: str) -> dict[str, Any]:
    """The answer when MEHO cannot know whether vCenter took the job."""
    return _add_answer(
        "start_unknown",
        params,
        "The start got no clear answer; MEHO does not know if the add runs. Do not start "
        "again blindly: call host.add again later. It answers already_added or "
        "already_running when the first call worked.",
        parent_name=parent_name,
        error_message=error,
    )


async def host_add_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Start adding an ESXi host to a cluster or a host folder; answer ``accepted``."""
    refusal = needs_vcenter(target)
    if refusal is not None:
        return _add_answer("needs_vcenter", params, refusal)
    kind, parent, mo_type = _parent(params)
    parent_props = await read_object(
        connector, target, operator, mo_type=mo_type, moid=parent, props=["name", "recentTask"]
    )
    if parent_props is None:
        return _add_answer(
            "parent_not_found", params, f"vCenter has no {kind} {parent!r}. Nothing was sent."
        )
    parent_name = _text(parent_props.get("name"))
    existing = await _list_host_moids(
        connector, target, operator, {"filter.names": [params["host_name"]]}
    )
    if existing:
        return _add_answer(
            "already_added",
            params,
            "A host with this name already is in vCenter. Nothing was sent. Read it, or "
            "connect it with host.connect when it is disconnected.",
            parent_name=parent_name,
            host=existing[0],
        )
    running = await _running_add_task(connector, target, operator, parent_props.get("recentTask"))
    if running is not None:
        return _add_answer(
            "already_running",
            params,
            f"A host add already runs on this {kind}. Nothing was started. Follow it with "
            f"{TASK_INFO_OP_ID}; when it ended, call host.add again if your host is not there.",
            parent_name=parent_name,
            task=running,
        )
    password = await read_secret_param(operator, target, params, prefix="password")
    secrets = (password,)
    spec = _connect_spec(params, password)
    if kind == "cluster":
        op_id, vmomi_path = _OP_ADD_HOST, f"/ClusterComputeResource/{parent}/AddHost_Task"
        body: dict[str, Any] = {"spec": spec, "asConnected": True}
    else:
        op_id, vmomi_path = _OP_ADD_STANDALONE_HOST, f"/Folder/{parent}/AddStandaloneHost_Task"
        body = {"spec": spec, "addConnected": True}
    try:
        gate, payload = await _write_vmomi_sub_op(
            connector,
            target,
            operator,
            op_id=op_id,
            vmomi_path=vmomi_path,
            body=body,
            params=_add_plan(params),
        )
    except httpx.HTTPStatusError as exc:
        if unreachable_http_reason(exc) is not None:
            # A gateway answered, not vCenter's service: the job may have started.
            return _start_unknown(params, parent_name, f"HTTP {exc.response.status_code}")
        fault_type, message = _start_fault(exc, secrets)
        return _add_answer(
            "start_failed",
            params,
            "vCenter refused the start; nothing runs. Fix the cause the error names, then "
            "call again.",
            parent_name=parent_name,
            error_type=fault_type,
            error_message=message,
        )
    except httpx.TransportError as exc:
        return _start_unknown(params, parent_name, type(exc).__name__)
    if gate is not None:
        return gate
    task = task_moref_value(_unwrap_value(payload))
    return _add_answer(
        "accepted",
        params,
        f"vCenter took the job; the host is not added yet. Read {TASK_INFO_OP_ID} with this "
        "task id until the state is success or error.",
        parent_name=parent_name,
        task=task,
    )


async def _host_add_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The add plan: where the host goes and how vCenter checks it; never the password."""
    if not isinstance(ctx.params.get("host_name"), str):
        return None
    return {
        **_add_plan(ctx.params),
        "what_happens_next": (
            "vCenter connects to the host, checks its certificate against ssl_thumbprint, "
            "installs its agent and adds the host with its VMs. The call answers accepted at "
            "once; follow the task with vmware.task.info."
        ),
    }

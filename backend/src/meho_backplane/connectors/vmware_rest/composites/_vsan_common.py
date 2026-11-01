# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared parts of the vSAN cluster operations of #4446.

The six operations (HCL database update, cluster power pre-check, cluster
shut down and start, proactive tests, object repair) call vSAN methods on
three vSAN objects that vCenter serves for all clusters, each with a fixed
id (vendor vSAN Management API reference, 8.0 U2):

* ``VsanVcClusterHealthSystem`` -- ``vsan-cluster-health-system``;
* ``VsanClusterPowerSystem`` -- ``vsan-cluster-power-system``;
* ``VsanVumSystem`` -- ``vsan-vum-system``.

Every method is a module-level :class:`~.._mount.VsanMethod` constant, so the
reconcile lanes check it against the vendor files of vCenter 8.0 (vSAN SDK
method lists) and 9.0 (``vi-json.yaml``). Every call goes through
``VmwareRestConnector._post_vsan``: VI-JSON on vCenter 9.x, SOAP to
``/vsanHealth`` on vCenter 8.0.

A call that changes something goes through :func:`vsan_write`: the sub-call
gate of the composite writes (``enforce_subop_policy``, #2254) first, then
the vSAN call. A gate that holds or denies the call comes back as is, and
nothing is sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest._mount import VsanMethod, vsan_method_path
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import CLUSTER_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.typed_ops import _int_or_none, _unwrap_value
from meho_backplane.connectors.vmware_rest.vim_body import fault_message, vim_moref
from meho_backplane.operations.composite import enforce_subop_policy

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "RUNNING_TASK_STATES",
    "TASK_INFO_OP_ID",
    "VSAN_CREATE_VM_TEST",
    "VSAN_NETWORK_PERF_TEST",
    "VSAN_PERFORM_CLUSTER_POWER_ACTION",
    "VSAN_QUERY_CLUSTER_POWER_CONTEXT",
    "VSAN_REPAIR_OBJECTS",
    "VSAN_RUN_VMDK_LOAD_TEST",
    "VSAN_UPDATE_HCL_DB_FROM_WEB",
    "VSAN_UPLOAD_HCL_DB",
    "VSAN_UPLOAD_RELEASE_DB",
    "VSAN_VMDK_WORKLOAD_TYPES",
    "cluster_problem",
    "cluster_ref",
    "dict_items",
    "read_task_state",
    "status_read",
    "vendor_error",
    "vsan_op_id",
    "vsan_read",
    "vsan_write",
]

_HEALTH_TYPE: Final = "VsanVcClusterHealthSystem"
_HEALTH_MOID: Final = "vsan-cluster-health-system"
_POWER_TYPE: Final = "VsanClusterPowerSystem"
_POWER_MOID: Final = "vsan-cluster-power-system"

#: Replace the HCL database with one vCenter downloads (from the vendor's
#: standard location, or from the ``url`` the caller gives).
VSAN_UPDATE_HCL_DB_FROM_WEB: Final = VsanMethod(
    _HEALTH_TYPE, _HEALTH_MOID, "VsanVcUpdateHclDbFromWeb"
)
#: Replace the HCL database with the given one (``db``: gzipped JSON, base64).
VSAN_UPLOAD_HCL_DB: Final = VsanMethod(_HEALTH_TYPE, _HEALTH_MOID, "VsanVcUploadHclDb")
#: Replace the release catalog with the given one (``db``: JSON text).
VSAN_UPLOAD_RELEASE_DB: Final = VsanMethod(
    "VsanVumSystem", "vsan-vum-system", "VsanVcUploadReleaseDb"
)
#: The power state of a cluster and the task of the last power action.
VSAN_QUERY_CLUSTER_POWER_CONTEXT: Final = VsanMethod(
    _POWER_TYPE, _POWER_MOID, "QueryClusterPowerContext"
)
#: Shut down or start a whole cluster; returns a vCenter task.
VSAN_PERFORM_CLUSTER_POWER_ACTION: Final = VsanMethod(
    _POWER_TYPE, _POWER_MOID, "PerformClusterPowerAction"
)
#: Proactive test: create a small VM on each host and delete it again.
VSAN_CREATE_VM_TEST: Final = VsanMethod(
    _HEALTH_TYPE, _HEALTH_MOID, "VsanQueryVcClusterCreateVmHealthTest"
)
#: Proactive test: network speed between the hosts.
VSAN_NETWORK_PERF_TEST: Final = VsanMethod(
    _HEALTH_TYPE, _HEALTH_MOID, "VsanQueryVcClusterNetworkPerfTest"
)
#: The storage load profiles (workload types) the storage test can run.
VSAN_VMDK_WORKLOAD_TYPES: Final = VsanMethod(
    _HEALTH_TYPE, _HEALTH_MOID, "VsanQueryVcClusterVmdkWorkloadTypes"
)
#: Proactive test: storage load on each host; returns a vCenter task.
VSAN_RUN_VMDK_LOAD_TEST: Final = VsanMethod(
    _HEALTH_TYPE, _HEALTH_MOID, "VsanVcClusterRunVmdkLoadTest"
)
#: Put objects with reduced redundancy into the repair queue now.
VSAN_REPAIR_OBJECTS: Final = VsanMethod(
    _HEALTH_TYPE, _HEALTH_MOID, "VsanHealthRepairClusterObjectsImmediate"
)

#: The status read of a vCenter task a change started.
TASK_INFO_OP_ID: Final = "vmware.task.info"
#: vCenter task states of a task that has not ended.
RUNNING_TASK_STATES: Final = frozenset({"queued", "running"})

_CLUSTER_MO_TYPE: Final = "ClusterComputeResource"
_TASK_MO_TYPE: Final = "Task"


def vsan_op_id(method: VsanMethod) -> str:
    """The governance key of a vSAN call: the path as the vendor VI/JSON file names it."""
    return f"POST:{vsan_method_path(method.mo_type, '{moId}', method.method)}"


def cluster_ref(cluster: str) -> dict[str, str]:
    """The ``ClusterComputeResource`` reference of *cluster* in the vim body form."""
    return vim_moref(_CLUSTER_MO_TYPE, cluster)


def cluster_problem(value: Any) -> str | None:
    """Why *value* is not a cluster moid, or ``None``."""
    return moid_problem(value, CLUSTER_MOID_PATTERN, "cluster")


def status_read(task: str) -> dict[str, Any]:
    """The read that follows a vCenter task: ``vmware.task.info`` with its id."""
    return {"op_id": TASK_INFO_OP_ID, "params": {"task": task}}


def dict_items(value: Any) -> list[dict[str, Any]]:
    """The dict entries of a vSAN list field, as a list.

    VI-JSON always sends a list. SOAP (vCenter 8.0) has no list marker, so a
    list with one entry comes back as a single dict.
    """
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


async def vsan_read(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    method: VsanMethod,
    body: dict[str, Any],
) -> Any:
    """Send one vSAN call that changes nothing and return its answer."""
    return _unwrap_value(await connector._post_vsan(target, method, operator=operator, json=body))


async def vsan_write(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    method: VsanMethod,
    body: dict[str, Any],
    params: dict[str, Any],
) -> tuple[OperationResult | None, Any]:
    """Check the gate, then send one vSAN call that changes something.

    Returns ``(gate, None)`` when the gate holds or denies the call (nothing
    is sent), else ``(None, answer)``. *params* name what the call changes,
    for the approval request; they never hold the request body (an HCL
    database can be megabytes).
    """
    gate = await enforce_subop_policy(
        operator=operator,
        connector_id=_write._step_connector_id(connector),
        op_id=vsan_op_id(method),
        safety_level=_write._WRITE_SAFETY_LEVEL,
        requires_approval=_write._WRITE_REQUIRES_APPROVAL,
        target=target,
        params=params,
    )
    if gate is not None:
        return gate, None
    answer = await connector._post_vsan(target, method, operator=operator, json=body)
    return None, _unwrap_value(answer)


def vendor_error(exc: BaseException) -> dict[str, str | None]:
    """The vendor's fault type and text of a failed call, as the dispatcher reads them."""
    from meho_backplane.operations._errors import result_connector_error

    extras = result_connector_error("vmware.vsan", exc, 0.0).extras or {}
    fault = extras.get("upstream_fault")
    fault = fault if isinstance(fault, dict) else {}
    return {
        "type": fault.get("type") or extras.get("exception_class"),
        "message": (
            fault.get("faultstring")
            or extras.get("upstream_message")
            or extras.get("exception_message")
        ),
    }


async def read_task_state(
    connector: VmwareRestConnector, target: Any, operator: Operator, task: str
) -> dict[str, Any]:
    """The state of one vCenter task: ``{task, state, progress, error}``.

    ``state`` is the vendor's state (``queued``, ``running``, ``success``,
    ``error``), or ``None`` when vCenter no longer knows the task.
    """
    props = await read_object(
        connector, target, operator, mo_type=_TASK_MO_TYPE, moid=task, props=["info"]
    )
    info = props.get("info") if props is not None else None
    if not isinstance(info, dict):
        return {"task": task, "state": None, "progress": None, "error": None}
    state = info.get("state")
    return {
        "task": task,
        "state": state if isinstance(state, str) else None,
        "progress": _int_or_none(info.get("progress")),
        "error": fault_message(info.get("error")) if state == "error" else None,
    }

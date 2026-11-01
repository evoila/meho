# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vSAN proactive tests and the immediate object repair (#4446).

* ``vmware.composite.vsan.proactive_test.run`` runs one proactive test and
  returns its result, one row per host:

  - ``vm_creation`` -- ``VsanQueryVcClusterCreateVmHealthTest``: vSAN creates
    a small VM on each host and deletes it again;
  - ``network_performance`` -- ``VsanQueryVcClusterNetworkPerfTest``: vSAN
    measures the network speed between the hosts. It puts real load on the
    vSAN network;
  - ``storage_load`` -- ``VsanQueryVcClusterVmdkWorkloadTypes`` (the load
    profiles), then ``VsanVcClusterRunVmdkLoadTest``: vSAN writes and reads
    test disks on each host. It puts real load on the storage. vCenter runs
    it as a task; MEHO follows the task to its end and returns its result.

  The first two answer in one call. MEHO waits at most 30 seconds for one
  answer from vCenter, so their test time is at most 25 seconds.

* ``vmware.composite.vsan.object.repair`` --
  ``VsanHealthRepairClusterObjectsImmediate``: puts the objects with reduced
  redundancy (all of them, or the given object UUIDs) into the repair queue
  now, without waiting for the vSAN repair timer. vCenter runs a task that
  ends when the objects are queued; the repair itself goes on in vSAN after
  that. MEHO follows that task to its end.

Each test and the repair go through the sub-call gate of the composite
writes before they are sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import moid_problem
from meho_backplane.connectors.vmware_rest.composites._vsan_common import (
    VSAN_CREATE_VM_TEST,
    VSAN_NETWORK_PERF_TEST,
    VSAN_REPAIR_OBJECTS,
    VSAN_RUN_VMDK_LOAD_TEST,
    VSAN_VMDK_WORKLOAD_TYPES,
    cluster_problem,
    cluster_ref,
    dict_items,
    status_read,
    vsan_read,
    vsan_write,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import DATASTORE_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.typed_ops import _int_or_none
from meho_backplane.connectors.vmware_rest.vim_body import fault_message, vim_moref
from meho_backplane.connectors.vmware_rest.vim_task import poll_vim_task, task_moref_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "OBJECT_REPAIR_OP_ID",
    "PROACTIVE_TEST_OP_ID",
    "checks_preview",
    "vsan_object_repair_composite",
    "vsan_proactive_test_composite",
]

PROACTIVE_TEST_OP_ID: Final = "vmware.composite.vsan.proactive_test.run"
OBJECT_REPAIR_OP_ID: Final = "vmware.composite.vsan.object.repair"

TEST_VM_CREATION: Final = "vm_creation"
TEST_NETWORK: Final = "network_performance"
TEST_STORAGE: Final = "storage_load"

_DEFAULT_SHORT_SECONDS: Final = 20
_DEFAULT_STORAGE_SECONDS: Final = 300
#: Time MEHO waits for a task beyond the test time.
_TASK_GRACE_SECONDS: Final = 300.0
_REPAIR_TASK_SECONDS: Final = 300.0
_DEFAULT_RUN_NAME: Final = "meho-storage-load-test"


def _invalid(problem: str, **fields: Any) -> dict[str, Any]:
    return {
        "status": "invalid_request",
        **fields,
        "issues": [issue("validation", "error", problem)],
        "guidance": "Fix the input and call again. Nothing was run.",
    }


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _float_or_none(value: Any) -> float | None:
    """A number from VI-JSON, or from SOAP text; ``None`` otherwise."""
    if isinstance(value, bool):
        return None
    try:
        return float(value) if isinstance(value, (int, float, str)) else None
    except ValueError:
        return None


def _host_row(**fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "host": None,
        "status": None,
        "error": None,
        "bandwidth_bps": None,
        "loss_percent": None,
        "jitter_ms": None,
        "iops": None,
        "throughput_bps": None,
        "avg_latency_us": None,
        "max_latency_us": None,
    }
    row.update(fields)
    return row


def _vm_creation_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _host_row(
            host=_str_or_none(host.get("hostname")),
            status=_str_or_none(host.get("state")),
            error=fault_message(host.get("fault")),
        )
        for host in dict_items(result.get("hostResults"))
    ]


def _network_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for host in dict_items(result.get("hostResults")):
        rows.append(
            _host_row(
                host=_str_or_none(host.get("hostname")),
                status=_str_or_none(host.get("status")),
                bandwidth_bps=_int_or_none(host.get("bandwidthBps")),
                loss_percent=_int_or_none(host.get("lossPct")),
                jitter_ms=_float_or_none(host.get("jitterMs")),
            )
        )
    return rows


def _storage_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for host in dict_items(result.get("hostResults")):
        name = _str_or_none(host.get("hostname"))
        issue_found = host.get("issueFound") in (True, "true")
        status = "issue_found" if issue_found else "ok"
        disks = dict_items(host.get("vmdkResults"))
        if not disks:
            rows.append(
                _host_row(host=name, status=status, error=_str_or_none(host.get("faultMessage")))
            )
        for disk in disks:
            rows.append(
                _host_row(
                    host=name,
                    status=status,
                    error=_str_or_none(disk.get("faultMessage"))
                    or _str_or_none(host.get("faultMessage")),
                    iops=_int_or_none(disk.get("iops")),
                    throughput_bps=_int_or_none(disk.get("tputBps")),
                    avg_latency_us=_int_or_none(disk.get("avgLatencyUs")),
                    max_latency_us=_int_or_none(disk.get("maxLatencyUs")),
                )
            )
    return rows


def _test_answer(
    cluster: str, test: str, result: Any, rows: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    cluster_result = result.get("clusterResult") if isinstance(result, dict) else None
    cluster_result = cluster_result if isinstance(cluster_result, dict) else {}
    overall = _str_or_none(cluster_result.get("overallStatus"))
    return {
        "status": "completed",
        "cluster": cluster,
        "test": test,
        "overall_status": overall,
        "overall_status_description": _str_or_none(cluster_result.get("overallStatusDescription")),
        "checked_at": _str_or_none(cluster_result.get("timestamp")),
        "hosts": rows,
        **extra,
        "guidance": (
            "Report overall_status and each host that did not pass. Run the test again after a fix."
        ),
    }


def _short_seconds(params: dict[str, Any]) -> int:
    value = params.get("duration_seconds", _DEFAULT_SHORT_SECONDS)
    return int(value)


async def _run_short_test(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    cluster: str,
    test: str,
    params: dict[str, Any],
) -> dict[str, Any] | OperationResult:
    seconds = _short_seconds(params)
    if not 5 <= seconds <= 25:
        return _invalid("duration_seconds must be 5 to 25 for this test", cluster=cluster)
    body: dict[str, Any] = {"cluster": cluster_ref(cluster)}
    if test == TEST_VM_CREATION:
        method = VSAN_CREATE_VM_TEST
        body["timeout"] = seconds
        datastore = params.get("datastore")
        if datastore is not None:
            problem = moid_problem(datastore, DATASTORE_MOID_PATTERN, "datastore")
            if problem is not None:
                return _invalid(problem, cluster=cluster)
            body["datastore"] = vim_moref("Datastore", str(datastore))
    else:
        method = VSAN_NETWORK_PERF_TEST
        body["multicast"] = False
        body["durationSec"] = seconds
    gate, result = await vsan_write(
        connector,
        target,
        operator,
        method=method,
        body=body,
        params={"cluster": cluster, "test": test, "duration_seconds": seconds},
    )
    if gate is not None:
        return gate
    if not isinstance(result, dict):
        raise RuntimeError(f"vSAN {test} test on cluster {cluster!r}: vCenter sent no result")
    rows = _vm_creation_rows(result) if test == TEST_VM_CREATION else _network_rows(result)
    return _test_answer(cluster, test, result, rows)


async def _run_storage_test(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    cluster: str,
    params: dict[str, Any],
) -> dict[str, Any] | OperationResult:
    workload = params.get("workload")
    seconds = int(params.get("duration_seconds", _DEFAULT_STORAGE_SECONDS))
    if not isinstance(workload, str) or not workload:
        return _invalid("workload is required for the storage_load test", cluster=cluster)
    if not 60 <= seconds <= 3600:
        return _invalid("duration_seconds must be 60 to 3600 for this test", cluster=cluster)
    types = dict_items(await vsan_read(connector, target, operator, VSAN_VMDK_WORKLOAD_TYPES, {}))
    chosen = next((t for t in types if t.get("typeId") == workload), None)
    if chosen is None:
        known = sorted(str(t.get("typeId")) for t in types if t.get("typeId"))
        return _invalid(
            f"workload {workload!r} is not a storage load profile of this vCenter; "
            f"known: {', '.join(known) or 'none'}",
            cluster=cluster,
            workloads=known,
        )
    gate, answer = await vsan_write(
        connector,
        target,
        operator,
        method=VSAN_RUN_VMDK_LOAD_TEST,
        body={
            "cluster": cluster_ref(cluster),
            "runname": str(params.get("run_name") or _DEFAULT_RUN_NAME),
            "durationSec": seconds,
            "specs": dict_items(chosen.get("specs")),
        },
        params={"cluster": cluster, "test": TEST_STORAGE, "workload": workload},
    )
    if gate is not None:
        return gate
    task = task_moref_value(answer)
    if task is None:
        raise RuntimeError(f"vSAN storage test on cluster {cluster!r}: vCenter sent no task id")
    outcome = await poll_vim_task(
        connector, target, operator, task=task, timeout_seconds=seconds + _TASK_GRACE_SECONDS
    )
    if outcome.succeeded:
        result = outcome.result if isinstance(outcome.result, dict) else {}
        return _test_answer(cluster, TEST_STORAGE, result, _storage_rows(result), task=task)
    return _task_problem(cluster, TEST_STORAGE, task, outcome.timed_out, outcome.error_message)


def _task_problem(
    cluster: str, what: str, task: str, timed_out: bool, error: str | None
) -> dict[str, Any]:
    if timed_out:
        status = "not_confirmed"
        message = f"the {what} task {task} did not end in time; it may still run"
        guidance = "Follow the task with vmware.task.info until it ends."
    else:
        status = "failed"
        message = f"the {what} task {task} failed: {error or 'no error text from vCenter'}"
        guidance = "Read the error, fix the cause, then call again."
    return {
        "status": status,
        "cluster": cluster,
        "task": task,
        "status_read": status_read(task),
        "error": error,
        "issues": [issue("connector", "error", message)],
        "guidance": guidance,
    }


async def vsan_proactive_test_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Run one vSAN proactive test and return its result.

    Op-id: ``vmware.composite.vsan.proactive_test.run``. ``status`` is
    ``completed`` with ``overall_status`` and one row per host (the storage
    test: one row per test disk). A storage test task that fails gives
    ``failed`` (an error issue with vCenter's text), one that does not end in
    time ``not_confirmed``. A vSAN fault on the call raises; the dispatcher
    answers with vCenter's fault type and text.
    """
    cluster = params.get("cluster")
    problem = cluster_problem(cluster)
    if problem is not None:
        return _invalid(problem, cluster=cluster)
    test = params.get("test")
    if test in (TEST_VM_CREATION, TEST_NETWORK):
        return await _run_short_test(connector, target, operator, str(cluster), test, params)
    if test == TEST_STORAGE:
        return await _run_storage_test(connector, target, operator, str(cluster), params)
    return _invalid(
        f"test must be {TEST_VM_CREATION}, {TEST_NETWORK} or {TEST_STORAGE}", cluster=cluster
    )


async def vsan_object_repair_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Queue the repair of vSAN objects now (``VsanHealthRepairClusterObjectsImmediate``).

    Op-id: ``vmware.composite.vsan.object.repair``. Without
    ``object_uuids`` every object of the cluster that needs a repair is
    queued. ``repair_queued`` when the vCenter task ended well: the objects
    are in the repair queue, and vSAN repairs them in the background.
    """
    cluster = params.get("cluster")
    problem = cluster_problem(cluster)
    if problem is not None:
        return _invalid(problem, cluster=cluster)
    cluster = str(cluster)
    uuids = [str(u) for u in params.get("object_uuids") or []]
    body: dict[str, Any] = {"cluster": cluster_ref(cluster)}
    if uuids:
        body["uuids"] = uuids
    gate, answer = await vsan_write(
        connector,
        target,
        operator,
        method=VSAN_REPAIR_OBJECTS,
        body=body,
        params={"cluster": cluster, "object_uuids": uuids or "all"},
    )
    if gate is not None:
        return gate
    task = task_moref_value(answer)
    if task is None:
        raise RuntimeError(f"vSAN object repair on cluster {cluster!r}: vCenter sent no task id")
    outcome = await poll_vim_task(
        connector, target, operator, task=task, timeout_seconds=_REPAIR_TASK_SECONDS
    )
    if not outcome.succeeded:
        return {
            **_task_problem(cluster, "repair", task, outcome.timed_out, outcome.error_message),
            "object_uuids": uuids,
        }
    return {
        "status": "repair_queued",
        "cluster": cluster,
        "task": task,
        "status_read": None,
        "error": None,
        "object_uuids": uuids,
        "guidance": (
            "The objects are in the repair queue; vSAN repairs them in the background. "
            "Check the progress with vmware.host.vsan_health."
        ),
    }


async def checks_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """For the approver: what the test or the repair does, and what else is affected."""
    cluster = ctx.params.get("cluster")
    if ctx.descriptor.op_id == OBJECT_REPAIR_OP_ID:
        uuids = ctx.params.get("object_uuids") or []
        scope = f"{len(uuids)} given object(s)" if uuids else "every object that needs it"
        return {
            "summary": f"Queue the repair of {scope} in vSAN cluster {cluster} now.",
            "blast_radius": {
                "affected": "The repair copies data between the hosts; it adds disk and "
                "network load until it ends.",
                "cannot_undo": "A started repair runs to its end in vSAN.",
            },
        }
    test = ctx.params.get("test")
    load = {
        TEST_VM_CREATION: "a small VM is created on each host and deleted again",
        TEST_NETWORK: "real network load between the hosts",
        TEST_STORAGE: "real storage load on each host",
    }.get(str(test), "a test")
    return {
        "summary": f"Run the vSAN {test} test on cluster {cluster}.",
        "blast_radius": {
            "affected": f"While it runs: {load}. Workloads on the cluster can slow down.",
            "cannot_undo": "Nothing stays changed after the test.",
        },
    }

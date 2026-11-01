# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shut down and start a whole vSAN cluster, and the pre-check before it (#4446).

* ``vmware.vsan.cluster.power.precheck`` (read): can the whole cluster be shut
  down now, and what blocks it. Two vSAN reads:
  ``VsanQueryVcClusterHealthSummary`` with the ``clusterPowerOffPrecheck``
  view (the checks vSAN runs before a cluster shut down), and
  ``QueryClusterPowerContext`` (the power state of the cluster and the task
  of the last power action).
* ``vmware.composite.vsan.cluster.shutdown`` and ``.start`` (changes):
  ``PerformClusterPowerAction`` with ``targetPowerStatus`` set to
  ``clusterPoweredOff`` or ``clusterPoweredOn``. vCenter runs the action as a
  task after the call returns (a vendor job). The answer is ``accepted`` with
  the task id and the status read (``vmware.task.info``); it never says the
  work is done.

Before a start, the change reads the power context. When its task of the last
power action still runs, the answer is ``already_running`` with that task id,
and nothing new starts. This check is a help, not a lock: two callers can both
check before either starts. When the cluster already has the asked power
state, the answer is ``unchanged``.

MEHO keeps no job state between calls; the caller keeps the task id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._vsan_common import (
    RUNNING_TASK_STATES,
    VSAN_PERFORM_CLUSTER_POWER_ACTION,
    VSAN_QUERY_CLUSTER_POWER_CONTEXT,
    cluster_problem,
    cluster_ref,
    dict_items,
    read_task_state,
    status_read,
    vsan_read,
    vsan_write,
)
from meho_backplane.connectors.vmware_rest.typed_ops_host_vsan_health import (
    VSAN_QUERY_HEALTH_SUMMARY,
    _parse_vsan_health_group,
)
from meho_backplane.connectors.vmware_rest.vim_task import task_moref_value

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "CLUSTER_POWERED_OFF",
    "CLUSTER_POWERED_ON",
    "POWER_PRECHECK_OP_ID",
    "SHUTDOWN_OP_ID",
    "START_OP_ID",
    "cluster_power_preview",
    "vsan_cluster_power_precheck_composite",
    "vsan_cluster_shutdown_composite",
    "vsan_cluster_start_composite",
]

POWER_PRECHECK_OP_ID: Final = "vmware.vsan.cluster.power.precheck"
SHUTDOWN_OP_ID: Final = "vmware.composite.vsan.cluster.shutdown"
START_OP_ID: Final = "vmware.composite.vsan.cluster.start"

#: ``ClusterPowerStatus`` values of the two power actions.
CLUSTER_POWERED_OFF: Final = "clusterPoweredOff"
CLUSTER_POWERED_ON: Final = "clusterPoweredOn"
#: The health view with the checks vSAN runs before a cluster shut down.
_PRECHECK_PERSPECTIVE: Final = "clusterPowerOffPrecheck"
_POWER_ACTION_SPEC_TYPE: Final = "PerformClusterPowerActionSpec"
_DEFAULT_REASON: Final = "Planned maintenance"

_RED: Final = "red"
_YELLOW: Final = "yellow"


def _invalid(problem: str, **fields: Any) -> dict[str, Any]:
    return {
        "status": "invalid_request",
        **fields,
        "issues": [issue("validation", "error", problem)],
        "guidance": "Fix the input and call again. Nothing was changed.",
    }


def _message_text(value: Any) -> str | None:
    """The text of a vim ``LocalizableMessage`` (or a plain text)."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        text = value.get("message") or value.get("key")
        return text if isinstance(text, str) and text else None
    return None


def _texts(value: Any) -> list[str]:
    """A list of texts; SOAP sends a list with one entry as a single text."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


async def _power_context(
    connector: VmwareRestConnector, target: Any, operator: Operator, cluster: str
) -> dict[str, Any]:
    """The power state of *cluster* and the state of its last power task."""
    raw = await vsan_read(
        connector,
        target,
        operator,
        VSAN_QUERY_CLUSTER_POWER_CONTEXT,
        {"cluster": cluster_ref(cluster)},
    )
    context = raw if isinstance(raw, dict) else {}
    task = task_moref_value(context.get("trackingTask"))
    task_state = await read_task_state(connector, target, operator, task) if task else None
    current = context.get("currentClusterPowerStatus")
    return {
        "current_status": current if isinstance(current, str) else None,
        "ordered_statuses": _texts(context.get("orderedClusterPowerStatus")),
        "tracking_task": task_state,
        "last_error": _message_text(context.get("lastErrorMessage")),
    }


def _check_rows(groups: list[dict[str, Any]], colour: str) -> list[dict[str, Any]]:
    return [
        {
            "group_name": group["group_name"],
            "test_id": test["test_id"],
            "test_name": test["test_name"],
            "test_health": test["test_health"],
            "test_short_description": test["test_short_description"],
        }
        for group in groups
        for test in group["tests"]
        if test["test_health"] == colour
    ]


async def _precheck(
    connector: VmwareRestConnector, target: Any, operator: Operator, cluster: str
) -> dict[str, Any]:
    summary = await vsan_read(
        connector,
        target,
        operator,
        VSAN_QUERY_HEALTH_SUMMARY,
        {"cluster": cluster_ref(cluster), "perspective": _PRECHECK_PERSPECTIVE},
    )
    if not isinstance(summary, dict):
        raise RuntimeError(
            f"vSAN power pre-check for cluster {cluster!r}: vCenter sent no health summary"
        )
    groups = [_parse_vsan_health_group(g) for g in dict_items(summary.get("groups"))]
    blocking = _check_rows(groups, _RED)
    overall = summary.get("overallHealth")
    power = await _power_context(connector, target, operator, cluster)
    ready = overall != _RED and not blocking
    if ready:
        guidance = (
            "No check is red: the cluster can be shut down. Read the warnings first. "
            "To shut it down, call vmware.composite.vsan.cluster.shutdown."
        )
    else:
        guidance = "Fix each blocking check before a shut down, then run this check again."
    return {
        "cluster": cluster,
        "ready_to_shut_down": ready,
        "overall_health": overall if isinstance(overall, str) else None,
        "blocking": blocking,
        "warnings": _check_rows(groups, _YELLOW),
        "checked_at": summary.get("timestamp"),
        "power": power,
        "guidance": guidance,
    }


async def vsan_cluster_power_precheck_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Can the whole vSAN cluster be shut down now, and what blocks it.

    Op-id: ``vmware.vsan.cluster.power.precheck``. A red check blocks the
    shut down; a yellow check is a warning. The answer also gives the power
    state of the cluster and the task of the last power action, so it is the
    read to use after a lost reply of a shut down or start. A vSAN fault
    raises; the dispatcher answers with vCenter's fault type and text.
    """
    cluster = params.get("cluster")
    problem = cluster_problem(cluster)
    if problem is not None:
        return _invalid(problem, cluster=cluster)
    return await _precheck(connector, target, operator, str(cluster))


def _job_answer(
    status: str, cluster: str, target_status: str, task: str | None, guidance: str, **extra: Any
) -> dict[str, Any]:
    return {
        "status": status,
        "cluster": cluster,
        "target_power_status": target_status,
        "task": task,
        "status_read": status_read(task) if task else None,
        **extra,
        "guidance": guidance,
    }


async def _power_action(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
    target_status: str,
) -> dict[str, Any] | OperationResult:
    cluster = params.get("cluster")
    problem = cluster_problem(cluster)
    if problem is not None:
        return _invalid(problem, cluster=cluster, target_power_status=target_status)
    cluster = str(cluster)
    power = await _power_context(connector, target, operator, cluster)
    running = power["tracking_task"]
    if running is not None and running["state"] in RUNNING_TASK_STATES:
        return _job_answer(
            "already_running",
            cluster,
            target_status,
            running["task"],
            "A power action of this cluster still runs; MEHO started nothing. Follow its "
            "task with vmware.task.info until it ends.",
            current_status=power["current_status"],
        )
    if power["current_status"] == target_status:
        return _job_answer(
            "unchanged",
            cluster,
            target_status,
            None,
            f"The cluster is already {target_status}; MEHO started nothing.",
            current_status=power["current_status"],
        )
    spec: dict[str, Any] = {
        "_typeName": _POWER_ACTION_SPEC_TYPE,
        "targetPowerStatus": target_status,
    }
    if target_status == CLUSTER_POWERED_OFF:
        spec["powerOffReason"] = str(params.get("reason") or _DEFAULT_REASON)
    gate, answer = await vsan_write(
        connector,
        target,
        operator,
        method=VSAN_PERFORM_CLUSTER_POWER_ACTION,
        body={"cluster": cluster_ref(cluster), "spec": spec},
        params={"cluster": cluster, "target_power_status": target_status},
    )
    if gate is not None:
        return gate
    task = task_moref_value(answer)
    if task is None:
        return {
            **_job_answer(
                "not_confirmed",
                cluster,
                target_status,
                None,
                "vCenter took the call but sent no task id. Call "
                "vmware.vsan.cluster.power.precheck to see the power state and the task "
                "before you call again.",
                current_status=power["current_status"],
            ),
            "issues": [issue("connector", "error", "vCenter sent no task id for the action")],
        }
    return _job_answer(
        "accepted",
        cluster,
        target_status,
        task,
        "vCenter accepted the power action; it is not done yet. Read status_read "
        "(vmware.task.info with this task id) until the task ends. After a lost reply, call "
        "vmware.vsan.cluster.power.precheck first: it shows the power state and this task.",
        current_status=power["current_status"],
    )


async def vsan_cluster_shutdown_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Shut down the whole vSAN cluster (``PerformClusterPowerAction``, ``clusterPoweredOff``).

    Op-id: ``vmware.composite.vsan.cluster.shutdown``. Answers ``accepted``
    with the vCenter task id at once; ``already_running`` when the last power
    task still runs; ``unchanged`` when the cluster is already off. A vSAN
    fault on the action raises; the dispatcher answers with vCenter's fault.
    """
    return await _power_action(connector, target, operator, params, CLUSTER_POWERED_OFF)


async def vsan_cluster_start_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Start the whole vSAN cluster again (``PerformClusterPowerAction``, ``clusterPoweredOn``).

    Op-id: ``vmware.composite.vsan.cluster.start``. Same answers as the shut
    down.
    """
    return await _power_action(connector, target, operator, params, CLUSTER_POWERED_ON)


async def cluster_power_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """For the approver: the pre-check of the cluster now, and what else is affected.

    Without a connector (``preview_operation``) it shows only the plan. A
    failed read is shown as such; it does not block the approval request.
    """
    cluster = ctx.params.get("cluster")
    shutdown = ctx.descriptor.op_id == SHUTDOWN_OP_ID
    action = "Shut down" if shutdown else "Start"
    preview: dict[str, Any] = {
        "summary": f"{action} every host of vSAN cluster {cluster}.",
        "blast_radius": {
            "affected": (
                "Every VM and every host of the cluster stops. A vCenter that runs on this "
                "cluster stops too."
                if shutdown
                else "Every host of the cluster starts, then the VMs that vSAN stopped."
            ),
            "cannot_undo": (
                "Running workloads stop; only a start brings them back."
                if shutdown
                else "Nothing; a shut down stops the cluster again."
            ),
        },
    }
    if not shutdown or ctx.connector_instance is None or cluster_problem(cluster):
        return preview
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    try:
        check = await _precheck(connector, ctx.target, ctx.operator, str(cluster))
    except Exception as exc:
        preview["precheck_error"] = type(exc).__name__
        return preview
    preview["precheck"] = {
        "ready_to_shut_down": check["ready_to_shut_down"],
        "blocking": check["blocking"],
        "warnings": check["warnings"],
    }
    return preview

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.cronjob.list`` -- list the CronJobs of a namespace.

``k8s.cronjob.run`` needs the exact CronJob name. This safe read gives
the names. It uses ``BatchV1Api`` from ``kubernetes_asyncio`` and takes the
same selectors and paging as ``k8s.job.list``. Each row has the name,
the namespace, the schedule, whether the CronJob is suspended, and the
last time it created a Job (``null`` when it never ran).

References
----------
* https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/cron-job-v1/
* https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_listparams import (
    LIST_BASE_PROPERTIES,
    NAMESPACE_XOR_ALL_NAMESPACES,
)
from meho_backplane.connectors.kubernetes.ops_workload import (
    _list_kwargs,
    _next_continue_token,
)

if TYPE_CHECKING:
    from kubernetes_asyncio.client.models import V1CronJob

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "CRONJOB_LIST_OPS",
    "K8S_CRONJOB_LIST_LLM_INSTRUCTIONS",
    "K8S_CRONJOB_LIST_PARAMETER_SCHEMA",
    "OPS",
    "cronjob_row",
    "k8s_cronjob_list",
]


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def cronjob_row(cronjob: V1CronJob) -> dict[str, Any]:
    """Project a :class:`V1CronJob` into the ``k8s.cronjob.list`` row shape.

    ``suspended`` is ``False`` when the API leaves ``spec.suspend`` unset
    (that is the API default). ``last_schedule_time`` is ``None`` for a
    CronJob that has not created a Job yet.
    """
    metadata = cronjob.metadata
    spec = cronjob.spec
    status = cronjob.status
    return {
        "name": metadata.name if metadata is not None else None,
        "namespace": metadata.namespace if metadata is not None else None,
        "schedule": spec.schedule if spec is not None else None,
        "suspended": bool(spec.suspend) if spec is not None else False,
        "last_schedule_time": _iso_utc(status.last_schedule_time) if status is not None else None,
    }


async def k8s_cronjob_list(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.cronjob.list``. Same shape as ``k8s.job.list``."""
    namespace: str | None = params.get("namespace")
    api_client = await connector._get_api_client(target, operator)
    batch_v1 = client.BatchV1Api(api_client)
    kwargs = _list_kwargs(params)
    if bool(params.get("all_namespaces", False)):
        resp = await batch_v1.list_cron_job_for_all_namespaces(**kwargs)
    else:
        resp = await batch_v1.list_namespaced_cron_job(namespace=namespace or "", **kwargs)
    rows = [cronjob_row(c) for c in (resp.items or [])]
    result: dict[str, Any] = {"rows": rows, "total": len(rows)}
    token = _next_continue_token(resp)
    if token is not None:
        result["next_continue"] = token
    return result


K8S_CRONJOB_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": LIST_BASE_PROPERTIES,
    "oneOf": NAMESPACE_XOR_ALL_NAMESPACES,
    "additionalProperties": False,
    "examples": [
        {"namespace": "meho"},
        {"all_namespaces": True, "label_selector": "app=backup"},
    ],
}


K8S_CRONJOB_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to see which CronJobs exist in a namespace, on what schedule "
        "they run, and whether they are suspended: the kubectl-equivalent of "
        "'kubectl get cronjobs -n <ns>'. Use it to find the exact name that "
        "k8s.cronjob.run needs. Use all_namespaces=true for a cluster-wide view."
    ),
    "parameter_hints": {
        "namespace": "Required unless all_namespaces is true.",
        "all_namespaces": "Pass true for cluster-wide listings.",
        "label_selector": "k8s selector syntax (e.g. 'app=backup').",
        "limit": "Cap rows per response. Combine with continue_token.",
        "continue_token": "Pass the prior response's next_continue verbatim.",
    },
    "output_shape": (
        "{'rows': [{name, namespace, schedule, suspended, last_schedule_time}], "
        "'total': <int>, 'next_continue': <str | absent>}. ``schedule`` is the "
        "cron text. ``suspended`` is true when the CronJob creates no new Jobs. "
        "``last_schedule_time`` is null when it never ran."
    ),
    "next_step": (
        "Call k8s.cronjob.run with a CronJob's name to start its Job now, or "
        "k8s.job.list to see the Jobs it created."
    ),
    "pagination_hint": {
        "params": {
            "continue_token": "Cursor from the prior response's next_continue.",
            "limit": "Server-side page size (1..1000).",
        },
        "example_next_call": {
            "tool": "call_operation",
            "args": {
                "op_id": "k8s.cronjob.list",
                "params": {"namespace": "meho", "limit": 200},
            },
        },
    },
}


_CRONJOB_ROW_PROPERTIES: dict[str, Any] = {
    "name": {"type": ["string", "null"], "description": "CronJob name."},
    "namespace": {"type": ["string", "null"], "description": "Namespace of the CronJob."},
    "schedule": {"type": ["string", "null"], "description": "Cron schedule text."},
    "suspended": {
        "type": "boolean",
        "description": "True when the CronJob creates no new Jobs.",
    },
    "last_schedule_time": {
        "type": ["string", "null"],
        "description": "When the CronJob last created a Job (UTC, ISO 8601); null if never.",
    },
}

_K8S_CRONJOB_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _CRONJOB_ROW_PROPERTIES,
                "required": list(_CRONJOB_ROW_PROPERTIES),
                "additionalProperties": False,
            },
            "description": "One row per CronJob.",
        },
        "total": {"type": "integer", "description": "Number of rows in this page."},
        "next_continue": {
            "type": "string",
            "description": "Pagination cursor; present only when the server has more pages.",
        },
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}


CRONJOB_LIST_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.cronjob.list",
        handler_attr="k8s_cronjob_list",
        summary="List CronJobs with schedule, suspended flag and last schedule time.",
        description=(
            "Calls BatchV1Api.list_namespaced_cron_job (or list_cron_job_for_all_namespaces) "
            "and projects each CronJob to {name, namespace, schedule, suspended, "
            "last_schedule_time}. Same selectors and server-side paging as k8s.job.list."
        ),
        parameter_schema=K8S_CRONJOB_LIST_PARAMETER_SCHEMA,
        response_schema=_K8S_CRONJOB_LIST_RESPONSE_SCHEMA,
        group_key="workload",
        tags=("read-only", "cronjob", "workload"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_CRONJOB_LIST_LLM_INSTRUCTIONS,
    ),
)

#: Collected by :func:`~meho_backplane.connectors.kubernetes.ops.collect_ops`.
OPS: tuple[KubernetesOp, ...] = CRONJOB_LIST_OPS

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Job read ops -- ``k8s.job.list`` + ``k8s.job.info``.

A Job runs one task to completion: a database migration, a backup, a
one-off data fix. MEHO could create a Job (``k8s.job.create``) and delete
one (``k8s.delete``) but could not say whether a Job still runs, finished
or failed. Both ops here are safe reads in the ``workload`` group and use
``BatchV1Api`` from ``kubernetes_asyncio``.

* ``k8s.job.list`` -- the ``k8s.deployment.list`` parameters; one row per
  Job with a one-word ``state``, pod counts, ``completions`` and the
  start / completion times.
* ``k8s.job.info`` -- one Job by exact name or unique prefix, plus its
  conditions, retry limits, template containers, pod selector and pods
  (newest first, at most :data:`JOB_INFO_MAX_PODS`).

``state`` is ``failed`` when the ``Failed`` condition is true, ``complete``
when ``Complete`` is true, ``suspended`` when ``Suspended`` is true, else
``running``. Prefix resolution and the list-param translation are shared
with :mod:`ops_workload`; the JSONFlux reducer owns set-shaped truncation.

References
----------
* https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/job-v1/
* https://kubernetes.io/docs/concepts/workloads/controllers/job/
* https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/docs/BatchV1Api.md
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from kubernetes_asyncio import client

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_core import age_seconds
from meho_backplane.connectors.kubernetes.ops_listparams import (
    LIST_BASE_PROPERTIES,
    NAMESPACE_XOR_ALL_NAMESPACES,
)
from meho_backplane.connectors.kubernetes.ops_workload import (
    _list_kwargs,
    _next_continue_token,
    _resolve_from_items,
    pod_row,
)

if TYPE_CHECKING:
    from kubernetes_asyncio.client.models import V1Job, V1JobCondition, V1LabelSelector, V1Pod

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "JOB_INFO_MAX_PODS",
    "JOB_NAME_LABEL",
    "JOB_OPS",
    "K8S_JOB_INFO_LLM_INSTRUCTIONS",
    "K8S_JOB_INFO_PARAMETER_SCHEMA",
    "K8S_JOB_LIST_LLM_INSTRUCTIONS",
    "K8S_JOB_LIST_PARAMETER_SCHEMA",
    "job_info",
    "job_pod_selector",
    "job_row",
    "job_state",
    "k8s_job_info",
    "k8s_job_list",
    "label_selector_string",
    "resolve_job_name",
]

#: Most pods ``k8s.job.info`` returns inline. A Job with many retries or
#: a high ``parallelism`` can own hundreds of pods; the cap keeps the
#: info result small. ``k8s.pod.list`` with the Job's ``pod_selector``
#: returns all of them.
JOB_INFO_MAX_PODS = 50

#: Label the Job controller puts on every pod of a Job. Used as the pod
#: selector when a Job carries no ``spec.selector`` of its own.
JOB_NAME_LABEL = "batch.kubernetes.io/job-name"


# ---------------------------------------------------------------------------
# Pure row-shape helpers
# ---------------------------------------------------------------------------


def _condition_is_true(job: V1Job, condition_type: str) -> bool:
    status = job.status
    if status is None or not status.conditions:
        return False
    return any(
        cond.type == condition_type and str(cond.status).lower() == "true"
        for cond in status.conditions
    )


def job_state(job: V1Job) -> str:
    """Return one word for the Job's state.

    ``failed`` wins over ``complete`` so a Job that failed after a
    partial success reads as failed. A suspended Job keeps no other
    terminal condition, so the order of the two checks after it does
    not matter in practice.
    """
    if _condition_is_true(job, "Failed"):
        return "failed"
    if _condition_is_true(job, "Complete"):
        return "complete"
    if _condition_is_true(job, "Suspended"):
        return "suspended"
    return "running"


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def job_row(job: V1Job, *, now: datetime | None = None) -> dict[str, Any]:
    """Project a :class:`V1Job` into the ``k8s.job.list`` row shape.

    The pod counts come from ``status`` (``active`` / ``succeeded`` /
    ``failed``); ``completions`` is the number of successes the Job
    needs, from ``spec`` (``None`` means any one success completes the
    Job). ``completion_time`` is set only when the Job succeeded.
    """
    metadata = job.metadata
    spec = job.spec
    status = job.status
    return {
        "name": metadata.name if metadata is not None else None,
        "namespace": metadata.namespace if metadata is not None else None,
        "age_seconds": age_seconds(
            metadata.creation_timestamp if metadata is not None else None,
            now=now,
        ),
        "state": job_state(job),
        "pods_active": int(status.active or 0) if status is not None else 0,
        "pods_succeeded": int(status.succeeded or 0) if status is not None else 0,
        "pods_failed": int(status.failed or 0) if status is not None else 0,
        "completions": int(spec.completions)
        if spec is not None and spec.completions is not None
        else None,
        "start_time": _iso_utc(status.start_time) if status is not None else None,
        "completion_time": _iso_utc(status.completion_time) if status is not None else None,
    }


def label_selector_string(selector: V1LabelSelector | None) -> str | None:
    """Render a :class:`V1LabelSelector` as the selector string the API accepts.

    ``matchLabels`` become ``key=value`` terms; ``matchExpressions``
    become ``key in (a,b)`` / ``key notin (a,b)`` / ``key`` / ``!key``.
    Returns ``None`` for an absent or empty selector.
    """
    if selector is None:
        return None
    terms = [f"{key}={value}" for key, value in sorted((selector.match_labels or {}).items())]
    for expr in selector.match_expressions or []:
        operator = str(expr.operator)
        values = ",".join(expr.values or [])
        if operator == "In":
            terms.append(f"{expr.key} in ({values})")
        elif operator == "NotIn":
            terms.append(f"{expr.key} notin ({values})")
        elif operator == "Exists":
            terms.append(str(expr.key))
        elif operator == "DoesNotExist":
            terms.append(f"!{expr.key}")
    return ",".join(terms) or None


def _job_condition_row(cond: V1JobCondition) -> dict[str, Any]:
    return {
        "type": cond.type,
        "status": cond.status,
        "reason": cond.reason,
        "message": cond.message,
        "last_transition_time": _iso_utc(cond.last_transition_time),
    }


def _template_container_rows(job: V1Job) -> list[dict[str, Any]]:
    spec = job.spec
    if spec is None or spec.template is None or spec.template.spec is None:
        return []
    return [{"name": c.name, "image": c.image} for c in (spec.template.spec.containers or [])]


def _pod_created(pod: V1Pod) -> datetime:
    created = pod.metadata.creation_timestamp if pod.metadata is not None else None
    return created if created is not None else datetime.min.replace(tzinfo=UTC)


def job_pod_selector(job: V1Job) -> str | None:
    """Return the label selector that finds the Job's pods.

    The API server sets ``spec.selector`` on every Job it stores. When
    it is missing (an in-memory object, or a manual selector left
    empty) the Job-name label every Job pod carries is the fallback.
    """
    spec = job.spec
    selector = label_selector_string(spec.selector) if spec is not None else None
    if selector is not None:
        return selector
    name = job.metadata.name if job.metadata is not None else None
    return f"{JOB_NAME_LABEL}={name}" if name else None


def job_info(job: V1Job, pods: list[V1Pod], *, now: datetime | None = None) -> dict[str, Any]:
    """Project a :class:`V1Job` plus its pods into the ``k8s.job.info`` shape.

    The row fields of :func:`job_row` come first, then the conditions
    (``type`` / ``status`` / ``reason`` / ``message``), the retry limits,
    the pod-template containers, the Job's pod selector, and the pods
    in the ``k8s.pod.list`` row shape. Pods are ordered newest first and
    cut at :data:`JOB_INFO_MAX_PODS`; ``pods_truncated`` says when the
    cut happened.
    """
    spec = job.spec
    status = job.status
    ordered = sorted(pods, key=_pod_created, reverse=True)
    kept = ordered[:JOB_INFO_MAX_PODS]
    return {
        **job_row(job, now=now),
        "conditions": [_job_condition_row(c) for c in (status.conditions or [])]
        if status is not None
        else [],
        "backoff_limit": spec.backoff_limit if spec is not None else None,
        "active_deadline_seconds": spec.active_deadline_seconds if spec is not None else None,
        "containers": _template_container_rows(job),
        "pod_selector": job_pod_selector(job),
        "pods": [pod_row(p, now=now) for p in kept],
        "pods_truncated": len(ordered) > len(kept),
    }


# ---------------------------------------------------------------------------
# Name resolution + handlers
# ---------------------------------------------------------------------------


async def resolve_job_name(batch_v1: client.BatchV1Api, namespace: str, job_name: str) -> V1Job:
    """Resolve a Job name + namespace pair to a :class:`V1Job`.

    Exact match wins; otherwise ``job_name`` is a prefix that must match
    one Job in the namespace. Raises
    :class:`~meho_backplane.connectors.kubernetes.ops_workload.WorkloadNotFoundError`
    / :class:`~meho_backplane.connectors.kubernetes.ops_workload.AmbiguousPrefixError`
    like the pod and deployment resolvers.
    """
    job_list = await batch_v1.list_namespaced_job(namespace=namespace)
    return cast("V1Job", _resolve_from_items(list(job_list.items or []), job_name, "job"))


async def k8s_job_list(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.job.list``. Same shape as ``k8s.deployment.list``."""
    namespace: str | None = params.get("namespace")
    all_namespaces = bool(params.get("all_namespaces", False))
    api_client = await connector._get_api_client(target, operator)
    batch_v1 = client.BatchV1Api(api_client)
    kwargs = _list_kwargs(params)
    if all_namespaces:
        resp = await batch_v1.list_job_for_all_namespaces(**kwargs)
    else:
        resp = await batch_v1.list_namespaced_job(namespace=namespace or "", **kwargs)
    rows = [job_row(j) for j in (resp.items or [])]
    result: dict[str, Any] = {"rows": rows, "total": len(rows)}
    token = _next_continue_token(resp)
    if token is not None:
        result["next_continue"] = token
    return result


async def k8s_job_info(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.job.info``.

    Resolves the Job, then lists its pods with the Job's own selector
    (one ``CoreV1Api.list_namespaced_pod`` call). The pod list is not
    paginated server-side: :func:`job_info` cuts it to
    :data:`JOB_INFO_MAX_PODS` newest pods.
    """
    job_name: str = params["job_name"]
    namespace: str = params["namespace"]
    api_client = await connector._get_api_client(target, operator)
    batch_v1 = client.BatchV1Api(api_client)
    job = await resolve_job_name(batch_v1, namespace, job_name)
    selector = job_pod_selector(job)
    pods: list[V1Pod] = []
    if selector is not None:
        v1 = client.CoreV1Api(api_client)
        pod_list = await v1.list_namespaced_pod(namespace=namespace, label_selector=selector)
        pods = list(pod_list.items or [])
    return job_info(job, pods)


# ---------------------------------------------------------------------------
# Parameter schemas, LLM instructions, response schemas
# ---------------------------------------------------------------------------


K8S_JOB_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": LIST_BASE_PROPERTIES,
    "oneOf": NAMESPACE_XOR_ALL_NAMESPACES,
    "additionalProperties": False,
    "examples": [
        {"namespace": "meho"},
        {"all_namespaces": True, "label_selector": "app=backup"},
    ],
}


K8S_JOB_INFO_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Job name. An exact match wins; otherwise a prefix that "
                "matches exactly one Job in the namespace. An ambiguous "
                "prefix returns an error that lists the candidates."
            ),
        },
        "namespace": {
            "type": "string",
            "minLength": 1,
            "description": "Namespace the Job lives in.",
        },
    },
    "required": ["job_name", "namespace"],
    "additionalProperties": False,
    "examples": [{"job_name": "meho-migrate", "namespace": "meho"}],
}


K8S_JOB_LIST_PAGINATION_HINT: dict[str, Any] = {
    "params": {
        "continue_token": (
            "Server-emitted pagination cursor from the prior response's "
            "``next_continue`` field. Pass back unchanged to fetch the "
            "next page; stale tokens return 410."
        ),
        "label_selector": "Standard k8s label selector. Forwarded server-side.",
        "field_selector": "Standard k8s field selector. Forwarded server-side.",
        "namespace": "Narrow to one namespace.",
        "limit": "Server-side page size (1..1000).",
    },
    "example_next_call": {
        "tool": "call_operation",
        "args": {
            "op_id": "k8s.job.list",
            "params": {"namespace": "meho", "label_selector": "app=backup"},
        },
    },
}


K8S_JOB_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to see which Jobs exist in a namespace and whether each one "
        "still runs, finished or failed: the kubectl-equivalent of "
        "'kubectl get jobs -n <ns>'. Use all_namespaces=true for a "
        "cluster-wide view. Combine limit + continue_token to page."
    ),
    "parameter_hints": {
        "namespace": "Required unless all_namespaces is true.",
        "all_namespaces": "Pass true for cluster-wide listings.",
        "label_selector": "k8s selector syntax (e.g. 'app=backup').",
        "limit": "Cap rows per response. Combine with continue_token.",
        "continue_token": "Pass the prior response's next_continue verbatim.",
    },
    "output_shape": (
        "{'rows': [{name, namespace, age_seconds, state, pods_active, "
        "pods_succeeded, pods_failed, completions, start_time, "
        "completion_time}], 'total': <int>, 'next_continue': <str | absent>}. "
        "``state`` is one of running / complete / failed / suspended. "
        "``completions`` is how many successes the Job needs (null = any one). "
        "``completion_time`` is set only when the Job succeeded."
    ),
    "next_step": (
        "Call k8s.job.info with the Job's name for its conditions (why it "
        "failed), retry limits and pods. Then k8s.pod.info for one pod's "
        "detail and k8s.logs for its log."
    ),
    "pagination_hint": K8S_JOB_LIST_PAGINATION_HINT,
}


K8S_JOB_INFO_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call when the operator names one Job and asks 'did it finish, "
        "did it work, and why not?': a database migration Job, a backup, "
        "a one-off batch task. Accepts a prefix as long as it matches one "
        "Job in the namespace."
    ),
    "parameter_hints": {
        "job_name": "Exact name or unique prefix within the namespace.",
        "namespace": "Required.",
    },
    "output_shape": (
        "Flat dict: {name, namespace, age_seconds, state, pods_active, "
        "pods_succeeded, pods_failed, completions, start_time, "
        "completion_time, conditions: [{type, status, reason, message, "
        "last_transition_time}], backoff_limit, active_deadline_seconds, "
        "containers: [{name, image}], pod_selector, pods: [{name, namespace, "
        "status, ready, restarts, age_seconds, node, ip}], pods_truncated}. "
        "A failed Job has a 'Failed' condition whose reason says why "
        "(BackoffLimitExceeded = too many retries, DeadlineExceeded = ran "
        "out of time). ``pods`` holds at most 50 pods, newest first; "
        "``pods_truncated`` is true when the list was cut."
    ),
    "next_step": (
        "For a failed or stuck Job, call k8s.pod.info on the newest pod to "
        "see the container reason (ImagePullBackOff, OOMKilled, ...) and "
        "k8s.logs for its output. When pods_truncated is true, call "
        "k8s.pod.list with label_selector=<pod_selector> for all pods."
    ),
    "result_scalars": {
        "keys": [
            "name",
            "namespace",
            "state",
            "pods_active",
            "pods_succeeded",
            "pods_failed",
            "completions",
            "start_time",
            "completion_time",
            "backoff_limit",
            "active_deadline_seconds",
            "pod_selector",
            "pods_truncated",
        ]
    },
}


def _nullable_str(description: str) -> dict[str, Any]:
    return {"type": ["string", "null"], "description": description}


_JOB_ROW_PROPERTIES: dict[str, Any] = {
    "name": _nullable_str("Job name."),
    "namespace": _nullable_str("Namespace of the Job."),
    "age_seconds": {
        "type": ["integer", "null"],
        "description": "Seconds since the Job was created.",
    },
    "state": {
        "type": "string",
        "enum": ["running", "complete", "failed", "suspended"],
        "description": "One word from the Job's conditions.",
    },
    "pods_active": {"type": "integer", "description": "Pods still pending or running."},
    "pods_succeeded": {"type": "integer", "description": "Pods that finished with success."},
    "pods_failed": {"type": "integer", "description": "Pods that failed."},
    "completions": {
        "type": ["integer", "null"],
        "description": "Successes the Job needs; null means any one success completes it.",
    },
    "start_time": _nullable_str("When the Job controller started the Job (UTC, ISO 8601)."),
    "completion_time": _nullable_str("When the Job succeeded (UTC, ISO 8601); null otherwise."),
}

_JOB_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _JOB_ROW_PROPERTIES,
    "required": list(_JOB_ROW_PROPERTIES),
    "additionalProperties": False,
}

_K8S_JOB_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {"type": "array", "items": _JOB_ROW_SCHEMA, "description": "One row per Job."},
        "total": {"type": "integer", "description": "Number of rows in this page."},
        "next_continue": {
            "type": "string",
            "description": "Pagination cursor; present only when the server has more pages.",
        },
    },
    "required": ["rows", "total"],
    "additionalProperties": False,
}

_K8S_JOB_INFO_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_JOB_ROW_PROPERTIES,
        "conditions": {
            "type": "array",
            "description": "Job conditions: type, status, reason, message, last_transition_time.",
            "items": {
                "type": "object",
                "properties": {
                    "type": _nullable_str("Condition type."),
                    "status": _nullable_str("True / False / Unknown."),
                    "reason": _nullable_str("Short reason code."),
                    "message": _nullable_str("Human-readable detail."),
                    "last_transition_time": _nullable_str("Last status change (UTC, ISO 8601)."),
                },
            },
        },
        "backoff_limit": {
            "type": ["integer", "null"],
            "description": "Retries allowed before the Job is marked failed.",
        },
        "active_deadline_seconds": {
            "type": ["integer", "null"],
            "description": "Seconds the Job may run before it is stopped; null = no limit.",
        },
        "containers": {
            "type": "array",
            "description": "Containers of the pod template: name and image.",
            "items": {
                "type": "object",
                "properties": {
                    "name": _nullable_str("Container name."),
                    "image": _nullable_str("Container image."),
                },
            },
        },
        "pod_selector": _nullable_str("Label selector that finds the Job's pods."),
        "pods": {
            "type": "array",
            "description": "The Job's pods in the k8s.pod.list row shape, newest first.",
            "items": {"type": "object", "additionalProperties": True},
        },
        "pods_truncated": {
            "type": "boolean",
            "description": "True when more pods exist than the inline cap returns.",
        },
    },
    "required": [*_JOB_ROW_PROPERTIES, "conditions", "containers", "pods", "pods_truncated"],
    "additionalProperties": False,
}


JOB_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.job.list",
        handler_attr="k8s_job_list",
        summary="List Jobs with a one-word state, pod counts and start / completion times.",
        description=(
            "Calls BatchV1Api.list_namespaced_job (or list_job_for_all_namespaces) "
            "and projects each Job to {name, namespace, age_seconds, state, "
            "pods_active, pods_succeeded, pods_failed, completions, start_time, "
            "completion_time}. Same selectors and server-side paging as k8s.pod.list."
        ),
        parameter_schema=K8S_JOB_LIST_PARAMETER_SCHEMA,
        response_schema=_K8S_JOB_LIST_RESPONSE_SCHEMA,
        group_key="workload",
        tags=("read-only", "job", "workload"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_JOB_LIST_LLM_INSTRUCTIONS,
    ),
    KubernetesOp(
        op_id="k8s.job.info",
        handler_attr="k8s_job_info",
        summary="One Job's state, conditions, retry limits and pods; exact name or unique prefix.",
        description=(
            "Resolves job_name in the namespace (exact match wins, else unique prefix) "
            "and returns the Job's state, pod counts, conditions with reason and "
            "message, backoff_limit, active_deadline_seconds, template containers, "
            "pod_selector and up to 50 of its pods, newest first."
        ),
        parameter_schema=K8S_JOB_INFO_PARAMETER_SCHEMA,
        response_schema=_K8S_JOB_INFO_RESPONSE_SCHEMA,
        group_key="workload",
        tags=("read-only", "job", "workload"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_JOB_INFO_LLM_INSTRUCTIONS,
    ),
)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.workload.list`` -- Deployments, StatefulSets or DaemonSets with their size.

One read for one workload kind. Each row has what a migration plan needs to
size the workload: desired replicas, per-container CPU and memory requests
and limits, images, volumes (with the claim name of a persistent volume
claim), the claim templates of a StatefulSet, and the label selector that
finds the workload's pods (pass it to ``k8s.pod.list`` to see which nodes
they run on).

Quantities stay as the API writes them (``"250m"``, ``"512Mi"``); the
reader converts them. Same namespace selectors and server-side paging as
``k8s.deployment.list``.

References
----------

* https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/stateful-set-v1/
* https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/daemon-set-v1/
* https://github.com/tomplus/kubernetes_asyncio/blob/master/kubernetes_asyncio/docs/AppsV1Api.md
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_job import label_selector_string
from meho_backplane.connectors.kubernetes.ops_listparams import (
    LIST_BASE_PROPERTIES,
    NAMESPACE_XOR_ALL_NAMESPACES,
)
from meho_backplane.connectors.kubernetes.ops_workload import (
    _list_kwargs,
    _next_continue_token,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "WORKLOAD_KINDS",
    "WORKLOAD_SET_OPS",
    "k8s_workload_list",
    "workload_set_row",
]

#: Workload kinds the op reads, with the AppsV1Api list methods for one
#: namespace and for all namespaces.
WORKLOAD_KINDS: dict[str, tuple[str, str]] = {
    "Deployment": ("list_namespaced_deployment", "list_deployment_for_all_namespaces"),
    "StatefulSet": ("list_namespaced_stateful_set", "list_stateful_set_for_all_namespaces"),
    "DaemonSet": ("list_namespaced_daemon_set", "list_daemon_set_for_all_namespaces"),
}

_VOLUME_SOURCES: tuple[str, ...] = (
    "persistent_volume_claim",
    "empty_dir",
    "host_path",
    "config_map",
    "secret",
    "projected",
    "downward_api",
    "csi",
    "ephemeral",
    "nfs",
)


def _resources(container: Any) -> tuple[dict[str, str], dict[str, str]]:
    resources = container.resources
    if resources is None:
        return {}, {}
    requests = {k: str(v) for k, v in (resources.requests or {}).items()}
    limits = {k: str(v) for k, v in (resources.limits or {}).items()}
    return requests, limits


def _container_rows(pod_spec: Any) -> list[dict[str, Any]]:
    rows = []
    for container in (pod_spec.containers or []) if pod_spec is not None else []:
        requests, limits = _resources(container)
        rows.append(
            {
                "name": container.name,
                "image": container.image,
                "requests": requests,
                "limits": limits,
            }
        )
    return rows


def _volume_row(volume: Any) -> dict[str, Any]:
    source = next((s for s in _VOLUME_SOURCES if getattr(volume, s, None) is not None), None)
    claim = volume.persistent_volume_claim
    return {
        "name": volume.name,
        "type": _camel(source) if source is not None else "other",
        "claim_name": claim.claim_name if claim is not None else None,
    }


def _camel(snake: str) -> str:
    head, *rest = snake.split("_")
    return head + "".join(part.title() for part in rest)


def _claim_template_row(template: Any) -> dict[str, Any]:
    spec = template.spec
    resources = spec.resources if spec is not None else None
    requests = (resources.requests or {}) if resources is not None else {}
    storage = requests.get("storage")
    return {
        "name": template.metadata.name if template.metadata is not None else None,
        "storage_request": str(storage) if storage is not None else None,
        "storage_class": spec.storage_class_name if spec is not None else None,
    }


def _desired_replicas(kind: str, obj: Any) -> int | None:
    if kind == "DaemonSet":
        status = obj.status
        value = status.desired_number_scheduled if status is not None else None
    else:
        value = obj.spec.replicas if obj.spec is not None else None
    return int(value) if value is not None else None


def workload_set_row(kind: str, obj: Any) -> dict[str, Any]:
    """Project a Deployment, StatefulSet or DaemonSet into the ``k8s.workload.list`` row."""
    metadata = obj.metadata
    spec = obj.spec
    template = spec.template if spec is not None else None
    pod_spec = template.spec if template is not None else None
    templates = getattr(spec, "volume_claim_templates", None) if spec is not None else None
    return {
        "kind": kind,
        "name": metadata.name if metadata is not None else None,
        "namespace": metadata.namespace if metadata is not None else None,
        "replicas": _desired_replicas(kind, obj),
        "containers": _container_rows(pod_spec),
        "volumes": [_volume_row(v) for v in (pod_spec.volumes or [])] if pod_spec else [],
        "volume_claim_templates": [_claim_template_row(t) for t in (templates or [])],
        "pod_selector": label_selector_string(spec.selector) if spec is not None else None,
    }


async def k8s_workload_list(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.workload.list``."""
    kind: str = params["kind"]
    namespaced, cluster_wide = WORKLOAD_KINDS[kind]
    api_client = await connector._get_api_client(target, operator)
    apps_v1 = client.AppsV1Api(api_client)
    kwargs = _list_kwargs(params)
    if params.get("all_namespaces", False):
        resp = await getattr(apps_v1, cluster_wide)(**kwargs)
    else:
        resp = await getattr(apps_v1, namespaced)(namespace=params.get("namespace") or "", **kwargs)
    rows = [workload_set_row(kind, item) for item in (resp.items or [])]
    result: dict[str, Any] = {"kind": kind, "rows": rows, "total": len(rows)}
    token = _next_continue_token(resp)
    if token is not None:
        result["next_continue"] = token
    return result


_QUANTITIES: dict[str, Any] = {
    "type": "object",
    "additionalProperties": {"type": "string"},
    "description": "Resource name to quantity as the API writes it, e.g. {'cpu': '250m'}.",
}

_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "description": "Deployment, StatefulSet or DaemonSet."},
        "name": {"type": ["string", "null"], "description": "Workload name."},
        "namespace": {"type": ["string", "null"], "description": "Namespace."},
        "replicas": {
            "type": ["integer", "null"],
            "description": "Desired pods (for a DaemonSet: pods it should run now).",
        },
        "containers": {
            "type": "array",
            "description": "Containers of one pod: name, image, requests, limits.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "image": {"type": ["string", "null"]},
                    "requests": _QUANTITIES,
                    "limits": _QUANTITIES,
                },
            },
        },
        "volumes": {
            "type": "array",
            "description": "Pod volumes: name, type and the claim name of a PVC volume.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "type": {"type": "string"},
                    "claim_name": {"type": ["string", "null"]},
                },
            },
        },
        "volume_claim_templates": {
            "type": "array",
            "description": "StatefulSet claim templates: name, storage request, storage class.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "storage_request": {"type": ["string", "null"]},
                    "storage_class": {"type": ["string", "null"]},
                },
            },
        },
        "pod_selector": {
            "type": ["string", "null"],
            "description": "Label selector that finds the workload's pods.",
        },
    },
}

K8S_WORKLOAD_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": list(WORKLOAD_KINDS),
            "description": "Which workloads to list: Deployment, StatefulSet or DaemonSet.",
        },
        **LIST_BASE_PROPERTIES,
    },
    "required": ["kind"],
    "oneOf": NAMESPACE_XOR_ALL_NAMESPACES,
    "additionalProperties": False,
    "examples": [
        {"kind": "StatefulSet", "all_namespaces": True},
        {"kind": "Deployment", "namespace": "shop", "limit": 200},
    ],
}

K8S_WORKLOAD_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "description": "The kind that was listed."},
        "rows": {"type": "array", "items": _ROW_SCHEMA, "description": "One row per workload."},
        "total": {"type": "integer", "description": "Number of rows in this page."},
        "next_continue": {
            "type": "string",
            "description": "Pagination cursor; present only when the server has more pages.",
        },
    },
    "required": ["kind", "rows", "total"],
    "additionalProperties": False,
}

K8S_WORKLOAD_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to size the workloads of a cluster: how many replicas, how much "
        "CPU and memory each pod asks for, which images and volumes it uses. "
        "Works for Deployments, StatefulSets and DaemonSets."
    ),
    "parameter_hints": {
        "kind": "Required. Deployment, StatefulSet or DaemonSet.",
        "namespace": "Required unless all_namespaces is true.",
        "all_namespaces": "Pass true for the whole cluster.",
        "limit": "Rows per page. Combine with continue_token.",
        "continue_token": "Pass the prior response's next_continue verbatim.",
    },
    "output_shape": (
        "{'kind', 'rows': [{kind, name, namespace, replicas, containers: [{name, "
        "image, requests, limits}], volumes: [{name, type, claim_name}], "
        "volume_claim_templates: [{name, storage_request, storage_class}], "
        "pod_selector}], 'total', 'next_continue'?}. Quantities are API text "
        "('250m', '512Mi')."
    ),
    "next_step": (
        "Call k8s.pod.list with label_selector=<pod_selector> to see the nodes "
        "the pods run on, or k8s.migration.inventory for the whole cluster in "
        "the common migration shape."
    ),
    "pagination_hint": {
        "params": {
            "continue_token": "Cursor from the prior response's next_continue.",
            "limit": "Server-side page size (1..1000).",
        },
        "example_next_call": {
            "tool": "call_operation",
            "args": {
                "op_id": "k8s.workload.list",
                "params": {"kind": "Deployment", "all_namespaces": True, "limit": 200},
            },
        },
    },
}


WORKLOAD_SET_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.workload.list",
        handler_attr="k8s_workload_list",
        summary="List Deployments, StatefulSets or DaemonSets with replicas, resources, volumes.",
        description=(
            "Calls AppsV1Api for one workload kind and returns per workload: desired "
            "replicas, per-container CPU and memory requests and limits, images, "
            "volumes with claim names, StatefulSet claim templates and the pod "
            "label selector. Same selectors and paging as k8s.deployment.list."
        ),
        parameter_schema=K8S_WORKLOAD_LIST_PARAMETER_SCHEMA,
        response_schema=K8S_WORKLOAD_LIST_RESPONSE_SCHEMA,
        group_key="workload",
        tags=("read-only", "workload", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_WORKLOAD_LIST_LLM_INSTRUCTIONS,
    ),
)

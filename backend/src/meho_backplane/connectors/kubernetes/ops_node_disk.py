# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.node.disk`` and ``k8s.node.images`` -- a node's disk state and its images.

Two read-only ops. They answer "is the node's disk full, and which container
images fill it?" and change nothing.

* ``k8s.node.disk``: per node, the file system capacity, used and free bytes
  of the node and of the image store, and the conditions ``DiskPressure``,
  ``MemoryPressure`` and ``PIDPressure`` with their last change time. The
  sizes come from the kubelet summary
  (``GET /api/v1/nodes/<node>/proxy/stats/summary``), the conditions from the
  node status.
* ``k8s.node.images``: per node, the images of the node status (names, size in
  bytes, biggest first), a flag "used by a running pod" and a total. The flag
  comes from the pods that run on the node.

Limits the answer states instead of hiding:

* The kubelet lists at most 50 images in the node status by default
  (``--node-status-max-images``). A node with exactly 50 images carries
  ``list_may_be_cut: true``.
* A read that failed (no right, kubelet not reachable, server error) is
  ``unknown`` with a short ``error``. It never looks like "no data", an empty
  list or "not used". A 401 is raised, so the dispatcher can renew the login.
* A pod counts as using an image when it is not ``Succeeded`` or ``Failed``
  and one of its containers (also init and debug containers) runs that image,
  by name or by digest.
* A total size is ``null`` when one of the images it adds up has no size: a
  sum without it would show less disk use than there is.

References
----------
* Kubelet summary API (``node.fs``, ``node.runtime.imageFs``,
  ``node.runtime.containerFs``):
  https://kubernetes.io/docs/reference/instrumentation/node-metrics/
* Node status ``images`` (``names``, ``sizeBytes``) and ``conditions``:
  https://kubernetes.io/docs/reference/kubernetes-api/cluster-resources/node-v1/
* Node pressure conditions:
  https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

from meho_backplane.connectors.kubernetes.ops import KubernetesOp
from meho_backplane.connectors.kubernetes.ops_node_disk_schemas import (
    K8S_NODE_DISK_LLM_INSTRUCTIONS,
    K8S_NODE_DISK_PARAMETER_SCHEMA,
    K8S_NODE_DISK_RESPONSE_SCHEMA,
    K8S_NODE_IMAGES_LLM_INSTRUCTIONS,
    K8S_NODE_IMAGES_PARAMETER_SCHEMA,
    K8S_NODE_IMAGES_RESPONSE_SCHEMA,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike

__all__ = [
    "NODE_DISK_OPS",
    "OPS",
    "k8s_node_disk",
    "k8s_node_images",
]

_PRESENT = "present"
_NOT_PRESENT = "not present"
_UNKNOWN = "unknown"

_PRESSURE_CONDITIONS = ("DiskPressure", "MemoryPressure", "PIDPressure")

#: The kubelet's default for the images it lists in the node status.
_KUBELET_DEFAULT_MAX_IMAGES = 50

#: Objects per page and the most pages read per list. A reached cap sets
#: ``incomplete``. At most this many nodes are read when no node is named.
_PAGE_SIZE = 200
_MAX_PAGES = 10
_MAX_NODES = 50

_SUMMARY_PATH = "/api/v1/nodes/{name}/proxy/stats/summary"
_SUMMARY_RESPONSE_TYPES = {200: "object", 401: None}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _error(exc: Exception) -> str:
    if isinstance(exc, ApiException):
        return f"HTTP {exc.status}"
    return type(exc).__name__


async def _list_all(call: Callable[..., Awaitable[Any]], **kwargs: Any) -> tuple[list[Any], bool]:
    """Follow every page of a list call, up to the cap. Returns (items, cap reached)."""
    rows: list[Any] = []
    token: str | None = None
    for _page in range(_MAX_PAGES):
        page_kwargs: dict[str, Any] = {**kwargs, "limit": _PAGE_SIZE}
        if token:
            page_kwargs["_continue"] = token
        resp = await call(**page_kwargs)
        rows.extend(resp.items or [])
        token = resp.metadata._continue if resp.metadata else None
        if not token:
            return rows, False
    return rows, True


async def _read_nodes(
    api_client: client.ApiClient, node: str | None
) -> tuple[list[Any], bool, bool]:
    """Return ``(nodes, incomplete, not_found)`` for one named node or every node."""
    core = client.CoreV1Api(api_client)
    if node is not None:
        try:
            return [await core.read_node(name=node)], False, False
        except ApiException as exc:
            if exc.status == 404:
                return [], False, True
            raise
    nodes, incomplete = await _list_all(core.list_node)
    if len(nodes) > _MAX_NODES:
        return nodes[:_MAX_NODES], True, False
    return nodes, incomplete, False


def _node_name(node: Any) -> str | None:
    return node.metadata.name if node.metadata else None


# ---------------------------------------------------------------------------
# k8s.node.disk
# ---------------------------------------------------------------------------


def _condition_rows(node: Any) -> list[dict[str, Any]]:
    conditions = (node.status.conditions if node.status else None) or []
    return [
        {
            "type": c.type,
            "status": c.status,
            "reason": c.reason,
            "last_transition_time": _iso(c.last_transition_time),
        }
        for c in conditions
        if c.type in _PRESSURE_CONDITIONS
    ]


def _fs_stats(raw: Any) -> dict[str, Any] | None:
    """Capacity, used and free bytes of one file system of the kubelet summary."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key, source in (
        ("capacity_bytes", "capacityBytes"),
        ("used_bytes", "usedBytes"),
        ("available_bytes", "availableBytes"),
    ):
        value = raw.get(source)
        out[key] = value if isinstance(value, int) and not isinstance(value, bool) else None
    capacity, used = out["capacity_bytes"], out["used_bytes"]
    out["used_percent"] = round(100 * used / capacity, 1) if capacity and used is not None else None
    return out


async def _kubelet_summary(api_client: client.ApiClient, name: str) -> dict[str, Any]:
    """Read the kubelet summary through the API server. A 401 is raised."""
    try:
        body = await api_client.call_api(
            _SUMMARY_PATH,
            "GET",
            path_params={"name": name},
            query_params=[],
            header_params={"Accept": "application/json"},
            response_types_map=_SUMMARY_RESPONSE_TYPES,
            auth_settings=["BearerToken"],
            _return_http_data_only=True,
        )
    except ApiException as exc:
        if exc.status == 401:
            raise
        return {"state": _UNKNOWN, "error": _error(exc)}
    except Exception as exc:
        return {"state": _UNKNOWN, "error": _error(exc)}
    node_stats = body.get("node") if isinstance(body, dict) else None
    if not isinstance(node_stats, dict):
        return {"state": _UNKNOWN, "error": "answer has no node section"}
    runtime = node_stats.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    return {
        "state": _PRESENT,
        "node_fs": _fs_stats(node_stats.get("fs")),
        "image_fs": _fs_stats(runtime.get("imageFs")),
        "container_fs": _fs_stats(runtime.get("containerFs")),
    }


async def _disk_row(api_client: client.ApiClient, node: Any) -> dict[str, Any]:
    name = _node_name(node)
    summary = (
        await _kubelet_summary(api_client, name)
        if name
        else {"state": _UNKNOWN, "error": "node has no name"}
    )
    row: dict[str, Any] = {
        "name": name,
        "conditions": _condition_rows(node),
        "summary": {"state": summary["state"]},
        "node_fs": None,
        "image_fs": None,
        "container_fs": None,
    }
    if summary["state"] == _PRESENT:
        row.update({k: summary[k] for k in ("node_fs", "image_fs", "container_fs")})
    else:
        row["summary"]["error"] = summary["error"]
    return row


async def k8s_node_disk(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.node.disk``. Reads only; changes nothing."""
    api_client = await connector._get_api_client(target, operator)
    nodes, incomplete, not_found = await _read_nodes(api_client, params.get("node"))
    rows = [await _disk_row(api_client, n) for n in nodes]
    result: dict[str, Any] = {"rows": rows, "total": len(rows), "incomplete": incomplete}
    if not_found:
        result["node_state"] = _NOT_PRESENT
    return result


# ---------------------------------------------------------------------------
# k8s.node.images
# ---------------------------------------------------------------------------


def _digest_of(reference: str) -> str | None:
    """The ``sha256:...`` part of an image reference or image id, when it has one."""
    _, sep, digest = reference.rpartition("@")
    if sep:
        return digest or None
    return reference if reference.startswith("sha256:") else None


def _strip_scheme(image_id: str) -> str:
    """``docker-pullable://repo@sha256:x`` -> ``repo@sha256:x``."""
    return image_id.split("://", 1)[1] if "://" in image_id else image_id


def _pod_image_refs(pod: Any) -> set[str]:
    """Every name and digest a running pod uses for its images."""
    refs: set[str] = set()
    spec = pod.spec
    for attr in ("containers", "init_containers", "ephemeral_containers"):
        for container in (getattr(spec, attr) if spec else None) or []:
            if container.image:
                refs.add(container.image)
    status = pod.status
    statuses = [
        st
        for attr in (
            "container_statuses",
            "init_container_statuses",
            "ephemeral_container_statuses",
        )
        for st in ((getattr(status, attr) if status else None) or [])
    ]
    for st in statuses:
        for text in (st.image, _strip_scheme(st.image_id or "")):
            if text:
                refs.add(text)
                digest = _digest_of(text)
                if digest:
                    refs.add(digest)
    return refs


def _is_running(pod: Any) -> bool:
    phase = pod.status.phase if pod.status else None
    return phase not in ("Succeeded", "Failed")


class _PodListCutError(Exception):
    """The pods of a node did not fit in the page cap: "not used" cannot be said."""


async def _used_refs(api_client: client.ApiClient, node: str) -> set[str]:
    pods, incomplete = await _list_all(
        client.CoreV1Api(api_client).list_pod_for_all_namespaces,
        field_selector=f"spec.nodeName={node}",
    )
    if incomplete:
        raise _PodListCutError
    refs: set[str] = set()
    for pod in pods:
        if _is_running(pod):
            refs |= _pod_image_refs(pod)
    return refs


def _image_used(names: list[str], refs: set[str]) -> bool:
    return any(n in refs or _digest_of(n) in refs for n in names)


def _image_rows(node: Any, refs: set[str] | None) -> list[dict[str, Any]]:
    name = _node_name(node)
    images = (node.status.images if node.status else None) or []
    rows = [
        {
            "node": name,
            "names": list(img.names or []),
            "size_bytes": img.size_bytes,
            "used_by_running_pod": None if refs is None else _image_used(img.names or [], refs),
        }
        for img in images
    ]
    rows.sort(key=lambda r: r["size_bytes"] or 0, reverse=True)
    return rows


def _total_size(rows: list[dict[str, Any]]) -> int | None:
    """The sum of the image sizes, or ``None`` when one size is not known."""
    sizes = [r["size_bytes"] for r in rows]
    return None if None in sizes else sum(sizes)


async def _node_images(
    api_client: client.ApiClient, node: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    name = _node_name(node)
    refs: set[str] | None = None
    pods_error: str | None = None
    if not name:
        pods_error = "node has no name"
    else:
        try:
            refs = await _used_refs(api_client, name)
        except _PodListCutError:
            pods_error = "too many pods to read"
        except ApiException as exc:
            if exc.status == 401:
                raise
            pods_error = _error(exc)
        except Exception as exc:
            pods_error = _error(exc)
    rows = _image_rows(node, refs)
    summary: dict[str, Any] = {
        "name": name,
        "image_count": len(rows),
        "total_size_bytes": _total_size(rows),
        "list_may_be_cut": len(rows) >= _KUBELET_DEFAULT_MAX_IMAGES,
        "pods": {"state": _UNKNOWN, "error": pods_error} if pods_error else {"state": _PRESENT},
        "unused_count": None,
        "unused_size_bytes": None,
    }
    if refs is not None:
        unused = [r for r in rows if not r["used_by_running_pod"]]
        summary["unused_count"] = len(unused)
        summary["unused_size_bytes"] = _total_size(unused)
    return summary, rows


async def k8s_node_images(
    connector: KubernetesConnector,
    target: KubernetesTargetLike,
    operator: Operator,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handler for ``k8s.node.images``. Reads only; changes nothing."""
    api_client = await connector._get_api_client(target, operator)
    nodes, incomplete, not_found = await _read_nodes(api_client, params.get("node"))
    node_rows: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for node in nodes:
        summary, images = await _node_images(api_client, node)
        node_rows.append(summary)
        rows.extend(images)
    result: dict[str, Any] = {
        "nodes": node_rows,
        "rows": rows,
        "total": len(rows),
        "incomplete": incomplete,
    }
    if not_found:
        result["node_state"] = _NOT_PRESENT
    return result


NODE_DISK_OPS: tuple[KubernetesOp, ...] = (
    KubernetesOp(
        op_id="k8s.node.disk",
        handler_attr="k8s_node_disk",
        summary="Show a node's disk space and its disk, memory and process pressure conditions.",
        description=(
            "Changes nothing. Reads the kubelet summary through the API server "
            "(file system capacity, used and free bytes of the node and of the image "
            "store) and the node's DiskPressure, MemoryPressure and PIDPressure "
            "conditions with their last change time."
        ),
        parameter_schema=K8S_NODE_DISK_PARAMETER_SCHEMA,
        response_schema=K8S_NODE_DISK_RESPONSE_SCHEMA,
        group_key="inventory",
        tags=("read-only", "node", "disk"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_NODE_DISK_LLM_INSTRUCTIONS,
    ),
    KubernetesOp(
        op_id="k8s.node.images",
        handler_attr="k8s_node_images",
        summary="List the container images on a node with their sizes and whether a pod uses them.",
        description=(
            "Changes nothing. Reads the images in the node status (names, size in "
            "bytes, biggest first) and flags each one that a running pod of the node "
            "uses. Gives a total per node."
        ),
        parameter_schema=K8S_NODE_IMAGES_PARAMETER_SCHEMA,
        response_schema=K8S_NODE_IMAGES_RESPONSE_SCHEMA,
        group_key="inventory",
        tags=("read-only", "node", "image"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=K8S_NODE_IMAGES_LLM_INSTRUCTIONS,
    ),
)

#: Collected by :func:`~meho_backplane.connectors.kubernetes.ops.collect_ops`.
OPS: tuple[KubernetesOp, ...] = NODE_DISK_OPS

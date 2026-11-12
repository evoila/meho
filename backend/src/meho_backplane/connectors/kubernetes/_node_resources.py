# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""CPU and memory of a node row (``k8s.node.list``): size and what is reserved.

``capacity`` and ``allocatable`` come from the node status as the API writes
them. ``requested`` is the sum of the requests of the pods that run on the
node, so an operator can tell how much more fits. A pod that ended
(``Succeeded`` or ``Failed``) holds nothing and is not counted. This is the
same rule the scheduler uses.

When the pod list cannot be read to the end, or a quantity cannot be read,
``requested`` is ``None`` (unknown). It is never a smaller sum that looks
like the whole one.

All of this is behind the switch ``K8S_NODE_LIST_RESOURCES``
(:attr:`~meho_backplane.settings.Settings.k8s_node_list_resources`, off by
default). Off: ``k8s.node.list`` answers, and is described, as before.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from math import ceil
from typing import TYPE_CHECKING, Any

from kubernetes_asyncio.client.exceptions import ApiException

from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.kubernetes.ops import KubernetesOp

__all__ = [
    "NODE_LIST_OP_ID",
    "NODE_RESOURCE_PROPERTIES",
    "add_node_resources",
    "add_requested",
    "node_list_resources_on",
    "node_resources",
    "pod_requests_by_node",
    "with_node_resources",
]

NODE_LIST_OP_ID = "k8s.node.list"

_PAGE_SIZE = 500
_MAX_PAGES = 40

_RESOURCES = ("cpu", "memory", "pods")
_ENDED_PHASES = ("Succeeded", "Failed")

_SUFFIX = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "": Decimal(1),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
    "Ki": Decimal(2**10),
    "Mi": Decimal(2**20),
    "Gi": Decimal(2**30),
    "Ti": Decimal(2**40),
    "Pi": Decimal(2**50),
    "Ei": Decimal(2**60),
}
_QUANTITY = re.compile(r"^([+-]?[0-9.]+(?:[eE][+-]?[0-9]+)?)([A-Za-z]*)$")

_QUANTITY_MAP = {
    "type": ["object", "null"],
    "properties": {name: {"type": ["string", "null"]} for name in _RESOURCES},
    "additionalProperties": False,
}

#: Response-schema properties of the fields :func:`node_resources` returns.
NODE_RESOURCE_PROPERTIES: dict[str, Any] = {
    "capacity": {
        **_QUANTITY_MAP,
        "description": "Total cpu, memory and pods of the node, as the API writes them.",
    },
    "allocatable": {
        **_QUANTITY_MAP,
        "description": "What pods may use: capacity minus what the node keeps back.",
    },
    "requested": {
        "type": ["object", "null"],
        "properties": {
            "cpu_millicores": {"type": "integer"},
            "memory_bytes": {"type": "integer"},
            "pods": {"type": "integer"},
        },
        "required": ["cpu_millicores", "memory_bytes", "pods"],
        "additionalProperties": False,
        "description": (
            "Sum of the requests of the pods on the node that have not ended. "
            "null when it cannot be said (see requests_error)."
        ),
    },
    "requests_error": {
        "type": ["string", "null"],
        "description": "Why requested is null (pod list failed or cut), else null.",
    },
}

_DESCRIPTION_MORE = (
    " Each row also has ``capacity`` / ``allocatable`` (the node's cpu, "
    "memory and pods) and ``requested``: the sum of the requests of the "
    "pods on it that have not ended."
)

_OUTPUT_SHAPE_MORE = (
    " Each row also has capacity, allocatable, requested and "
    "requests_error. ``capacity`` and ``allocatable`` are {cpu, memory, "
    "pods} as the API writes them ('4', '16393216Ki', '110'). "
    "``requested`` is the sum of the requests of the pods on the node that "
    "have not ended: {cpu_millicores, memory_bytes, pods}. Free room = "
    "allocatable minus requested (cpu '4' = 4000 millicores; 'Ki' = 1024 "
    "bytes). ``requested`` is null when the pod list could not be read to "
    "the end: then ``requests_error`` says why. Never treat null as zero."
)


def node_list_resources_on() -> bool:
    """Return the state of the ``K8S_NODE_LIST_RESOURCES`` switch."""
    return get_settings().k8s_node_list_resources


def with_node_resources(op: KubernetesOp) -> KubernetesOp:
    """Return ``k8s.node.list`` with the new fields in its text and response schema."""
    schema = copy.deepcopy(op.response_schema or {})
    row = schema["properties"]["rows"]["items"]
    row["properties"].update(NODE_RESOURCE_PROPERTIES)
    row["required"].extend(NODE_RESOURCE_PROPERTIES)
    instructions = dict(op.llm_instructions or {})
    instructions["output_shape"] = instructions.get("output_shape", "") + _OUTPUT_SHAPE_MORE
    return replace(
        op,
        description=op.description + _DESCRIPTION_MORE,
        response_schema=schema,
        llm_instructions=instructions,
    )


def _quantity_map(values: Any) -> dict[str, str | None] | None:
    if not values:
        return None
    return {
        name: (str(values[name]) if values.get(name) is not None else None) for name in _RESOURCES
    }


def node_resources(node: Any) -> dict[str, Any]:
    """Return ``capacity`` and ``allocatable`` of a :class:`V1Node`."""
    status = node.status
    return {
        "capacity": _quantity_map(getattr(status, "capacity", None) if status else None),
        "allocatable": _quantity_map(getattr(status, "allocatable", None) if status else None),
    }


def _parse(text: Any) -> Decimal:
    match = _QUANTITY.match(str(text))
    if match is None or match.group(2) not in _SUFFIX:
        raise ValueError("unreadable quantity")
    try:
        return Decimal(match.group(1)) * _SUFFIX[match.group(2)]
    except InvalidOperation as exc:
        raise ValueError("unreadable quantity") from exc


def _request(owner: Any, resource: str) -> Decimal | None:
    """The request of a container or a pod spec; ``None`` when it sets none."""
    resources = getattr(owner, "resources", None)
    requests = (getattr(resources, "requests", None) if resources else None) or {}
    value = requests.get(resource)
    return _parse(value) if value is not None else None


def _is_sidecar(container: Any) -> bool:
    return getattr(container, "restart_policy", None) == "Always"


def _pod_request(pod: Any, resource: str) -> Decimal:
    """What the scheduler reserves for one pod, plus the pod overhead.

    A pod-level request (``spec.resources.requests``) replaces the sum of the
    containers for that resource. Else it is the containers and sidecars, or
    the init peak when that is higher.
    """
    spec = pod.spec
    pod_level = _request(spec, resource)
    total = pod_level if pod_level is not None else _containers_request(spec, resource)
    overhead = getattr(spec, "overhead", None) or {}
    if overhead.get(resource) is not None:
        total += _parse(overhead[resource])
    return total


def _containers_request(spec: Any, resource: str) -> Decimal:
    containers = spec.containers or []
    inits = spec.init_containers or []
    running = sum((_request(c, resource) or Decimal(0) for c in containers), Decimal(0))
    sidecars = Decimal(0)
    init_peak = Decimal(0)
    for init in inits:
        value = _request(init, resource) or Decimal(0)
        if _is_sidecar(init):
            sidecars += value
            running += value
        else:
            init_peak = max(init_peak, sidecars + value)
    return max(running, init_peak)


def pod_requests_by_node(pods: list[Any]) -> dict[str, dict[str, int] | None]:
    """Sum the requests of the pods that have not ended, for each node name.

    A node maps to ``None`` when one of its pods has a quantity that cannot
    be read. A pod that is not on a node yet (``spec.node_name`` empty)
    counts for no node.
    """
    sums: dict[str, dict[str, int] | None] = {}
    for pod in pods:
        if pod.spec is None or not pod.spec.node_name:
            continue
        if pod.status is not None and pod.status.phase in _ENDED_PHASES:
            continue
        name = pod.spec.node_name
        if name in sums and sums[name] is None:
            continue
        try:
            cpu = _pod_request(pod, "cpu")
            memory = _pod_request(pod, "memory")
        except ValueError:
            sums[name] = None
            continue
        current = sums.get(name) or {"cpu_millicores": 0, "memory_bytes": 0, "pods": 0}
        sums[name] = {
            "cpu_millicores": current["cpu_millicores"] + ceil(cpu * 1000),
            "memory_bytes": current["memory_bytes"] + ceil(memory),
            "pods": current["pods"] + 1,
        }
    return sums


async def _list_all_pods(call: Callable[..., Awaitable[Any]]) -> tuple[list[Any], bool]:
    """Follow every page of the pod list. Returns ``(pods, cap reached)``."""
    pods: list[Any] = []
    token: str | None = None
    for _page in range(_MAX_PAGES):
        kwargs: dict[str, Any] = {"limit": _PAGE_SIZE}
        if token:
            kwargs["_continue"] = token
        resp = await call(**kwargs)
        pods.extend(resp.items or [])
        token = resp.metadata._continue if resp.metadata else None
        if not token:
            return pods, False
    return pods, True


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ApiException):
        return f"pod list failed: HTTP {exc.status}"
    return f"pod list failed: {type(exc).__name__}"


async def add_requested(
    rows: list[dict[str, Any]], list_pods: Callable[..., Awaitable[Any]]
) -> None:
    """Add ``requested`` and ``requests_error`` to every node row, in place.

    A 401 is raised so the dispatcher can renew the login. Any other failure,
    or a pod list cut at the page cap, makes ``requested`` ``None`` with the
    reason in ``requests_error``.
    """
    sums: dict[str, dict[str, int] | None] = {}
    error: str | None = None
    try:
        pods, cut = await _list_all_pods(list_pods)
    except ApiException as exc:
        if exc.status == 401:
            raise
        error = _error_text(exc)
    except Exception as exc:
        error = _error_text(exc)
    else:
        if cut:
            error = "pod list is longer than the page cap"
        else:
            sums = pod_requests_by_node(pods)
    for row in rows:
        if error is not None:
            row["requested"], row["requests_error"] = None, error
        elif row["name"] in sums and sums[row["name"]] is None:
            row["requested"], row["requests_error"] = (
                None,
                "a pod has a request that cannot be read",
            )
        else:
            zero = {"cpu_millicores": 0, "memory_bytes": 0, "pods": 0}
            row["requested"], row["requests_error"] = sums.get(row["name"]) or zero, None


async def add_node_resources(
    rows: list[dict[str, Any]],
    nodes: Sequence[Any],
    list_pods: Callable[..., Awaitable[Any]],
) -> None:
    """Add ``capacity``, ``allocatable``, ``requested`` and ``requests_error`` in place."""
    for row, node in zip(rows, nodes, strict=True):
        row.update(node_resources(node))
    await add_requested(rows, list_pods)

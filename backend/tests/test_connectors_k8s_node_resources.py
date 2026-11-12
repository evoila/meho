# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.node.list``: capacity, allocatable and what the pods on a node request."""

from __future__ import annotations

from unittest.mock import AsyncMock

import jsonschema
import pytest
from kubernetes_asyncio.client import (
    V1Container,
    V1ListMeta,
    V1Node,
    V1NodeStatus,
    V1ObjectMeta,
    V1Pod,
    V1PodList,
    V1PodSpec,
    V1PodStatus,
    V1ResourceRequirements,
)
from kubernetes_asyncio.client.exceptions import ApiException

from meho_backplane.connectors.kubernetes._node_resources import (
    add_requested,
    node_resources,
    pod_requests_by_node,
)
from meho_backplane.connectors.kubernetes.ops import KUBERNETES_OPS
from meho_backplane.connectors.kubernetes.ops_core import node_row


def _container(cpu: str | None = None, memory: str | None = None, **extra: object) -> V1Container:
    requests = {k: v for k, v in (("cpu", cpu), ("memory", memory)) if v is not None}
    return V1Container(
        name="c", resources=V1ResourceRequirements(requests=requests or None), **extra
    )


def _pod(
    node: str | None,
    containers: list[V1Container],
    *,
    phase: str = "Running",
    inits: list[V1Container] | None = None,
    overhead: dict[str, str] | None = None,
) -> V1Pod:
    return V1Pod(
        metadata=V1ObjectMeta(name="p"),
        spec=V1PodSpec(
            node_name=node, containers=containers, init_containers=inits, overhead=overhead
        ),
        status=V1PodStatus(phase=phase),
    )


def _node(name: str) -> V1Node:
    return V1Node(
        metadata=V1ObjectMeta(name=name),
        status=V1NodeStatus(
            capacity={"cpu": "8", "memory": "32Gi", "pods": "110", "ephemeral-storage": "1Ti"},
            allocatable={"cpu": "7910m", "memory": "31000Mi", "pods": "110"},
        ),
    )


def test_node_resources_returns_cpu_memory_pods_only() -> None:
    result = node_resources(_node("n1"))
    assert result["capacity"] == {"cpu": "8", "memory": "32Gi", "pods": "110"}
    assert result["allocatable"] == {"cpu": "7910m", "memory": "31000Mi", "pods": "110"}


def test_node_resources_missing_status_is_null() -> None:
    assert node_resources(V1Node(metadata=V1ObjectMeta(name="n"))) == {
        "capacity": None,
        "allocatable": None,
    }


def test_requests_sum_units_and_skip_ended_pods() -> None:
    pods = [
        _pod("n1", [_container("250m", "128Mi"), _container("1", "1Gi")]),
        _pod("n1", [_container("500m", "1G")]),
        _pod("n1", [_container("4", "8Gi")], phase="Succeeded"),
        _pod("n1", [_container("4", "8Gi")], phase="Failed"),
        _pod("n1", [_container("100m", None)], phase="Pending"),
        _pod("n2", [_container("2", "2Ki")]),
    ]
    sums = pod_requests_by_node(pods)
    assert sums["n1"] == {
        "cpu_millicores": 250 + 1000 + 500 + 100,
        "memory_bytes": 128 * 2**20 + 2**30 + 10**9,
        "pods": 3,
    }
    assert sums["n2"] == {"cpu_millicores": 2000, "memory_bytes": 2048, "pods": 1}


def test_requests_edge_inputs() -> None:
    pods = [
        _pod(None, [_container("1", "1Gi")]),  # not scheduled yet: counts for no node
        _pod("n1", [_container(None, None)]),  # no requests: zero, but the pod is counted
        _pod("n3", [_container("1500u", "1e3")]),  # micro cpu rounds up; exponent form
    ]
    sums = pod_requests_by_node(pods)
    assert set(sums) == {"n1", "n3"}
    assert sums["n1"] == {"cpu_millicores": 0, "memory_bytes": 0, "pods": 1}
    assert sums["n3"] == {"cpu_millicores": 2, "memory_bytes": 1000, "pods": 1}


def test_requests_init_peak_sidecar_and_overhead() -> None:
    sidecar = _container("100m", "100Mi", restart_policy="Always")
    big_init = _container("2", "1Gi")
    pod = _pod(
        "n1",
        [_container("500m", "500Mi")],
        inits=[sidecar, big_init],
        overhead={"cpu": "10m", "memory": "10Mi"},
    )
    # Running: 500m + 100m sidecar = 600m. Init peak: 100m sidecar + 2 = 2100m. Max wins.
    sums = pod_requests_by_node([pod])
    assert sums["n1"] == {
        "cpu_millicores": 2100 + 10,
        "memory_bytes": (100 + 1024) * 2**20 + 10 * 2**20,
        "pods": 1,
    }


def test_unreadable_quantity_makes_the_node_unknown_not_zero() -> None:
    pods = [_pod("n1", [_container("lots", "1Gi")]), _pod("n1", [_container("1", "1Gi")])]
    assert pod_requests_by_node(pods) == {"n1": None}


def _rows() -> list[dict[str, object]]:
    return [node_row(_node("n1")), node_row(_node("idle"))]


@pytest.mark.asyncio
async def test_add_requested_follows_every_page() -> None:
    pages = [
        V1PodList(
            items=[_pod("n1", [_container("1", "1Gi")])], metadata=V1ListMeta(_continue="tok")
        ),
        V1PodList(items=[_pod("n1", [_container("2", "1Gi")])], metadata=V1ListMeta()),
    ]
    call = AsyncMock(side_effect=pages)
    rows = _rows()
    await add_requested(rows, call)
    assert call.await_args_list[1].kwargs["_continue"] == "tok"
    assert rows[0]["requested"] == {"cpu_millicores": 3000, "memory_bytes": 2 * 2**30, "pods": 2}
    assert rows[0]["requests_error"] is None
    # A node with no pods has zero requested, which is a real answer.
    assert rows[1]["requested"] == {"cpu_millicores": 0, "memory_bytes": 0, "pods": 0}


@pytest.mark.asyncio
async def test_add_requested_cut_list_is_unknown_not_partial() -> None:
    page = V1PodList(items=[_pod("n1", [_container("1")])], metadata=V1ListMeta(_continue="more"))
    rows = _rows()
    await add_requested(rows, AsyncMock(return_value=page))
    assert all(r["requested"] is None for r in rows)
    assert "page cap" in str(rows[0]["requests_error"])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 500, 503])
async def test_add_requested_failed_read_is_unknown_not_zero(status: int) -> None:
    rows = _rows()
    await add_requested(rows, AsyncMock(side_effect=ApiException(status=status)))
    assert all(r["requested"] is None for r in rows)
    assert rows[0]["requests_error"] == f"pod list failed: HTTP {status}"


@pytest.mark.asyncio
async def test_add_requested_other_error_is_unknown() -> None:
    rows = _rows()
    await add_requested(rows, AsyncMock(side_effect=TimeoutError()))
    assert rows[0]["requested"] is None
    assert rows[0]["requests_error"] == "pod list failed: TimeoutError"


@pytest.mark.asyncio
async def test_add_requested_401_is_raised_for_the_dispatcher() -> None:
    with pytest.raises(ApiException):
        await add_requested(_rows(), AsyncMock(side_effect=ApiException(status=401)))


@pytest.mark.asyncio
async def test_add_requested_unreadable_quantity_marks_only_that_node() -> None:
    page = V1PodList(
        items=[_pod("n1", [_container("junk")]), _pod("other", [_container("1")])],
        metadata=V1ListMeta(),
    )
    rows = _rows()
    await add_requested(rows, AsyncMock(return_value=page))
    assert rows[0]["requested"] is None
    assert "cannot be read" in str(rows[0]["requests_error"])
    assert rows[1]["requested"] == {"cpu_millicores": 0, "memory_bytes": 0, "pods": 0}


@pytest.mark.asyncio
async def test_rows_match_the_response_schema_known_and_unknown() -> None:
    schema = {op.op_id: op for op in KUBERNETES_OPS}["k8s.node.list"].response_schema
    for call in (
        AsyncMock(
            return_value=V1PodList(items=[_pod("n1", [_container("1")])], metadata=V1ListMeta())
        ),
        AsyncMock(side_effect=ApiException(status=500)),
    ):
        rows = _rows()
        await add_requested(rows, call)
        jsonschema.validate({"rows": rows, "total": len(rows)}, schema)


def test_op_text_names_the_new_fields() -> None:
    op = {o.op_id: o for o in KUBERNETES_OPS}["k8s.node.list"]
    text = str(op.llm_instructions) + op.description
    for word in ("capacity", "allocatable", "requested", "requests_error"):
        assert word in text

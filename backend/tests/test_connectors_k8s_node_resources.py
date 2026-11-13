# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.node.list``: capacity, allocatable and what the pods on a node request."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jsonschema
import pytest
from kubernetes_asyncio.client import (
    V1Container,
    V1ListMeta,
    V1Node,
    V1NodeList,
    V1NodeStatus,
    V1ObjectMeta,
    V1Pod,
    V1PodList,
    V1PodSpec,
    V1PodStatus,
    V1ResourceRequirements,
)
from kubernetes_asyncio.client.exceptions import ApiException

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.kubernetes._node_resources import (
    add_node_resources,
    add_requested,
    node_resources,
    pod_requests_by_node,
    with_node_resources,
)
from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
from meho_backplane.connectors.kubernetes.ops import KUBERNETES_OPS
from meho_backplane.connectors.kubernetes.ops_core import node_row
from meho_backplane.settings import get_settings

_NEW_FIELDS = ("capacity", "allocatable", "requested", "requests_error")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("K8S_NODE_LIST_RESOURCES", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K8S_NODE_LIST_RESOURCES", "true")
    get_settings.cache_clear()


def _node_list_op() -> Any:
    return {op.op_id: op for op in KUBERNETES_OPS}["k8s.node.list"]


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
    pod_requests: dict[str, str] | None = None,
) -> V1Pod:
    return V1Pod(
        metadata=V1ObjectMeta(name="p"),
        spec=V1PodSpec(
            node_name=node,
            containers=containers,
            init_containers=inits,
            overhead=overhead,
            resources=V1ResourceRequirements(requests=pod_requests) if pod_requests else None,
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


def test_pod_level_requests_with_no_container_requests() -> None:
    pod = _pod("n1", [_container(None, None)], pod_requests={"cpu": "2", "memory": "1Gi"})
    assert pod_requests_by_node([pod])["n1"] == {
        "cpu_millicores": 2000,
        "memory_bytes": 2**30,
        "pods": 1,
    }


def test_pod_level_request_replaces_the_container_sum_per_resource() -> None:
    # Pod-level cpu is smaller than the containers and still wins. Memory is not
    # set at pod level, so it stays the container sum. Overhead is added after.
    pod = _pod(
        "n1",
        [_container("3", "512Mi"), _container("1", "512Mi")],
        pod_requests={"cpu": "1500m"},
        overhead={"cpu": "10m", "memory": "1Mi"},
    )
    assert pod_requests_by_node([pod])["n1"] == {
        "cpu_millicores": 1500 + 10,
        "memory_bytes": 1024 * 2**20 + 2**20,
        "pods": 1,
    }


def test_unreadable_pod_level_request_makes_the_node_unknown() -> None:
    pod = _pod("n1", [_container("1", "1Gi")], pod_requests={"memory": "much"})
    assert pod_requests_by_node([pod]) == {"n1": None}


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
    schema = with_node_resources(_node_list_op()).response_schema
    nodes = [_node("n1"), _node("idle")]
    for call in (
        AsyncMock(
            return_value=V1PodList(items=[_pod("n1", [_container("1")])], metadata=V1ListMeta())
        ),
        AsyncMock(side_effect=ApiException(status=500)),
    ):
        rows = [node_row(n) for n in nodes]
        await add_node_resources(rows, nodes, call)
        jsonschema.validate({"rows": rows, "total": len(rows)}, schema)


def test_op_text_with_the_switch_names_the_new_fields() -> None:
    op = with_node_resources(_node_list_op())
    text = str(op.llm_instructions) + op.description
    for word in _NEW_FIELDS:
        assert word in text


def test_op_text_without_the_switch_is_unchanged() -> None:
    op = _node_list_op()
    text = str(op.llm_instructions) + op.description + str(op.response_schema)
    for word in ("allocatable", "requested", "requests_error"):
        assert word not in text
    on = with_node_resources(op)
    # The switch only adds text; the old text stays as it is.
    assert on.description.startswith(op.description)
    assert on.llm_instructions["output_shape"].startswith(op.llm_instructions["output_shape"])
    assert {k: v for k, v in on.llm_instructions.items() if k != "output_shape"} == {
        k: v for k, v in op.llm_instructions.items() if k != "output_shape"
    }


async def _run_handler() -> tuple[dict[str, Any], AsyncMock]:
    nodes = [_node("n1"), _node("idle")]
    pods = AsyncMock(
        return_value=V1PodList(items=[_pod("n1", [_container("1", "1Gi")])], metadata=V1ListMeta())
    )
    connector = KubernetesConnector(kubeconfig_loader=AsyncMock())
    with (
        patch.object(connector, "_get_api_client", AsyncMock(return_value=MagicMock())),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_node = AsyncMock(
            return_value=V1NodeList(items=nodes, metadata=V1ListMeta())
        )
        core_v1_cls.return_value.list_pod_for_all_namespaces = pods
        result = await connector.k8s_node_list(MagicMock(spec=Operator), MagicMock(), {})
    return result, pods


@pytest.mark.asyncio
async def test_handler_without_the_switch_answers_as_before() -> None:
    result, pods = await _run_handler()
    pods.assert_not_awaited()
    assert result["rows"] == [node_row(_node("n1")), node_row(_node("idle"))]
    for row in result["rows"]:
        assert not set(_NEW_FIELDS) & set(row)
    jsonschema.validate(result, _node_list_op().response_schema)


@pytest.mark.asyncio
async def test_handler_with_the_switch_adds_the_new_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    result, pods = await _run_handler()
    pods.assert_awaited()
    first = result["rows"][0]
    assert first["capacity"] == {"cpu": "8", "memory": "32Gi", "pods": "110"}
    assert first["requested"] == {"cpu_millicores": 1000, "memory_bytes": 2**30, "pods": 1}
    jsonschema.validate(result, with_node_resources(_node_list_op()).response_schema)


async def _registered_node_list(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from meho_backplane.operations import typed_register

    register = AsyncMock()
    monkeypatch.setattr(typed_register, "register_typed_operation", register)
    await KubernetesConnector.register_operations()
    calls = [c.kwargs for c in register.await_args_list if c.kwargs["op_id"] == "k8s.node.list"]
    assert len(calls) == 1
    return calls[0]


@pytest.mark.asyncio
async def test_register_without_the_switch_keeps_the_old_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op = _node_list_op()
    kwargs = await _registered_node_list(monkeypatch)
    assert kwargs["description"] == op.description
    assert kwargs["llm_instructions"] == op.llm_instructions
    assert kwargs["response_schema"] == op.response_schema


@pytest.mark.asyncio
async def test_register_with_the_switch_uses_the_new_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    on = with_node_resources(_node_list_op())
    kwargs = await _registered_node_list(monkeypatch)
    assert kwargs["description"] == on.description
    assert kwargs["llm_instructions"] == on.llm_instructions
    assert kwargs["response_schema"] == on.response_schema


@pytest.mark.asyncio
async def test_register_does_not_need_the_other_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYCLOAK_ISSUER_URL", raising=False)
    get_settings.cache_clear()
    _switch_on(monkeypatch)
    kwargs = await _registered_node_list(monkeypatch)
    assert kwargs["description"] == with_node_resources(_node_list_op()).description

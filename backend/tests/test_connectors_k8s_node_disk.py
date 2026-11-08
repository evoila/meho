# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``k8s.node.disk`` and ``k8s.node.images``.

The Kubernetes API is a set of fake API classes that hand back real
``kubernetes_asyncio`` model objects. The kubelet summary goes through a fake
``ApiClient.call_api``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.client.models import (
    V1Container,
    V1ContainerImage,
    V1ContainerStatus,
    V1ListMeta,
    V1Node,
    V1NodeCondition,
    V1NodeList,
    V1NodeStatus,
    V1ObjectMeta,
    V1Pod,
    V1PodList,
    V1PodSpec,
    V1PodStatus,
)

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import (
    KUBERNETES_OPS,
    KubernetesConnector,
    KubernetesTargetLike,
)
from meho_backplane.settings import get_settings

_MOD = "meho_backplane.connectors.kubernetes.ops_node_disk"
_BIG_DIGEST = "sha256:" + "aa" * 32
_SMALL_DIGEST = "sha256:" + "bb" * 32
_OLD_DIGEST = "sha256:" + "cc" * 32
_GB = 1024**3


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    id: object = field(default_factory=uuid4)
    tenant_id: object = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget(name="k8s-unit", host="k8s-unit.test.invalid", port=6443, secret_ref="k8s/x")


def _make_connector() -> KubernetesConnector:
    async def _loader(_target: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "default",
            "contexts": [{"name": "default", "context": {"cluster": "c1", "user": "u1"}}],
            "clusters": [{"name": "c1", "cluster": {"server": "https://k8s.test:6443"}}],
            "users": [{"name": "u1", "user": {"token": "stub-token"}}],
        }

    return KubernetesConnector(kubeconfig_loader=_loader)


def _operator() -> Operator:
    return Operator(
        sub="op-node-disk-test",
        name="Node Disk Test Operator",
        email=None,
        raw_jwt="op.node.disk.jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000005955"),
        tenant_role=TenantRole.OPERATOR,
    )


def _node(
    name: str = "worker-1",
    images: list[V1ContainerImage] | None = None,
    conditions: list[V1NodeCondition] | None = None,
) -> V1Node:
    return V1Node(
        metadata=V1ObjectMeta(name=name),
        status=V1NodeStatus(images=images or [], conditions=conditions or []),
    )


def _image(names: list[str], size: int) -> V1ContainerImage:
    return V1ContainerImage(names=names, size_bytes=size)


def _condition(kind: str, status: str) -> V1NodeCondition:
    return V1NodeCondition(
        type=kind,
        status=status,
        reason="made-up-reason",
        last_transition_time=datetime(2026, 11, 7, 10, 0, tzinfo=UTC),
    )


def _pod(
    image: str,
    image_id: str = "",
    *,
    phase: str = "Running",
    init: bool = False,
) -> V1Pod:
    status = V1ContainerStatus(
        name="c", image=image, image_id=image_id, ready=True, restart_count=0
    )
    return V1Pod(
        spec=V1PodSpec(
            containers=[] if init else [V1Container(name="c", image=image)],
            init_containers=[V1Container(name="c", image=image)] if init else None,
        ),
        status=V1PodStatus(
            phase=phase,
            container_statuses=None if init else [status],
            init_container_statuses=[status] if init else None,
        ),
    )


def _summary(
    *, node_fs: bool = True, image_fs: bool = True, container_fs: bool = False
) -> dict[str, Any]:
    def fs(cap: int, used: int) -> dict[str, int]:
        return {"capacityBytes": cap, "usedBytes": used, "availableBytes": cap - used}

    runtime: dict[str, Any] = {}
    if image_fs:
        runtime["imageFs"] = fs(60 * _GB, 45 * _GB)
    if container_fs:
        runtime["containerFs"] = fs(60 * _GB, 1 * _GB)
    return {"node": {"nodeName": "worker-1", "fs": fs(60 * _GB, 50 * _GB), "runtime": runtime}}


@dataclass
class _Cluster:
    """What the fake API answers. An ``Exception`` value is raised."""

    nodes: list[V1Node] | Exception = field(default_factory=lambda: [_node()])
    pods: list[V1Pod] | Exception = field(default_factory=list)
    summary: dict[str, Any] | Exception = field(default_factory=_summary)
    read_node: V1Node | Exception | None = None


@dataclass
class _Fakes:
    core: MagicMock
    api_client: MagicMock


def _raise_or(value: Any) -> AsyncMock:
    if isinstance(value, Exception):
        return AsyncMock(side_effect=value)
    return AsyncMock(return_value=value)


async def _run(
    op: str, cluster: _Cluster, params: dict[str, Any] | None = None
) -> tuple[dict[str, Any], _Fakes]:
    core = MagicMock()
    nodes = cluster.nodes
    core.list_node = _raise_or(
        V1NodeList(items=nodes, metadata=V1ListMeta()) if isinstance(nodes, list) else nodes
    )
    core.read_node = _raise_or(cluster.read_node if cluster.read_node is not None else _node())
    pods = cluster.pods
    core.list_pod_for_all_namespaces = _raise_or(
        V1PodList(items=pods, metadata=V1ListMeta()) if isinstance(pods, list) else pods
    )
    api_client = MagicMock(close=AsyncMock())
    api_client.call_api = _raise_or(cluster.summary)
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=api_client,
        ),
        patch(f"{_MOD}.client.CoreV1Api", return_value=core),
    ):
        result = await getattr(_make_connector(), op)(_operator(), _TARGET, params or {})
    return result, _Fakes(core=core, api_client=api_client)


def _api_error(status: int) -> ApiException:
    return ApiException(status=status, reason="made-up reason")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_id", "handler"),
    [("k8s.node.disk", "k8s_node_disk"), ("k8s.node.images", "k8s_node_images")],
)
def test_registered_as_safe_reads(op_id: str, handler: str) -> None:
    op = next(o for o in KUBERNETES_OPS if o.op_id == op_id)
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert "read-only" in op.tags
    assert op.handler_attr == handler
    assert op.llm_instructions is not None
    assert hasattr(KubernetesConnector, handler)


# ---------------------------------------------------------------------------
# k8s.node.disk
# ---------------------------------------------------------------------------


async def test_disk_reports_sizes_and_pressure_conditions() -> None:
    cluster = _Cluster(
        nodes=[
            _node(
                conditions=[
                    _condition("Ready", "True"),
                    _condition("DiskPressure", "True"),
                    _condition("MemoryPressure", "False"),
                    _condition("PIDPressure", "False"),
                ]
            )
        ],
        summary=_summary(container_fs=True),
    )
    result, fakes = await _run("k8s_node_disk", cluster)

    assert result["total"] == 1
    assert result["incomplete"] is False
    row = result["rows"][0]
    assert row["name"] == "worker-1"
    assert row["summary"] == {"state": "present"}
    assert row["node_fs"] == {
        "capacity_bytes": 60 * _GB,
        "used_bytes": 50 * _GB,
        "available_bytes": 10 * _GB,
        "used_percent": 83.3,
    }
    assert row["image_fs"]["available_bytes"] == 15 * _GB
    assert row["container_fs"]["used_bytes"] == 1 * _GB
    kinds = {c["type"]: c["status"] for c in row["conditions"]}
    assert kinds == {"DiskPressure": "True", "MemoryPressure": "False", "PIDPressure": "False"}
    assert row["conditions"][0]["last_transition_time"] == "2026-11-07T10:00:00Z"
    path = fakes.api_client.call_api.await_args.args[0]
    assert path == "/api/v1/nodes/{name}/proxy/stats/summary"
    assert fakes.api_client.call_api.await_args.kwargs["path_params"] == {"name": "worker-1"}


async def test_disk_without_image_store_or_container_store_gives_null() -> None:
    result, _ = await _run("k8s_node_disk", _Cluster(summary=_summary(image_fs=False)))
    row = result["rows"][0]
    assert row["node_fs"] is not None
    assert row["image_fs"] is None
    assert row["container_fs"] is None
    assert row["summary"]["state"] == "present"


async def test_disk_edge_inputs_null_sizes_and_no_conditions() -> None:
    summary = {"node": {"fs": {"capacityBytes": 0, "usedBytes": None}, "runtime": None}}
    result, _ = await _run("k8s_node_disk", _Cluster(summary=summary))
    row = result["rows"][0]
    assert row["conditions"] == []
    assert row["node_fs"] == {
        "capacity_bytes": 0,
        "used_bytes": None,
        "available_bytes": None,
        "used_percent": None,
    }
    assert row["image_fs"] is None


@pytest.mark.parametrize("status", [403, 404, 500, 503])
async def test_disk_summary_failure_is_unknown_not_empty(status: int) -> None:
    cluster = _Cluster(
        nodes=[_node(conditions=[_condition("DiskPressure", "False")])],
        summary=_api_error(status),
    )
    result, _ = await _run("k8s_node_disk", cluster)
    row = result["rows"][0]
    assert row["summary"] == {"state": "unknown", "error": f"HTTP {status}"}
    assert row["node_fs"] is None
    assert row["image_fs"] is None
    assert row["conditions"][0]["type"] == "DiskPressure"


async def test_disk_summary_timeout_is_unknown() -> None:
    result, _ = await _run("k8s_node_disk", _Cluster(summary=TimeoutError()))
    assert result["rows"][0]["summary"] == {"state": "unknown", "error": "TimeoutError"}


@pytest.mark.parametrize("body", [None, [], {}, {"node": "x"}])
async def test_disk_odd_summary_answer_is_unknown(body: Any) -> None:
    result, _ = await _run("k8s_node_disk", _Cluster(summary=body))
    assert result["rows"][0]["summary"]["state"] == "unknown"
    assert result["rows"][0]["node_fs"] is None


async def test_disk_401_reaches_the_dispatcher() -> None:
    with pytest.raises(ApiException) as excinfo:
        await _run("k8s_node_disk", _Cluster(summary=_api_error(401)))
    assert excinfo.value.status == 401
    with pytest.raises(ApiException) as excinfo:
        await _run("k8s_node_disk", _Cluster(nodes=_api_error(401)))
    assert excinfo.value.status == 401


async def test_disk_node_list_failure_is_raised_not_empty() -> None:
    with pytest.raises(ApiException) as excinfo:
        await _run("k8s_node_disk", _Cluster(nodes=_api_error(500)))
    assert excinfo.value.status == 500


async def test_disk_named_node_is_read_by_name() -> None:
    cluster = _Cluster(read_node=_node("worker-9"))
    result, fakes = await _run("k8s_node_disk", cluster, {"node": "worker-9"})
    fakes.core.read_node.assert_awaited_once_with(name="worker-9")
    fakes.core.list_node.assert_not_awaited()
    assert result["rows"][0]["name"] == "worker-9"


async def test_disk_unknown_node_is_not_present() -> None:
    result, _ = await _run("k8s_node_disk", _Cluster(read_node=_api_error(404)), {"node": "gone"})
    assert result["rows"] == []
    assert result["total"] == 0
    assert result["node_state"] == "not present"


async def test_disk_empty_cluster_is_empty_rows() -> None:
    result, _ = await _run("k8s_node_disk", _Cluster(nodes=[]))
    assert result == {"rows": [], "total": 0, "incomplete": False}


async def test_disk_more_than_the_node_cap_is_flagged_incomplete() -> None:
    nodes = [_node(f"n{i}") for i in range(60)]
    result, fakes = await _run("k8s_node_disk", _Cluster(nodes=nodes))
    assert result["total"] == 50
    assert result["incomplete"] is True
    assert fakes.api_client.call_api.await_count == 50


async def test_disk_follows_node_pages() -> None:
    core = MagicMock()
    page1 = V1NodeList(items=[_node("a")], metadata=V1ListMeta(_continue="tok"))
    page2 = V1NodeList(items=[_node("b")], metadata=V1ListMeta())
    core.list_node = AsyncMock(side_effect=[page1, page2])
    api_client = MagicMock(close=AsyncMock(), call_api=AsyncMock(return_value=_summary()))
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=api_client,
        ),
        patch(f"{_MOD}.client.CoreV1Api", return_value=core),
    ):
        result = await _make_connector().k8s_node_disk(_operator(), _TARGET, {})
    assert [r["name"] for r in result["rows"]] == ["a", "b"]
    assert core.list_node.await_args_list[1].kwargs["_continue"] == "tok"


# ---------------------------------------------------------------------------
# k8s.node.images
# ---------------------------------------------------------------------------

_BIG = f"registry.example/big/app@{_BIG_DIGEST}"
_BIG_TAG = "registry.example/big/app:2.0"
_SMALL = f"registry.example/small/app@{_SMALL_DIGEST}"
_SMALL_TAG = "registry.example/small/app:1.0"
_OLD = f"registry.example/old/app@{_OLD_DIGEST}"
_OLD_TAG = "registry.example/old/app:0.9"


def _images_node() -> V1Node:
    return _node(
        images=[
            _image([_SMALL, _SMALL_TAG], 200 * 1024**2),
            _image([_BIG, _BIG_TAG], 118 * _GB // 10),
            _image([_OLD, _OLD_TAG], 500 * 1024**2),
        ]
    )


async def test_images_sorted_by_size_with_used_flag_and_totals() -> None:
    cluster = _Cluster(
        nodes=[_images_node()],
        pods=[
            _pod(_BIG_TAG, f"docker-pullable://registry.example/big/app@{_BIG_DIGEST}"),
            _pod(_SMALL_TAG, f"registry.example/small/app@{_SMALL_DIGEST}"),
        ],
    )
    result, fakes = await _run("k8s_node_images", cluster)

    assert [r["size_bytes"] for r in result["rows"]] == sorted(
        (r["size_bytes"] for r in result["rows"]), reverse=True
    )
    assert result["rows"][0]["names"] == [_BIG, _BIG_TAG]
    flags = {r["names"][1]: r["used_by_running_pod"] for r in result["rows"]}
    assert flags == {_BIG_TAG: True, _SMALL_TAG: True, _OLD_TAG: False}
    summary = result["nodes"][0]
    assert summary["image_count"] == 3
    assert summary["total_size_bytes"] == sum(r["size_bytes"] for r in result["rows"])
    assert summary["unused_count"] == 1
    assert summary["unused_size_bytes"] == 500 * 1024**2
    assert summary["pods"] == {"state": "present"}
    assert summary["list_may_be_cut"] is False
    assert result["total"] == 3
    kwargs = fakes.core.list_pod_for_all_namespaces.await_args.kwargs
    assert kwargs["field_selector"] == "spec.nodeName=worker-1"


async def test_images_matched_by_digest_when_the_pod_uses_another_name() -> None:
    cluster = _Cluster(
        nodes=[_images_node()],
        pods=[_pod("registry.example/mirror/app:latest", f"sha256:{'cc' * 32}")],
    )
    result, _ = await _run("k8s_node_images", cluster)
    flags = {r["names"][1]: r["used_by_running_pod"] for r in result["rows"]}
    assert flags[_OLD_TAG] is True
    assert flags[_BIG_TAG] is False


async def test_images_init_container_counts_as_use() -> None:
    cluster = _Cluster(nodes=[_images_node()], pods=[_pod(_OLD_TAG, init=True)])
    result, _ = await _run("k8s_node_images", cluster)
    flags = {r["names"][1]: r["used_by_running_pod"] for r in result["rows"]}
    assert flags[_OLD_TAG] is True


@pytest.mark.parametrize("phase", ["Succeeded", "Failed"])
async def test_images_of_finished_pods_are_not_in_use(phase: str) -> None:
    cluster = _Cluster(nodes=[_images_node()], pods=[_pod(_BIG_TAG, phase=phase)])
    result, _ = await _run("k8s_node_images", cluster)
    assert all(r["used_by_running_pod"] is False for r in result["rows"])
    assert result["nodes"][0]["unused_count"] == 3


async def test_images_pod_read_failure_is_unknown_never_unused() -> None:
    result, _ = await _run(
        "k8s_node_images", _Cluster(nodes=[_images_node()], pods=_api_error(403))
    )
    assert all(r["used_by_running_pod"] is None for r in result["rows"])
    summary = result["nodes"][0]
    assert summary["pods"] == {"state": "unknown", "error": "HTTP 403"}
    assert summary["unused_count"] is None
    assert summary["unused_size_bytes"] is None
    assert summary["image_count"] == 3


async def test_images_pod_read_timeout_is_unknown() -> None:
    result, _ = await _run("k8s_node_images", _Cluster(nodes=[_images_node()], pods=TimeoutError()))
    assert result["nodes"][0]["pods"] == {"state": "unknown", "error": "TimeoutError"}


async def test_images_pod_list_beyond_the_page_cap_is_unknown() -> None:
    core = MagicMock()
    core.list_node = AsyncMock(
        return_value=V1NodeList(items=[_images_node()], metadata=V1ListMeta())
    )
    core.list_pod_for_all_namespaces = AsyncMock(
        return_value=V1PodList(items=[], metadata=V1ListMeta(_continue="more"))
    )
    api_client = MagicMock(close=AsyncMock())
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=api_client,
        ),
        patch(f"{_MOD}.client.CoreV1Api", return_value=core),
    ):
        result = await _make_connector().k8s_node_images(_operator(), _TARGET, {})
    assert result["nodes"][0]["pods"]["state"] == "unknown"
    assert all(r["used_by_running_pod"] is None for r in result["rows"])


async def test_images_401_reaches_the_dispatcher() -> None:
    with pytest.raises(ApiException) as excinfo:
        await _run("k8s_node_images", _Cluster(nodes=[_images_node()], pods=_api_error(401)))
    assert excinfo.value.status == 401


async def test_images_edge_inputs_no_images_null_size_no_names() -> None:
    node = _node(images=[V1ContainerImage(names=None, size_bytes=None), _image(["x:1"], 5)])
    result, _ = await _run("k8s_node_images", _Cluster(nodes=[node]))
    assert [r["size_bytes"] for r in result["rows"]] == [5, None]
    assert result["rows"][1]["names"] == []
    assert result["rows"][1]["used_by_running_pod"] is False

    empty, _ = await _run("k8s_node_images", _Cluster(nodes=[_node()]))
    assert empty["rows"] == []
    assert empty["nodes"][0]["image_count"] == 0
    assert empty["nodes"][0]["total_size_bytes"] == 0


async def test_images_fifty_rows_flag_a_possibly_cut_list() -> None:
    node = _node(images=[_image([f"img:{i}"], i) for i in range(50)])
    result, _ = await _run("k8s_node_images", _Cluster(nodes=[node]))
    assert result["nodes"][0]["list_may_be_cut"] is True


async def test_images_same_image_on_two_nodes_is_judged_per_node() -> None:
    nodes = [
        _node("a", images=[_image([_BIG_TAG], 10)]),
        _node("b", images=[_image([_BIG_TAG], 10)]),
    ]
    core = MagicMock()
    core.list_node = AsyncMock(return_value=V1NodeList(items=nodes, metadata=V1ListMeta()))

    async def pods_for(**kwargs: Any) -> V1PodList:
        used = kwargs["field_selector"] == "spec.nodeName=a"
        return V1PodList(items=[_pod(_BIG_TAG)] if used else [], metadata=V1ListMeta())

    core.list_pod_for_all_namespaces = AsyncMock(side_effect=pods_for)
    api_client = MagicMock(close=AsyncMock())
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=api_client,
        ),
        patch(f"{_MOD}.client.CoreV1Api", return_value=core),
    ):
        result = await _make_connector().k8s_node_images(_operator(), _TARGET, {})
    assert {r["node"]: r["used_by_running_pod"] for r in result["rows"]} == {"a": True, "b": False}


async def test_images_unknown_node_is_not_present() -> None:
    result, _ = await _run("k8s_node_images", _Cluster(read_node=_api_error(404)), {"node": "gone"})
    assert result["rows"] == []
    assert result["nodes"] == []
    assert result["node_state"] == "not present"

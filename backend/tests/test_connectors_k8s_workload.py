# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the G3.2-T3 (#323) K8s workload ops.

Coverage matrix (per Issue #323 acceptance criteria):

* :data:`KUBERNETES_OPS` exposes the four new workload ops alongside
  the existing T1 / T2 / T5 surface; ``register_operations`` would land
  all rows in ``endpoint_descriptor``.
* :meth:`KubernetesConnector.k8s_pod_list` happy-path + cluster-wide
  + label-selector + field-selector + server-side pagination
  (``limit`` + ``continue_token`` + ``next_continue`` round-trip).
* :meth:`KubernetesConnector.k8s_pod_info` exact-match resolution +
  unique-prefix resolution + ambiguous-prefix error + not-found error.
* :meth:`KubernetesConnector.k8s_deployment_list` mirrors pod-list
  pagination shape against ``AppsV1Api``.
* :meth:`KubernetesConnector.k8s_deployment_info` prefix-resolution
  + status block + conditions list.
* Pure helpers (:func:`pod_row`, :func:`pod_info`, :func:`deployment_row`,
  :func:`deployment_info`, :func:`pod_ready_string`,
  :func:`container_status_row`) pinned against synthetic Kubernetes
  model objects.

The k3d integration shape lives in
:mod:`tests.integration.test_connectors_k8s_k3d`; this module
exercises the same contract with mocked
``kubernetes_asyncio.client.CoreV1Api`` / ``AppsV1Api`` so the gate
runs in every CI lane regardless of Docker availability.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client.models import (
    V1Container,
    V1ContainerPort,
    V1ContainerState,
    V1ContainerStateRunning,
    V1ContainerStateTerminated,
    V1ContainerStateWaiting,
    V1ContainerStatus,
    V1Deployment,
    V1DeploymentCondition,
    V1DeploymentList,
    V1DeploymentSpec,
    V1DeploymentStatus,
    V1DeploymentStrategy,
    V1EnvVar,
    V1LabelSelector,
    V1ListMeta,
    V1ObjectMeta,
    V1Pod,
    V1PodCondition,
    V1PodList,
    V1PodSpec,
    V1PodStatus,
    V1PodTemplateSpec,
    V1ResourceRequirements,
    V1RollingUpdateDeployment,
    V1Volume,
    V1VolumeMount,
)

from meho_backplane.connectors.kubernetes import (
    KUBERNETES_OPS,
    KubernetesConnector,
    KubernetesTargetLike,
)
from meho_backplane.connectors.kubernetes.ops_workload import (
    WORKLOAD_OPS,
    AmbiguousPrefixError,
    WorkloadNotFoundError,
    container_status_row,
    deployment_info,
    deployment_row,
    pod_info,
    pod_ready_string,
    pod_row,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Settings + registry fixtures (mirror test_connectors_k8s_core.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for the DB-touching path."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_kubernetes_registry() -> Iterator[None]:
    """Re-register :class:`KubernetesConnector` between tests.

    A prior test's autouse ``clear_registry()`` strips the K8s entries
    that ``connectors/kubernetes/__init__.py`` set; restore them so the
    dispatcher can resolve the connector during any forwarding tests.
    """
    clear_registry()
    register_connector("k8s", KubernetesConnector)
    register_connector_v2(
        product="k8s",
        version="1.x",
        impl_id="kubernetes-asyncio",
        cls=KubernetesConnector,
    )
    yield


# ---------------------------------------------------------------------------
# Target + connector stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str


_TARGET = _StubTarget(
    name="rke2-meho",
    host="rke2-meho.test.invalid",
    port=6443,
    secret_ref="kv/data/k8s/rke2-meho",
)


def _stub_kubeconfig() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "default",
        "contexts": [{"name": "default", "context": {"cluster": "c1", "user": "u1"}}],
        "clusters": [{"name": "c1", "cluster": {"server": "https://k8s.test:6443"}}],
        "users": [{"name": "u1", "user": {"token": "stub-token"}}],
    }


def _make_connector() -> KubernetesConnector:
    async def _loader(_target: KubernetesTargetLike) -> dict[str, Any]:
        return _stub_kubeconfig()

    return KubernetesConnector(kubeconfig_loader=_loader)


# ---------------------------------------------------------------------------
# Synthetic K8s model builders
# ---------------------------------------------------------------------------


def _make_container_status(
    *,
    name: str,
    ready: bool = True,
    restart_count: int = 0,
    state_label: str = "running",
) -> V1ContainerStatus:
    state_map = {
        "running": V1ContainerState(running=V1ContainerStateRunning(started_at=None)),
        "waiting": V1ContainerState(waiting=V1ContainerStateWaiting(reason="ContainerCreating")),
        "terminated": V1ContainerState(
            terminated=V1ContainerStateTerminated(exit_code=0, reason="Completed")
        ),
    }
    return V1ContainerStatus(
        name=name,
        image=f"{name}:1.0",
        image_id="sha256:abcdef",
        ready=ready,
        restart_count=restart_count,
        state=state_map[state_label],
    )


def _make_pod(
    *,
    name: str,
    namespace: str = "default",
    phase: str = "Running",
    containers: list[str] | None = None,
    container_statuses: list[V1ContainerStatus] | None = None,
    pod_ip: str | None = "10.0.0.1",
    host_ip: str | None = "192.168.1.1",
    node_name: str | None = "node-1",
    qos_class: str = "Guaranteed",
    created: datetime | None = None,
    labels: dict[str, str] | None = None,
    init_containers: list[str] | None = None,
    volumes: list[str] | None = None,
) -> V1Pod:
    container_names = containers if containers is not None else [name]
    container_objs = [V1Container(name=c, image=f"{c}:1.0") for c in container_names]
    init_objs = [V1Container(name=c, image=f"{c}:init") for c in (init_containers or [])]
    volume_objs = [V1Volume(name=v) for v in (volumes or [])]
    statuses = container_statuses
    if statuses is None:
        statuses = [_make_container_status(name=c) for c in container_names]
    return V1Pod(
        metadata=V1ObjectMeta(
            name=name,
            namespace=namespace,
            creation_timestamp=created,
            labels=labels,
        ),
        spec=V1PodSpec(
            containers=container_objs,
            init_containers=init_objs,
            volumes=volume_objs,
            node_name=node_name,
        ),
        status=V1PodStatus(
            phase=phase,
            container_statuses=statuses,
            init_container_statuses=[],
            conditions=[V1PodCondition(type="Ready", status="True")],
            pod_ip=pod_ip,
            host_ip=host_ip,
            qos_class=qos_class,
        ),
    )


def _make_deployment(
    *,
    name: str,
    namespace: str = "default",
    replicas_desired: int = 3,
    replicas_ready: int = 3,
    replicas_available: int = 3,
    image: str = "nginx:1.25",
    strategy_type: str = "RollingUpdate",
    rolling_max_surge: str | None = "25%",
    rolling_max_unavailable: str | None = "25%",
    created: datetime | None = None,
    labels: dict[str, str] | None = None,
    conditions: list[V1DeploymentCondition] | None = None,
) -> V1Deployment:
    template = V1PodTemplateSpec(
        metadata=V1ObjectMeta(labels={"app": name}),
        spec=V1PodSpec(containers=[V1Container(name="app", image=image)]),
    )
    rolling = None
    if strategy_type == "RollingUpdate":
        rolling = V1RollingUpdateDeployment(
            max_surge=rolling_max_surge,
            max_unavailable=rolling_max_unavailable,
        )
    strategy = V1DeploymentStrategy(type=strategy_type, rolling_update=rolling)
    return V1Deployment(
        metadata=V1ObjectMeta(
            name=name,
            namespace=namespace,
            creation_timestamp=created,
            labels=labels,
        ),
        spec=V1DeploymentSpec(
            replicas=replicas_desired,
            strategy=strategy,
            selector=V1LabelSelector(match_labels={"app": name}),
            template=template,
        ),
        status=V1DeploymentStatus(
            replicas=replicas_desired,
            ready_replicas=replicas_ready,
            available_replicas=replicas_available,
            updated_replicas=replicas_desired,
            unavailable_replicas=max(replicas_desired - replicas_available, 0),
            observed_generation=1,
            conditions=conditions or [],
        ),
    )


def _pod_list(pods: list[V1Pod], continue_token: str | None = None) -> V1PodList:
    return V1PodList(items=pods, metadata=V1ListMeta(_continue=continue_token))


def _deployment_list(
    deps: list[V1Deployment], continue_token: str | None = None
) -> V1DeploymentList:
    return V1DeploymentList(items=deps, metadata=V1ListMeta(_continue=continue_token))


def _patch_kubeconfig() -> Any:
    """Patch the kubeconfig client builder so the connector's cache wires up."""
    return patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        return_value=MagicMock(close=AsyncMock()),
    )


# ---------------------------------------------------------------------------
# Registration surface
# ---------------------------------------------------------------------------


def test_workload_ops_in_kubernetes_ops_tuple() -> None:
    """``KUBERNETES_OPS`` exposes the four T3 workload ops alongside the rest."""
    op_ids = {op.op_id for op in KUBERNETES_OPS}
    assert "k8s.pod.list" in op_ids
    assert "k8s.pod.info" in op_ids
    assert "k8s.deployment.list" in op_ids
    assert "k8s.deployment.info" in op_ids


def test_workload_ops_metadata_shape() -> None:
    """Every workload op is safe / no-approval / read-only / group=workload."""
    by_id = {op.op_id: op for op in WORKLOAD_OPS}
    for op_id in (
        "k8s.pod.list",
        "k8s.pod.info",
        "k8s.deployment.list",
        "k8s.deployment.info",
    ):
        op = by_id[op_id]
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert "read-only" in op.tags
        assert op.group_key == "workload"
        assert op.llm_instructions is not None


def test_workload_handler_attrs_resolve_to_async_methods() -> None:
    """Every workload op's ``handler_attr`` points at an async method on the connector."""
    import inspect

    for op in WORKLOAD_OPS:
        method = getattr(KubernetesConnector, op.handler_attr, None)
        assert method is not None, f"{op.op_id!r} declares missing handler {op.handler_attr!r}"
        assert inspect.iscoroutinefunction(method), (
            f"handler {op.handler_attr!r} for {op.op_id!r} must be ``async def``"
        )


def test_list_schemas_enforce_namespace_xor_all_namespaces() -> None:
    """Schema requires exactly one of {namespace, all_namespaces=true}."""
    from jsonschema import Draft202012Validator

    for op in WORKLOAD_OPS:
        if op.op_id not in {"k8s.pod.list", "k8s.deployment.list"}:
            continue
        validator = Draft202012Validator(op.parameter_schema)
        # Valid: namespace alone.
        assert validator.is_valid({"namespace": "argocd"})
        # Valid: all_namespaces=true alone.
        assert validator.is_valid({"all_namespaces": True})
        # Invalid: neither.
        assert not validator.is_valid({})
        # Invalid: both, with all_namespaces=true (conflict).
        assert not validator.is_valid({"namespace": "argocd", "all_namespaces": True})
        # Valid: both, with all_namespaces=false (effectively just namespace).
        assert validator.is_valid({"namespace": "argocd", "all_namespaces": False})


def test_info_schemas_require_name_and_namespace() -> None:
    """Schema rejects empty name / namespace inputs."""
    from jsonschema import Draft202012Validator

    pod_info_op = next(op for op in WORKLOAD_OPS if op.op_id == "k8s.pod.info")
    validator = Draft202012Validator(pod_info_op.parameter_schema)
    assert validator.is_valid({"pod_name": "argocd-server", "namespace": "argocd"})
    assert not validator.is_valid({"pod_name": "argocd-server"})  # missing namespace
    assert not validator.is_valid({"namespace": "argocd"})  # missing pod_name
    assert not validator.is_valid({"pod_name": "", "namespace": "argocd"})  # empty pod_name


# ---------------------------------------------------------------------------
# Pure row-shape helpers -- pod
# ---------------------------------------------------------------------------


def test_pod_ready_string_x_over_y_excluding_init() -> None:
    """``ready`` column counts main containers only, like kubectl."""
    pod = _make_pod(
        name="multi",
        containers=["app", "sidecar"],
        container_statuses=[
            _make_container_status(name="app", ready=True),
            _make_container_status(name="sidecar", ready=False),
        ],
        init_containers=["init-1"],
    )
    assert pod_ready_string(pod) == "1/2"


def test_pod_row_projects_full_shape() -> None:
    """The row dict carries every operator-facing list column."""
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    pod = _make_pod(
        name="argocd-server-x7r2k",
        namespace="argocd",
        phase="Running",
        containers=["argocd-server"],
        container_statuses=[
            _make_container_status(name="argocd-server", ready=True, restart_count=2)
        ],
        pod_ip="10.42.1.23",
        node_name="rke2-meho-01",
        created=now - timedelta(seconds=86400),
    )
    row = pod_row(pod, now=now)
    assert row == {
        "name": "argocd-server-x7r2k",
        "namespace": "argocd",
        "status": "Running",
        "ready": "1/1",
        "restarts": 2,
        "age_seconds": 86400,
        "node": "rke2-meho-01",
        "ip": "10.42.1.23",
    }


def test_pod_row_pending_pod_with_nil_node_and_ip() -> None:
    """A Pending pod with no scheduled node / IP surfaces ``None`` not crashes."""
    pod = _make_pod(
        name="pending",
        phase="Pending",
        node_name=None,
        pod_ip=None,
        container_statuses=[],
    )
    row = pod_row(pod)
    assert row["status"] == "Pending"
    assert row["node"] is None
    assert row["ip"] is None
    assert row["ready"] == "0/1"
    assert row["restarts"] == 0


def test_container_status_row_picks_state_branch() -> None:
    """The state label is the populated union branch's key."""
    cs_running = _make_container_status(name="app", state_label="running")
    cs_waiting = _make_container_status(name="app", state_label="waiting")
    cs_terminated = _make_container_status(name="app", state_label="terminated")
    assert container_status_row(cs_running)["state"] == "running"
    assert container_status_row(cs_waiting)["state"] == "waiting"
    assert container_status_row(cs_terminated)["state"] == "terminated"


def test_pod_info_carries_per_container_statuses_and_volumes() -> None:
    """``pod_info`` projects spec + status + container statuses + volumes."""
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    pod = V1Pod(
        metadata=V1ObjectMeta(
            name="argocd-server-x",
            namespace="argocd",
            creation_timestamp=now - timedelta(seconds=60),
            labels={"app": "argocd-server"},
        ),
        spec=V1PodSpec(
            containers=[
                V1Container(
                    name="argocd-server",
                    image="argocd:1.0",
                    ports=[V1ContainerPort(name="http", container_port=8080, protocol="TCP")],
                    env=[V1EnvVar(name="LOG_LEVEL", value="info")],
                    resources=V1ResourceRequirements(
                        requests={"cpu": "100m"}, limits={"cpu": "500m"}
                    ),
                    volume_mounts=[V1VolumeMount(name="cfg", mount_path="/etc/argocd")],
                )
            ],
            init_containers=[V1Container(name="init-cfg", image="busybox")],
            volumes=[V1Volume(name="cfg")],
            node_name="rke2-meho-01",
        ),
        status=V1PodStatus(
            phase="Running",
            pod_ip="10.0.0.5",
            host_ip="192.168.1.1",
            qos_class="Burstable",
            container_statuses=[
                _make_container_status(name="argocd-server", ready=True, restart_count=1)
            ],
            init_container_statuses=[
                _make_container_status(name="init-cfg", state_label="terminated")
            ],
            conditions=[V1PodCondition(type="Ready", status="True")],
        ),
    )

    info = pod_info(pod, now=now)
    assert info["name"] == "argocd-server-x"
    assert info["namespace"] == "argocd"
    assert info["status"] == "Running"
    assert info["node"] == "rke2-meho-01"
    assert info["ip"] == "10.0.0.5"
    assert info["qos_class"] == "Burstable"
    assert info["age_seconds"] == 60
    assert len(info["containers"]) == 1
    container = info["containers"][0]
    assert container["name"] == "argocd-server"
    assert container["ports"][0]["container_port"] == 8080
    assert container["env"][0] == {"name": "LOG_LEVEL", "value": "info"}
    assert container["resources"]["limits"]["cpu"] == "500m"
    assert container["volume_mounts"][0]["mount_path"] == "/etc/argocd"
    assert len(info["init_containers"]) == 1
    assert info["init_containers"][0]["name"] == "init-cfg"
    assert info["volumes"][0]["name"] == "cfg"
    assert info["container_statuses"][0]["ready"] is True
    assert info["container_statuses"][0]["restart_count"] == 1
    assert info["init_container_statuses"][0]["state"] == "terminated"
    assert info["conditions"][0]["type"] == "Ready"


# ---------------------------------------------------------------------------
# Pure row-shape helpers -- deployment
# ---------------------------------------------------------------------------


def test_deployment_row_projects_full_shape() -> None:
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    dep = _make_deployment(
        name="argocd-server",
        namespace="argocd",
        replicas_desired=3,
        replicas_ready=2,
        replicas_available=2,
        image="argocd:1.2",
        created=now - timedelta(seconds=3600),
    )
    row = deployment_row(dep, now=now)
    assert row == {
        "name": "argocd-server",
        "namespace": "argocd",
        "replicas_desired": 3,
        "replicas_ready": 2,
        "replicas_available": 2,
        "image": "argocd:1.2",
        "age_seconds": 3600,
        "strategy": "RollingUpdate",
    }


def test_deployment_row_zero_replica_fields_default_to_int_zero() -> None:
    """A brand-new deployment with no observed replicas yet reports 0, not None."""
    dep = V1Deployment(
        metadata=V1ObjectMeta(name="fresh", namespace="default"),
        spec=V1DeploymentSpec(
            replicas=3,
            selector=V1LabelSelector(match_labels={"app": "fresh"}),
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels={"app": "fresh"}),
                spec=V1PodSpec(containers=[V1Container(name="app", image="img:1")]),
            ),
        ),
        status=V1DeploymentStatus(),  # all None
    )
    row = deployment_row(dep)
    assert row["replicas_desired"] == 3
    assert row["replicas_ready"] == 0
    assert row["replicas_available"] == 0
    assert row["strategy"] is None


def test_deployment_info_carries_status_block_and_conditions() -> None:
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    cond = V1DeploymentCondition(
        type="Available",
        status="True",
        reason="MinimumReplicasAvailable",
        message="Deployment has minimum availability.",
    )
    dep = _make_deployment(
        name="argocd-server",
        namespace="argocd",
        replicas_desired=3,
        replicas_ready=3,
        replicas_available=3,
        image="argocd:1.2",
        created=now - timedelta(seconds=120),
        labels={"app": "argocd-server"},
        conditions=[cond],
    )
    info = deployment_info(dep, now=now)
    assert info["name"] == "argocd-server"
    assert info["namespace"] == "argocd"
    assert info["replicas_desired"] == 3
    assert info["age_seconds"] == 120
    assert info["labels"] == {"app": "argocd-server"}
    assert info["strategy"]["type"] == "RollingUpdate"
    assert info["strategy"]["rolling_update"]["max_surge"] == "25%"
    assert info["containers"][0]["image"] == "argocd:1.2"
    assert info["status"]["replicas"] == 3
    assert info["status"]["ready_replicas"] == 3
    assert info["status"]["available_replicas"] == 3
    assert info["status"]["updated_replicas"] == 3
    assert info["status"]["observed_generation"] == 1
    assert info["conditions"][0]["type"] == "Available"
    assert info["conditions"][0]["reason"] == "MinimumReplicasAvailable"


# ---------------------------------------------------------------------------
# k8s.pod.list handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_pod_list_returns_rows_and_total() -> None:
    """Handler wraps ``list_namespaced_pod`` and projects each row through ``pod_row``."""
    pods = [
        _make_pod(name="argocd-server-x", namespace="argocd"),
        _make_pod(name="argocd-repo-y", namespace="argocd"),
    ]
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespaced_pod = AsyncMock(return_value=_pod_list(pods))
        result = await connector.k8s_pod_list(_TARGET, {"namespace": "argocd"})

    assert result["total"] == 2
    assert {row["name"] for row in result["rows"]} == {"argocd-server-x", "argocd-repo-y"}
    assert "next_continue" not in result
    core_v1_cls.return_value.list_namespaced_pod.assert_awaited_once()
    kwargs = core_v1_cls.return_value.list_namespaced_pod.call_args.kwargs
    assert kwargs["namespace"] == "argocd"
    assert "label_selector" not in kwargs
    assert "limit" not in kwargs


@pytest.mark.asyncio
async def test_k8s_pod_list_all_namespaces_uses_for_all_namespaces() -> None:
    """``all_namespaces=true`` routes through ``list_pod_for_all_namespaces``."""
    pods = [
        _make_pod(name="p1", namespace="argocd"),
        _make_pod(name="p2", namespace="kube-system"),
    ]
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_pod_for_all_namespaces = AsyncMock(
            return_value=_pod_list(pods)
        )
        core_v1_cls.return_value.list_namespaced_pod = AsyncMock()
        result = await connector.k8s_pod_list(_TARGET, {"all_namespaces": True})

    assert result["total"] == 2
    core_v1_cls.return_value.list_pod_for_all_namespaces.assert_awaited_once()
    core_v1_cls.return_value.list_namespaced_pod.assert_not_awaited()


@pytest.mark.asyncio
async def test_k8s_pod_list_label_selector_flows_through() -> None:
    """``label_selector`` forwards to the API as ``label_selector=...``."""
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespaced_pod = AsyncMock(return_value=_pod_list([]))
        await connector.k8s_pod_list(
            _TARGET, {"namespace": "argocd", "label_selector": "app=argocd-server"}
        )
    kwargs = core_v1_cls.return_value.list_namespaced_pod.call_args.kwargs
    assert kwargs["label_selector"] == "app=argocd-server"


@pytest.mark.asyncio
async def test_k8s_pod_list_field_selector_flows_through() -> None:
    """``field_selector`` forwards verbatim to the API."""
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespaced_pod = AsyncMock(return_value=_pod_list([]))
        await connector.k8s_pod_list(
            _TARGET, {"namespace": "argocd", "field_selector": "status.phase=Running"}
        )
    kwargs = core_v1_cls.return_value.list_namespaced_pod.call_args.kwargs
    assert kwargs["field_selector"] == "status.phase=Running"


@pytest.mark.asyncio
async def test_k8s_pod_list_server_side_pagination_returns_next_continue() -> None:
    """``limit=N`` + server continuation produces ``next_continue`` in the result."""
    pods = [_make_pod(name=f"p{i}", namespace="argocd") for i in range(5)]
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespaced_pod = AsyncMock(
            return_value=_pod_list(pods, continue_token="page-2")
        )
        result = await connector.k8s_pod_list(_TARGET, {"namespace": "argocd", "limit": 5})

    assert result["total"] == 5
    assert result["next_continue"] == "page-2"
    kwargs = core_v1_cls.return_value.list_namespaced_pod.call_args.kwargs
    assert kwargs["limit"] == 5
    assert "_continue" not in kwargs  # no token passed in the request


@pytest.mark.asyncio
async def test_k8s_pod_list_continue_token_passed_to_api_as_underscore_continue() -> None:
    """Operator's ``continue_token`` maps to ``_continue=`` on the API call."""
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespaced_pod = AsyncMock(return_value=_pod_list([]))
        await connector.k8s_pod_list(_TARGET, {"namespace": "argocd", "continue_token": "abc123"})
    kwargs = core_v1_cls.return_value.list_namespaced_pod.call_args.kwargs
    assert kwargs["_continue"] == "abc123"


@pytest.mark.asyncio
async def test_k8s_pod_list_omits_next_continue_when_no_more_pages() -> None:
    """When the server returns no continuation token, the key is omitted."""
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespaced_pod = AsyncMock(
            return_value=_pod_list([_make_pod(name="solo", namespace="argocd")])
        )
        result = await connector.k8s_pod_list(_TARGET, {"namespace": "argocd"})
    assert "next_continue" not in result


# ---------------------------------------------------------------------------
# k8s.pod.info handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_pod_info_exact_match_returns_full_detail() -> None:
    """An exact name match returns the full info dict without a second round-trip."""
    pod = _make_pod(name="argocd-server", namespace="argocd")
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespaced_pod = AsyncMock(return_value=_pod_list([pod]))
        result = await connector.k8s_pod_info(
            _TARGET, {"pod_name": "argocd-server", "namespace": "argocd"}
        )
    assert result["name"] == "argocd-server"
    assert result["namespace"] == "argocd"
    assert "containers" in result


@pytest.mark.asyncio
async def test_k8s_pod_info_unique_prefix_resolves_to_one_pod() -> None:
    """A prefix that matches exactly one pod resolves to it."""
    pods = [
        _make_pod(name="argocd-server-7c4b8d-x7r2k", namespace="argocd"),
        _make_pod(name="argocd-repo-abc", namespace="argocd"),
    ]
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespaced_pod = AsyncMock(return_value=_pod_list(pods))
        result = await connector.k8s_pod_info(
            _TARGET, {"pod_name": "argocd-server", "namespace": "argocd"}
        )
    assert result["name"] == "argocd-server-7c4b8d-x7r2k"


@pytest.mark.asyncio
async def test_k8s_pod_info_ambiguous_prefix_raises() -> None:
    """A prefix matching multiple pods raises with the candidate list."""
    pods = [
        _make_pod(name="api-1", namespace="argocd"),
        _make_pod(name="api-2", namespace="argocd"),
    ]
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespaced_pod = AsyncMock(return_value=_pod_list(pods))
        with pytest.raises(AmbiguousPrefixError) as exc:
            await connector.k8s_pod_info(_TARGET, {"pod_name": "api", "namespace": "argocd"})
    assert exc.value.candidates == ["api-1", "api-2"]


@pytest.mark.asyncio
async def test_k8s_pod_info_not_found_raises() -> None:
    """A name matching nothing raises :class:`WorkloadNotFoundError`."""
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespaced_pod = AsyncMock(return_value=_pod_list([]))
        with pytest.raises(WorkloadNotFoundError):
            await connector.k8s_pod_info(_TARGET, {"pod_name": "ghost", "namespace": "argocd"})


@pytest.mark.asyncio
async def test_k8s_pod_info_exact_match_wins_over_prefix() -> None:
    """``foo-bar`` resolves to the exact-named pod even when ``foo-bar-x`` also exists."""
    pods = [
        _make_pod(name="api-1", namespace="argocd"),
        _make_pod(name="api-1-replica", namespace="argocd"),
    ]
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespaced_pod = AsyncMock(return_value=_pod_list(pods))
        result = await connector.k8s_pod_info(_TARGET, {"pod_name": "api-1", "namespace": "argocd"})
    assert result["name"] == "api-1"


# ---------------------------------------------------------------------------
# k8s.deployment.list handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_deployment_list_returns_rows_and_total() -> None:
    deps = [
        _make_deployment(name="argocd-server", namespace="argocd"),
        _make_deployment(name="argocd-repo-server", namespace="argocd"),
    ]
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.AppsV1Api") as apps_v1_cls,
    ):
        apps_v1_cls.return_value.list_namespaced_deployment = AsyncMock(
            return_value=_deployment_list(deps)
        )
        result = await connector.k8s_deployment_list(_TARGET, {"namespace": "argocd"})

    assert result["total"] == 2
    names = {row["name"] for row in result["rows"]}
    assert names == {"argocd-server", "argocd-repo-server"}
    assert "next_continue" not in result


@pytest.mark.asyncio
async def test_k8s_deployment_list_all_namespaces_routes_to_all_namespaces_api() -> None:
    deps = [
        _make_deployment(name="d1", namespace="argocd"),
        _make_deployment(name="d2", namespace="kube-system"),
    ]
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.AppsV1Api") as apps_v1_cls,
    ):
        apps_v1_cls.return_value.list_deployment_for_all_namespaces = AsyncMock(
            return_value=_deployment_list(deps)
        )
        apps_v1_cls.return_value.list_namespaced_deployment = AsyncMock()
        result = await connector.k8s_deployment_list(_TARGET, {"all_namespaces": True})
    assert result["total"] == 2
    apps_v1_cls.return_value.list_deployment_for_all_namespaces.assert_awaited_once()
    apps_v1_cls.return_value.list_namespaced_deployment.assert_not_awaited()


@pytest.mark.asyncio
async def test_k8s_deployment_list_pagination_returns_next_continue() -> None:
    deps = [_make_deployment(name=f"d{i}", namespace="argocd") for i in range(3)]
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.AppsV1Api") as apps_v1_cls,
    ):
        apps_v1_cls.return_value.list_namespaced_deployment = AsyncMock(
            return_value=_deployment_list(deps, continue_token="page-2")
        )
        result = await connector.k8s_deployment_list(_TARGET, {"namespace": "argocd", "limit": 3})
    assert result["next_continue"] == "page-2"
    kwargs = apps_v1_cls.return_value.list_namespaced_deployment.call_args.kwargs
    assert kwargs["limit"] == 3


# ---------------------------------------------------------------------------
# k8s.deployment.info handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_deployment_info_exact_match_returns_full_detail() -> None:
    dep = _make_deployment(name="argocd-server", namespace="argocd")
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.AppsV1Api") as apps_v1_cls,
    ):
        apps_v1_cls.return_value.list_namespaced_deployment = AsyncMock(
            return_value=_deployment_list([dep])
        )
        result = await connector.k8s_deployment_info(
            _TARGET, {"deployment_name": "argocd-server", "namespace": "argocd"}
        )
    assert result["name"] == "argocd-server"
    assert result["status"]["replicas"] == 3
    assert result["strategy"]["type"] == "RollingUpdate"


@pytest.mark.asyncio
async def test_k8s_deployment_info_prefix_resolves_to_one_deployment() -> None:
    deps = [
        _make_deployment(name="argocd-server", namespace="argocd"),
        _make_deployment(name="argocd-redis", namespace="argocd"),
    ]
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.AppsV1Api") as apps_v1_cls,
    ):
        apps_v1_cls.return_value.list_namespaced_deployment = AsyncMock(
            return_value=_deployment_list(deps)
        )
        result = await connector.k8s_deployment_info(
            _TARGET, {"deployment_name": "argocd-serv", "namespace": "argocd"}
        )
    assert result["name"] == "argocd-server"


@pytest.mark.asyncio
async def test_k8s_deployment_info_ambiguous_prefix_raises() -> None:
    deps = [
        _make_deployment(name="api-1", namespace="argocd"),
        _make_deployment(name="api-2", namespace="argocd"),
    ]
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.AppsV1Api") as apps_v1_cls,
    ):
        apps_v1_cls.return_value.list_namespaced_deployment = AsyncMock(
            return_value=_deployment_list(deps)
        )
        with pytest.raises(AmbiguousPrefixError) as exc:
            await connector.k8s_deployment_info(
                _TARGET, {"deployment_name": "api", "namespace": "argocd"}
            )
    assert exc.value.candidates == ["api-1", "api-2"]


@pytest.mark.asyncio
async def test_k8s_deployment_info_not_found_raises() -> None:
    connector = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_workload.client.AppsV1Api") as apps_v1_cls,
    ):
        apps_v1_cls.return_value.list_namespaced_deployment = AsyncMock(
            return_value=_deployment_list([])
        )
        with pytest.raises(WorkloadNotFoundError):
            await connector.k8s_deployment_info(
                _TARGET, {"deployment_name": "ghost", "namespace": "argocd"}
            )

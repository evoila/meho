# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the G0.14-T12 (#1201) K8s topology populator.

Coverage matrix (per Issue #1201 acceptance criteria, unit slice):

* :func:`build_target_node_hint` projects a :class:`VersionInfo` into
  the ``target``-kinded :class:`NodeHint` with the cluster's server
  version on ``properties`` (no extra round-trip vs. ``k8s.about``).
* :func:`namespace_node_hint` re-uses
  :func:`~meho_backplane.connectors.kubernetes.ops_core.namespace_row`
  so the populator and ``k8s.namespace.list`` share their wire shape.
* :func:`node_node_hint` re-uses
  :func:`~meho_backplane.connectors.kubernetes.ops_core.node_row` for
  the same reason.
* :func:`namespace_to_target_edge` / :func:`node_to_target_edge` emit
  ``belongs-to`` edges with the namespace / cluster-node as ``from``
  and the target as ``to``.
* :func:`build_topology_hints` assembles the full snapshot: 1 target
  + N namespaces + M nodes + (N + M) ``belongs-to`` edges. Objects
  with a missing ``metadata.name`` are skipped (defensive — the API
  server requires the field in practice).
* :meth:`KubernetesConnector.discover_topology` end-to-end against a
  mocked ApiClient: returns the cluster + namespaces + nodes
  :class:`TopologyHints` and accepts the refresh-private
  ``operator`` keyword argument (re-uses the per-target
  ``_get_api_client(target, operator)`` chain).

The k3s testcontainer slice lives in
``tests/integration/test_connectors_k8s_k3d.py`` — these unit tests
drive synthetic ``V1Namespace`` / ``V1Node`` / ``VersionInfo``
fixtures so the suite runs without Docker.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client.models import (
    V1Namespace,
    V1NamespaceList,
    V1NamespaceStatus,
    V1Node,
    V1NodeCondition,
    V1NodeList,
    V1NodeSpec,
    V1NodeStatus,
    V1NodeSystemInfo,
    V1ObjectMeta,
    VersionInfo,
)

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import (
    KubernetesConnector,
    KubernetesTargetLike,
)
from meho_backplane.connectors.kubernetes._topology import (
    build_target_node_hint,
    build_topology_hints,
    namespace_node_hint,
    namespace_to_target_edge,
    node_node_hint,
    node_to_target_edge,
)
from meho_backplane.connectors.schemas import TopologyHints
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for the DB-touching path."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Synthetic K8s model fixtures
# ---------------------------------------------------------------------------


def _make_version() -> VersionInfo:
    return VersionInfo(
        build_date="2026-01-01T00:00:00Z",
        compiler="gc",
        git_commit="abcdef0",
        git_tree_state="clean",
        git_version="v1.32.5+rke2r1",
        go_version="go1.22.0",
        major="1",
        minor="32",
        platform="linux/amd64",
    )


def _make_namespace(name: str, *, phase: str = "Active") -> V1Namespace:
    return V1Namespace(
        metadata=V1ObjectMeta(
            name=name,
            creation_timestamp=datetime(2026, 5, 1, tzinfo=UTC),
            labels={"k8s.io/managed-by": "rke2"},
        ),
        status=V1NamespaceStatus(phase=phase),
    )


def _make_node(name: str, *, roles: tuple[str, ...] = ("control-plane",)) -> V1Node:
    role_labels = {f"node-role.kubernetes.io/{role}": "" for role in roles}
    return V1Node(
        metadata=V1ObjectMeta(
            name=name,
            creation_timestamp=datetime(2026, 5, 1, tzinfo=UTC),
            labels=role_labels,
        ),
        status=V1NodeStatus(
            conditions=[V1NodeCondition(type="Ready", status="True")],
            node_info=V1NodeSystemInfo(
                architecture="amd64",
                boot_id="b",
                container_runtime_version="containerd://1.7",
                kernel_version="6.1.0-test",
                kube_proxy_version="v1.32.5+rke2r1",
                kubelet_version="v1.32.5+rke2r1",
                machine_id="m",
                operating_system="Linux",
                os_image="SLES Micro",
                system_uuid="u",
            ),
            addresses=[],
        ),
        spec=V1NodeSpec(taints=[]),
    )


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
    secret_ref="k8s/rke2-meho",
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
    async def _loader(_target: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return _stub_kubeconfig()

    return KubernetesConnector(kubeconfig_loader=_loader)


def _make_operator() -> Operator:
    return Operator(
        sub="op-topo-test",
        name="Topo Test Operator",
        email=None,
        raw_jwt="op.topo.jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# build_target_node_hint
# ---------------------------------------------------------------------------


def test_target_node_hint_carries_version_properties() -> None:
    version = _make_version()
    hint = build_target_node_hint("rke2-meho", version)
    assert hint.kind == "target"
    assert hint.name == "rke2-meho"
    assert hint.properties["git_version"] == "v1.32.5+rke2r1"
    assert hint.properties["major"] == "1"
    assert hint.properties["minor"] == "32"
    assert hint.properties["platform"] == "linux/amd64"


def test_target_node_hint_with_no_version_has_empty_properties() -> None:
    hint = build_target_node_hint("offline-cluster", None)
    assert hint.kind == "target"
    assert hint.name == "offline-cluster"
    assert dict(hint.properties) == {}


# ---------------------------------------------------------------------------
# namespace_node_hint
# ---------------------------------------------------------------------------


def test_namespace_node_hint_shares_namespace_row_shape() -> None:
    ns = _make_namespace("argocd")
    hint = namespace_node_hint(ns)
    assert hint.kind == "namespace"
    assert hint.name == "argocd"
    # Same fields as ``namespace_row`` minus ``name`` (which is the
    # NodeHint's own field). The wire shape stays uniform across the
    # populator and the ``k8s.namespace.list`` op.
    assert "status" in hint.properties
    assert "age_seconds" in hint.properties
    assert "labels" in hint.properties
    assert hint.properties["status"] == "Active"


def test_namespace_node_hint_name_is_string_even_when_metadata_is_partial() -> None:
    # Build a corrupt fixture where metadata.name is None — defensive
    # path. ``NodeHint.name`` is typed ``str`` so the helper coerces
    # ``None`` to ``""``; the caller filters these in
    # ``build_topology_hints`` so the empty-name path never lands in a
    # snapshot.
    ns = V1Namespace(metadata=V1ObjectMeta(name=None), status=V1NamespaceStatus(phase="Active"))
    hint = namespace_node_hint(ns)
    assert hint.name == ""


# ---------------------------------------------------------------------------
# node_node_hint
# ---------------------------------------------------------------------------


def test_node_node_hint_shares_node_row_shape() -> None:
    node = _make_node("ctrl-plane-1", roles=("control-plane", "etcd"))
    hint = node_node_hint(node)
    assert hint.kind == "node"
    assert hint.name == "ctrl-plane-1"
    assert hint.properties["roles"] == ["control-plane", "etcd"]
    assert hint.properties["version"] == "v1.32.5+rke2r1"
    assert hint.properties["kernel"] == "6.1.0-test"
    assert hint.properties["status"] == "Ready"


# ---------------------------------------------------------------------------
# Edge helpers
# ---------------------------------------------------------------------------


def test_namespace_to_target_edge_is_belongs_to() -> None:
    edge = namespace_to_target_edge("argocd", "rke2-meho")
    assert edge.kind == "belongs-to"
    assert edge.from_kind == "namespace"
    assert edge.from_name == "argocd"
    assert edge.to_kind == "target"
    assert edge.to_name == "rke2-meho"


def test_node_to_target_edge_is_belongs_to() -> None:
    edge = node_to_target_edge("ctrl-plane-1", "rke2-meho")
    assert edge.kind == "belongs-to"
    assert edge.from_kind == "node"
    assert edge.from_name == "ctrl-plane-1"
    assert edge.to_kind == "target"
    assert edge.to_name == "rke2-meho"


# ---------------------------------------------------------------------------
# build_topology_hints
# ---------------------------------------------------------------------------


def test_build_topology_hints_emits_target_namespaces_nodes_and_edges() -> None:
    namespaces = [_make_namespace("default"), _make_namespace("argocd")]
    nodes = [_make_node("ctrl-plane-1"), _make_node("worker-1", roles=("worker",))]

    hints = build_topology_hints(
        target_name="rke2-meho",
        version=_make_version(),
        namespaces=namespaces,
        nodes=nodes,
    )

    assert isinstance(hints, TopologyHints)
    # 1 target + 2 namespaces + 2 nodes.
    assert len(hints.nodes) == 5
    kinds = [n.kind for n in hints.nodes]
    assert kinds.count("target") == 1
    assert kinds.count("namespace") == 2
    assert kinds.count("node") == 2

    # 2 namespace-->target + 2 node-->target belongs-to edges.
    assert len(hints.edges) == 4
    assert all(e.kind == "belongs-to" for e in hints.edges)
    assert all(e.to_kind == "target" and e.to_name == "rke2-meho" for e in hints.edges)
    edge_endpoints = {(e.from_kind, e.from_name) for e in hints.edges}
    assert ("namespace", "default") in edge_endpoints
    assert ("namespace", "argocd") in edge_endpoints
    assert ("node", "ctrl-plane-1") in edge_endpoints
    assert ("node", "worker-1") in edge_endpoints


def test_build_topology_hints_skips_objects_with_no_metadata_name() -> None:
    # API server requires ``metadata.name`` so this is defensive only;
    # but the test pins the shape so a future bug in fixture-building
    # surfaces here rather than as an opaque "kind/name unique key
    # violated" error downstream in the refresh service.
    bogus_ns = V1Namespace(metadata=V1ObjectMeta(name=None), status=V1NamespaceStatus(phase="x"))
    good_ns = _make_namespace("default")
    hints = build_topology_hints(
        target_name="rke2-meho",
        version=_make_version(),
        namespaces=[bogus_ns, good_ns],
        nodes=[],
    )
    namespace_hints = [n for n in hints.nodes if n.kind == "namespace"]
    assert [h.name for h in namespace_hints] == ["default"]
    # No belongs-to edge for the skipped namespace.
    namespace_edges = [e for e in hints.edges if e.from_kind == "namespace"]
    assert [e.from_name for e in namespace_edges] == ["default"]


def test_build_topology_hints_stamps_discovered_at_at_call_time() -> None:
    before = datetime.now(UTC)
    hints = build_topology_hints(
        target_name="rke2-meho",
        version=None,
        namespaces=[],
        nodes=[],
    )
    after = datetime.now(UTC)
    assert before <= hints.discovered_at <= after
    # Even with zero namespaces and zero nodes, the cluster target node
    # is always emitted so the refresh service's diff/apply has an
    # anchor for the ``belongs-to`` edges future Tasks may add.
    assert len(hints.nodes) == 1
    assert hints.nodes[0].kind == "target"


# ---------------------------------------------------------------------------
# KubernetesConnector.discover_topology -- integration of the helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kubernetes_connector_discover_topology_returns_hints_with_operator() -> None:
    """``discover_topology`` issues 3 API calls and shapes the snapshot."""
    connector = _make_connector()

    version_resp = _make_version()
    namespaces_resp = V1NamespaceList(
        items=[_make_namespace("default"), _make_namespace("kube-system")]
    )
    nodes_resp = V1NodeList(items=[_make_node("ctrl-plane-1")])

    with (
        patch("meho_backplane.connectors.kubernetes.connector.client.VersionApi") as mock_version,
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as mock_core,
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new=AsyncMock(return_value=MagicMock()),
        ),
    ):
        mock_version.return_value.get_code = AsyncMock(return_value=version_resp)
        mock_core.return_value.list_namespace = AsyncMock(return_value=namespaces_resp)
        mock_core.return_value.list_node = AsyncMock(return_value=nodes_resp)

        hints = await connector.discover_topology(_TARGET, operator=_make_operator())

    assert isinstance(hints, TopologyHints)
    kinds = {(n.kind, n.name) for n in hints.nodes}
    assert ("target", "rke2-meho") in kinds
    assert ("namespace", "default") in kinds
    assert ("namespace", "kube-system") in kinds
    assert ("node", "ctrl-plane-1") in kinds
    # 2 namespaces + 1 node = 3 belongs-to edges.
    assert len(hints.edges) == 3
    assert all(e.kind == "belongs-to" and e.to_name == "rke2-meho" for e in hints.edges)


@pytest.mark.asyncio
async def test_kubernetes_connector_discover_topology_default_operator_is_system() -> None:
    """Omitting ``operator`` falls back to the synthesised system operator.

    The same fail-closed posture the fingerprint/probe paths use — a
    direct caller (test, future on-demand surface that doesn't yet
    thread the operator) still works closed-loop and the kubeconfig
    loader receives the system-operator placeholder JWT.
    """
    captured_operators: list[Operator] = []

    async def _loader(_target: KubernetesTargetLike, operator: Operator) -> dict[str, Any]:
        captured_operators.append(operator)
        return _stub_kubeconfig()

    connector = KubernetesConnector(kubeconfig_loader=_loader)

    with (
        patch("meho_backplane.connectors.kubernetes.connector.client.VersionApi") as mock_version,
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as mock_core,
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new=AsyncMock(return_value=MagicMock()),
        ),
    ):
        mock_version.return_value.get_code = AsyncMock(return_value=_make_version())
        mock_core.return_value.list_namespace = AsyncMock(return_value=V1NamespaceList(items=[]))
        mock_core.return_value.list_node = AsyncMock(return_value=V1NodeList(items=[]))

        hints = await connector.discover_topology(_TARGET)

    assert isinstance(hints, TopologyHints)
    # System operator's identifier surfaces in the kubeconfig loader so
    # the system-call carve-out (operator-context Vault read rejects
    # the placeholder JWT) keeps holding.
    assert len(captured_operators) == 1
    assert captured_operators[0].sub == "system:connector-probe"

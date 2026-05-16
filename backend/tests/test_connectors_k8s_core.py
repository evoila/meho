# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the G3.2-T2 (#322) K8s core inventory ops.

Coverage matrix (per Issue #322 acceptance criteria):

* :data:`KUBERNETES_OPS` exposes the three new core ops alongside the
  existing ``k8s.about``; ``register_operations`` lands all four rows
  in ``endpoint_descriptor``.
* :meth:`KubernetesConnector.k8s_namespace_list` projects every
  :class:`V1Namespace` through
  :func:`~meho_backplane.connectors.kubernetes.ops_core.namespace_row`
  and returns ``{rows, total}`` with the expected shape.
* :meth:`KubernetesConnector.k8s_node_list` projects every
  :class:`V1Node` through
  :func:`~meho_backplane.connectors.kubernetes.ops_core.node_row` --
  Ready-condition mapping, role-label derivation, internal-IP picking,
  taint flattening all covered.
* :meth:`KubernetesConnector.k8s_ls` three-way path dispatch:
  - root -> ``{namespaces, cluster_kinds}``.
  - namespace -> per-kind count via ``limit=1`` + remaining_item_count.
  - namespace/kind -> forwards through :meth:`execute` to
    ``k8s.<kind>.list``; an unregistered kind comes back as the shim's
    ``unknown_op`` envelope inside the ``result`` field.
* Pure helpers (:func:`age_seconds`, :func:`namespace_row`,
  :func:`node_row`, :func:`taint_row`) pinned against synthetic
  Kubernetes model objects.

The handle-pattern acceptance criterion ("50+ namespaces -> sample of
20 + handle returned") is **not** a unit-test target: the criterion
relies on a JSONFlux reducer that ships in a follow-on Initiative (the
v0.2 substrate's :class:`PassThroughReducer` never produces a handle).
The connector's contract is "emit raw rows + total"; the reducer's
contract is "spill set-shaped payloads". See the module docstring of
:mod:`~meho_backplane.connectors.kubernetes.ops_core` for the full
rationale.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client.models import (
    V1ListMeta,
    V1Namespace,
    V1NamespaceList,
    V1NamespaceStatus,
    V1Node,
    V1NodeAddress,
    V1NodeCondition,
    V1NodeList,
    V1NodeSpec,
    V1NodeStatus,
    V1NodeSystemInfo,
    V1ObjectMeta,
    V1Taint,
)

from meho_backplane.connectors.kubernetes import (
    KUBERNETES_OPS,
    KubernetesConnector,
    KubernetesTargetLike,
)
from meho_backplane.connectors.kubernetes.ops_core import (
    CORE_OPS,
    K8S_CLUSTER_KINDS,
    K8S_NAMESPACED_KIND_LISTERS,
    age_seconds,
    namespace_row,
    node_row,
    taint_row,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.connectors.schemas import OperationResult
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


@pytest.fixture(autouse=True)
def _clean_kubernetes_registry() -> Iterator[None]:
    """Re-register :class:`KubernetesConnector` between tests.

    ``test_connectors_registry.py`` (alphabetically earlier) has an
    autouse ``clear_registry()``; this fixture restores the K8s entries
    that ``connectors/kubernetes/__init__.py`` would have set so the
    dispatcher's resolver finds the connector during ``execute()``
    forwarding (k8s.ls subop dispatch).
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
# Target / connector fixtures
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
# Synthetic k8s model fixtures -- builders for the pure-helper tests
# ---------------------------------------------------------------------------


def _make_namespace(
    *,
    name: str,
    phase: str = "Active",
    created: datetime | None = None,
    labels: dict[str, str] | None = None,
) -> V1Namespace:
    return V1Namespace(
        metadata=V1ObjectMeta(
            name=name,
            creation_timestamp=created,
            labels=labels,
        ),
        status=V1NamespaceStatus(phase=phase),
    )


def _make_node(
    *,
    name: str,
    ready: str = "True",
    roles: list[str] | None = None,
    kubelet_version: str = "v1.28.5+rke2r1",
    kernel: str = "6.1.0-test",
    os_name: str = "Linux",
    internal_ip: str | None = "10.0.0.1",
    taints: list[V1Taint] | None = None,
    created: datetime | None = None,
) -> V1Node:
    role_labels: dict[str, str] = {}
    for role in roles or []:
        role_labels[f"node-role.kubernetes.io/{role}"] = ""
    addresses: list[V1NodeAddress] = []
    if internal_ip is not None:
        addresses.append(V1NodeAddress(address=internal_ip, type="InternalIP"))
    return V1Node(
        metadata=V1ObjectMeta(name=name, creation_timestamp=created, labels=role_labels),
        status=V1NodeStatus(
            conditions=[V1NodeCondition(type="Ready", status=ready)],
            node_info=V1NodeSystemInfo(
                architecture="amd64",
                boot_id="b",
                container_runtime_version="containerd://1.7",
                kernel_version=kernel,
                kube_proxy_version=kubelet_version,
                kubelet_version=kubelet_version,
                machine_id="m",
                operating_system=os_name,
                os_image="rancher/k3s",
                system_uuid="u",
            ),
            addresses=addresses,
        ),
        spec=V1NodeSpec(taints=taints or []),
    )


# ---------------------------------------------------------------------------
# Tests -- registration surface
# ---------------------------------------------------------------------------


def test_core_ops_in_kubernetes_ops_tuple() -> None:
    """``KUBERNETES_OPS`` now exposes the three core inventory ops + about."""
    op_ids = {op.op_id for op in KUBERNETES_OPS}
    assert "k8s.about" in op_ids
    assert "k8s.ls" in op_ids
    assert "k8s.namespace.list" in op_ids
    assert "k8s.node.list" in op_ids


def test_core_ops_metadata_shape() -> None:
    """Each core op declares safe / no-approval / read-only inventory tags."""
    by_id = {op.op_id: op for op in CORE_OPS}
    for op_id in ("k8s.ls", "k8s.namespace.list", "k8s.node.list"):
        op = by_id[op_id]
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert "read-only" in op.tags
        assert op.group_key == "inventory"
        assert op.llm_instructions is not None


def test_handler_attr_resolves_to_bound_method() -> None:
    """Every core op's ``handler_attr`` points at a real async method."""
    import inspect

    for op in CORE_OPS:
        method = getattr(KubernetesConnector, op.handler_attr, None)
        assert method is not None, f"{op.op_id!r} declares missing handler {op.handler_attr!r}"
        assert inspect.iscoroutinefunction(method), (
            f"handler {op.handler_attr!r} for {op.op_id!r} must be ``async def``"
        )


# ---------------------------------------------------------------------------
# Tests -- pure helper shapes
# ---------------------------------------------------------------------------


def test_age_seconds_returns_int_or_none() -> None:
    """``age_seconds`` returns ``int`` for tz-aware datetimes and ``None`` for missing."""
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    assert age_seconds(now - timedelta(seconds=10), now=now) == 10
    assert age_seconds(now - timedelta(seconds=12, milliseconds=400), now=now) == 12
    assert age_seconds(None, now=now) is None


def test_namespace_row_includes_phase_and_labels() -> None:
    """Pure mapping returns the wire dict shape."""
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    ns = _make_namespace(
        name="argocd",
        phase="Active",
        created=now - timedelta(seconds=3600),
        labels={"kubernetes.io/metadata.name": "argocd"},
    )
    row = namespace_row(ns, now=now)
    assert row == {
        "name": "argocd",
        "status": "Active",
        "age_seconds": 3600,
        "labels": {"kubernetes.io/metadata.name": "argocd"},
    }


def test_namespace_row_handles_missing_labels() -> None:
    """``labels=None`` on the API object surfaces as ``{}`` on the wire."""
    ns = _make_namespace(name="default", phase="Active", labels=None)
    row = namespace_row(ns)
    assert row["labels"] == {}


def test_namespace_row_terminating_phase_surfaces_verbatim() -> None:
    """A namespace mid-deletion reports its phase, not a coerced 'Unknown'."""
    ns = _make_namespace(name="going-away", phase="Terminating")
    assert namespace_row(ns)["status"] == "Terminating"


def test_taint_row_flattens_to_string_dict() -> None:
    """Taints land as ``{key, value, effect}``."""
    taint = V1Taint(key="node-role.kubernetes.io/control-plane", value="", effect="NoSchedule")
    row = taint_row(taint)
    assert row == {
        "key": "node-role.kubernetes.io/control-plane",
        "value": "",
        "effect": "NoSchedule",
    }


def test_node_row_ready_status_mapped() -> None:
    """A ``Ready=True`` condition surfaces as ``status='Ready'``."""
    node = _make_node(name="cp-01", ready="True", roles=["control-plane", "etcd"])
    row = node_row(node)
    assert row["status"] == "Ready"
    assert row["roles"] == ["control-plane", "etcd"]  # sorted
    assert row["version"] == "v1.28.5+rke2r1"


def test_node_row_not_ready_status_mapped() -> None:
    """A ``Ready=False`` condition surfaces as ``status='NotReady'``."""
    node = _make_node(name="cp-01", ready="False")
    assert node_row(node)["status"] == "NotReady"


def test_node_row_unknown_status_when_no_ready_condition() -> None:
    """A node with no Ready condition (synthetic edge case) reports 'Unknown'."""
    node = V1Node(
        metadata=V1ObjectMeta(name="orphan"),
        status=V1NodeStatus(conditions=[V1NodeCondition(type="MemoryPressure", status="False")]),
        spec=V1NodeSpec(taints=[]),
    )
    assert node_row(node)["status"] == "Unknown"


def test_node_row_legacy_kubernetes_io_role_label() -> None:
    """Older clusters using ``kubernetes.io/role`` map into the roles list."""
    node = V1Node(
        metadata=V1ObjectMeta(
            name="legacy-cp",
            labels={"kubernetes.io/role": "master"},
        ),
        status=V1NodeStatus(
            conditions=[V1NodeCondition(type="Ready", status="True")],
            node_info=V1NodeSystemInfo(
                architecture="amd64",
                boot_id="b",
                container_runtime_version="docker://20",
                kernel_version="4.19",
                kube_proxy_version="v1.20.0",
                kubelet_version="v1.20.0",
                machine_id="m",
                operating_system="Linux",
                os_image="ubuntu",
                system_uuid="u",
            ),
            addresses=[V1NodeAddress(address="192.168.1.1", type="InternalIP")],
        ),
        spec=V1NodeSpec(taints=[]),
    )
    assert node_row(node)["roles"] == ["master"]


def test_node_row_taints_flattened() -> None:
    """``spec.taints`` flows through ``taint_row`` per taint."""
    taints = [
        V1Taint(key="dedicated", value="gpu", effect="NoSchedule"),
        V1Taint(key="node-role.kubernetes.io/control-plane", value="", effect="NoSchedule"),
    ]
    node = _make_node(name="cp-01", taints=taints)
    rows = node_row(node)["taints"]
    assert len(rows) == 2
    assert rows[0]["key"] == "dedicated"
    assert rows[1]["effect"] == "NoSchedule"


def test_node_row_internal_ip_picked_from_addresses() -> None:
    """Only ``InternalIP`` addresses populate the ``internal_ip`` field."""
    node = V1Node(
        metadata=V1ObjectMeta(name="multi-addr"),
        status=V1NodeStatus(
            conditions=[V1NodeCondition(type="Ready", status="True")],
            node_info=V1NodeSystemInfo(
                architecture="amd64",
                boot_id="b",
                container_runtime_version="containerd://1.7",
                kernel_version="6.1",
                kube_proxy_version="v1.28",
                kubelet_version="v1.28",
                machine_id="m",
                operating_system="Linux",
                os_image="rke2",
                system_uuid="u",
            ),
            addresses=[
                V1NodeAddress(address="1.2.3.4", type="ExternalIP"),
                V1NodeAddress(address="10.0.0.5", type="InternalIP"),
                V1NodeAddress(address="node-1", type="Hostname"),
            ],
        ),
        spec=V1NodeSpec(taints=[]),
    )
    assert node_row(node)["internal_ip"] == "10.0.0.5"


def test_node_row_no_internal_ip_yields_none() -> None:
    """Nodes without an InternalIP address surface ``internal_ip=None``."""
    node = _make_node(name="bare", internal_ip=None)
    assert node_row(node)["internal_ip"] is None


# ---------------------------------------------------------------------------
# Tests -- k8s.namespace.list handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_namespace_list_returns_rows_and_total() -> None:
    """Handler wraps ``list_namespace`` and projects each row through ``namespace_row``."""
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    age = timedelta(seconds=8400000)
    namespaces = [
        _make_namespace(name="argocd", phase="Active", created=now - age),
        _make_namespace(name="kube-system", phase="Active", created=now - age),
    ]
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespace = AsyncMock(
            return_value=V1NamespaceList(items=namespaces, metadata=V1ListMeta())
        )
        result = await connector.k8s_namespace_list(_TARGET, {})

    assert result["total"] == 2
    assert len(result["rows"]) == 2
    assert {r["name"] for r in result["rows"]} == {"argocd", "kube-system"}
    for row in result["rows"]:
        assert row["status"] == "Active"
        assert isinstance(row["age_seconds"], int)


@pytest.mark.asyncio
async def test_k8s_namespace_list_empty_cluster() -> None:
    """A cluster with no namespaces yields ``rows=[]``, ``total=0``."""
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespace = AsyncMock(
            return_value=V1NamespaceList(items=[], metadata=V1ListMeta())
        )
        result = await connector.k8s_namespace_list(_TARGET, {})
    assert result == {"rows": [], "total": 0}


# ---------------------------------------------------------------------------
# Tests -- k8s.node.list handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_node_list_returns_rows_and_total() -> None:
    """Handler projects each node through ``node_row``."""
    nodes = [
        _make_node(name="cp-01", ready="True", roles=["control-plane", "etcd"]),
        _make_node(name="worker-01", ready="True", roles=["worker"]),
    ]
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_node = AsyncMock(
            return_value=V1NodeList(items=nodes, metadata=V1ListMeta())
        )
        result = await connector.k8s_node_list(_TARGET, {})

    assert result["total"] == 2
    names = [row["name"] for row in result["rows"]]
    assert names == ["cp-01", "worker-01"]
    assert result["rows"][0]["roles"] == ["control-plane", "etcd"]
    assert result["rows"][1]["roles"] == ["worker"]


# ---------------------------------------------------------------------------
# Tests -- k8s.ls root view
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_ls_root_returns_namespaces_and_cluster_kinds() -> None:
    """``k8s.ls /`` returns sorted namespace names + the fixed cluster-kind list."""
    namespaces = [
        _make_namespace(name="zebra"),
        _make_namespace(name="argocd"),
        _make_namespace(name="kube-system"),
    ]
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespace = AsyncMock(
            return_value=V1NamespaceList(items=namespaces, metadata=V1ListMeta())
        )
        result = await connector.k8s_ls(_TARGET, {"path": "/"})

    assert result["path"] == "/"
    assert result["namespaces"] == ["argocd", "kube-system", "zebra"]
    assert result["cluster_kinds"] == list(K8S_CLUSTER_KINDS)


@pytest.mark.asyncio
async def test_k8s_ls_default_path_is_root() -> None:
    """Missing ``path`` is equivalent to ``path='/'``."""
    namespaces = [_make_namespace(name="default")]
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespace = AsyncMock(
            return_value=V1NamespaceList(items=namespaces, metadata=V1ListMeta())
        )
        result = await connector.k8s_ls(_TARGET, {})  # no path
    assert result["path"] == "/"
    assert result["namespaces"] == ["default"]


# ---------------------------------------------------------------------------
# Tests -- k8s.ls /<namespace> view -- per-kind count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_ls_namespace_counts_via_remaining_item_count() -> None:
    """Per-kind count = inline items + ``remaining_item_count`` (avoids full pull)."""
    connector = _make_connector()
    # Build one fake response per kind in K8S_NAMESPACED_KIND_LISTERS so the
    # handler can iterate without raising on a missing attr. The numeric
    # counts differ per kind so the assertions can identify which kind
    # produced which value.
    counts_by_kind = {
        "pods": 7,
        "services": 2,
        "configmaps": 5,
        "events": 100,
        "persistentvolumeclaims": 0,
    }
    # The mocked list responses: limit=1, so ``items`` has 0 or 1 entry and
    # ``remaining_item_count`` carries the rest.
    fake_methods: dict[str, AsyncMock] = {}
    for kind_label, method_name in K8S_NAMESPACED_KIND_LISTERS:
        total = counts_by_kind[kind_label]
        if total == 0:
            inline_items: list[Any] = []
            remaining = 0
        else:
            inline_items = [MagicMock()]
            remaining = total - 1
        resp = MagicMock()
        resp.items = inline_items
        resp.metadata = V1ListMeta(remaining_item_count=remaining)
        fake_methods[method_name] = AsyncMock(return_value=resp)

    core_v1_instance = MagicMock()
    for method_name, mock in fake_methods.items():
        setattr(core_v1_instance, method_name, mock)
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch(
            "meho_backplane.connectors.kubernetes.connector.client.CoreV1Api",
            return_value=core_v1_instance,
        ),
    ):
        result = await connector.k8s_ls(_TARGET, {"path": "/argocd"})

    assert result["path"] == "/argocd"
    assert result["namespace"] == "argocd"
    assert result["cluster_kinds_omitted"] is True
    kinds_by_label = {entry["kind"]: entry for entry in result["kinds"]}
    for kind_label, expected_total in counts_by_kind.items():
        assert kinds_by_label[kind_label]["count"] == expected_total

    # Every list call was issued with ``limit=1`` and the right namespace.
    for _kind_label, method_name in K8S_NAMESPACED_KIND_LISTERS:
        fake_methods[method_name].assert_awaited_once_with(namespace="argocd", limit=1)


@pytest.mark.asyncio
async def test_k8s_ls_namespace_kind_with_rbac_403_records_error() -> None:
    """A 403 on one kind doesn't poison the whole ``ls`` -- record per-kind error."""
    connector = _make_connector()
    core_v1_instance = MagicMock()
    forbidden = RuntimeError("403 Forbidden")
    for kind_label, method_name in K8S_NAMESPACED_KIND_LISTERS:
        if kind_label == "events":
            setattr(core_v1_instance, method_name, AsyncMock(side_effect=forbidden))
        else:
            resp = MagicMock()
            resp.items = []
            resp.metadata = V1ListMeta(remaining_item_count=0)
            setattr(core_v1_instance, method_name, AsyncMock(return_value=resp))
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch(
            "meho_backplane.connectors.kubernetes.connector.client.CoreV1Api",
            return_value=core_v1_instance,
        ),
    ):
        result = await connector.k8s_ls(_TARGET, {"path": "/argocd"})

    kinds_by_label = {entry["kind"]: entry for entry in result["kinds"]}
    assert kinds_by_label["events"]["count"] is None
    assert "RuntimeError" in kinds_by_label["events"]["error"]
    # The other kinds still produced zero counts -- the error didn't abort.
    assert kinds_by_label["pods"]["count"] == 0


# ---------------------------------------------------------------------------
# Tests -- k8s.ls /<namespace>/<kind> forwarding via execute()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_ls_namespace_kind_forwards_to_unknown_op_envelope() -> None:
    """Kinds whose ``list`` op isn't registered come back as ``unknown_op`` via the shim."""
    # ``k8s.pod.list`` isn't registered (T3 ships it). The shim returns the
    # dispatcher's structured ``unknown_op`` envelope; the forwarder
    # surfaces it verbatim under ``result``.
    from meho_backplane.operations import typed_register as tr_module

    connector = _make_connector()
    # Register only the T2 surface so the descriptor lookup is deterministic.
    with patch.object(
        tr_module,
        "encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    ):
        await KubernetesConnector.register_operations()

    result = await connector.k8s_ls(_TARGET, {"path": "/argocd/pods"})

    assert result["path"] == "/argocd/pods"
    assert result["forwarded_to"] == "k8s.pod.list"
    # The shim's ``unknown_op`` envelope -- serialised via ``model_dump(mode='json')``.
    inner = result["result"]
    assert inner["status"] == "error"
    assert inner["error"].startswith("unknown_op:")
    assert inner["extras"]["error_code"] == "unknown_op"


@pytest.mark.asyncio
async def test_k8s_ls_path_with_extra_segments_collapses_to_two_arg_forward() -> None:
    """``k8s.ls /ns/kind/extra`` collapses to the ``/ns/kind`` forwarder shape."""
    from meho_backplane.operations import typed_register as tr_module

    connector = _make_connector()
    with patch.object(
        tr_module,
        "encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    ):
        await KubernetesConnector.register_operations()

    result = await connector.k8s_ls(_TARGET, {"path": "/argocd/pods/extra/segments"})
    assert result["forwarded_to"] == "k8s.pod.list"
    # The path retained is the canonical 2-segment shape.
    assert result["path"] == "/argocd/pods"


# ---------------------------------------------------------------------------
# Tests -- _normalise_kind_to_singular (regression: PR #543 review B1)
# ---------------------------------------------------------------------------


def test_normalise_kind_to_singular_pins_every_irregular_plural() -> None:
    """Each entry in ``_PLURAL_TO_SINGULAR_KIND`` maps to its singular form."""
    from meho_backplane.connectors.kubernetes.connector import (
        _PLURAL_TO_SINGULAR_KIND,
        _normalise_kind_to_singular,
    )

    for plural, singular in _PLURAL_TO_SINGULAR_KIND.items():
        assert _normalise_kind_to_singular(plural) == singular


def test_normalise_kind_to_singular_is_idempotent_on_singular_kinds_ending_in_s() -> None:
    """Singular kinds ending in 's' must NOT have the trailing 's' stripped.

    Regression: PR #543 review finding B1. Before the fix, the
    strip-trailing-s default branch mangled ``ingress`` -> ``ingres`` and
    ``storageclass`` -> ``storageclas`` for operators who typed the
    singular form directly (``/argocd/ingress``). The guard now returns
    the kind unchanged when it already matches one of the mapped
    singular forms.
    """
    from meho_backplane.connectors.kubernetes.connector import (
        _PLURAL_TO_SINGULAR_KIND,
        _normalise_kind_to_singular,
    )

    for singular in _PLURAL_TO_SINGULAR_KIND.values():
        assert _normalise_kind_to_singular(singular) == singular, (
            f"singular kind {singular!r} should pass through unchanged"
        )
    # Spot-checks for the most operator-visible cases.
    assert _normalise_kind_to_singular("ingress") == "ingress"
    assert _normalise_kind_to_singular("storageclass") == "storageclass"
    assert _normalise_kind_to_singular("persistentvolume") == "persistentvolume"
    assert _normalise_kind_to_singular("persistentvolumeclaim") == "persistentvolumeclaim"


def test_normalise_kind_to_singular_strips_trailing_s_on_regular_plurals() -> None:
    """The strip-trailing-s default branch still handles the common case."""
    from meho_backplane.connectors.kubernetes.connector import (
        _normalise_kind_to_singular,
    )

    assert _normalise_kind_to_singular("pods") == "pod"
    assert _normalise_kind_to_singular("services") == "service"
    assert _normalise_kind_to_singular("configmaps") == "configmap"
    assert _normalise_kind_to_singular("nodes") == "node"
    assert _normalise_kind_to_singular("namespaces") == "namespace"


@pytest.mark.asyncio
async def test_k8s_ls_forwards_singular_ingress_path_without_mangling() -> None:
    """``/argocd/ingress`` (singular) forwards to ``k8s.ingress.list``, not ``k8s.ingres.list``.

    Regression: PR #543 review finding m2. The forwarder runs the
    operator-typed kind through :func:`_normalise_kind_to_singular`
    before composing the sub-op id; the B1 fix ensures the singular
    form survives the normalisation.
    """
    from meho_backplane.operations import typed_register as tr_module

    connector = _make_connector()
    with patch.object(
        tr_module,
        "encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    ):
        await KubernetesConnector.register_operations()

    result = await connector.k8s_ls(_TARGET, {"path": "/argocd/ingress"})

    assert result["path"] == "/argocd/ingress"
    assert result["forwarded_to"] == "k8s.ingress.list"


# ---------------------------------------------------------------------------
# Tests -- end-to-end through execute() shim (registered op dispatch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_namespace_list_via_dispatcher_shim_returns_ok() -> None:
    """`k8s.namespace.list` registered -> execute() resolves handler + returns ok."""
    from meho_backplane.operations import typed_register as tr_module
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    reset_handler_cache()
    with patch.object(
        tr_module,
        "encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    ):
        await KubernetesConnector.register_operations()

    connector = _make_connector()
    namespaces = [_make_namespace(name="default"), _make_namespace(name="argocd")]
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_namespace = AsyncMock(
            return_value=V1NamespaceList(items=namespaces, metadata=V1ListMeta())
        )
        result = await connector.execute(_TARGET, "k8s.namespace.list", {})

    assert isinstance(result, OperationResult)
    assert result.status == "ok"
    assert result.op_id == "k8s.namespace.list"
    payload = result.result
    assert isinstance(payload, dict)
    assert payload["total"] == 2


@pytest.mark.asyncio
async def test_node_list_via_dispatcher_shim_returns_ok() -> None:
    """`k8s.node.list` registered -> execute() routes through the shim."""
    from meho_backplane.operations import typed_register as tr_module
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    reset_handler_cache()
    with patch.object(
        tr_module,
        "encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    ):
        await KubernetesConnector.register_operations()

    connector = _make_connector()
    nodes = [_make_node(name="cp-01", roles=["control-plane"])]
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_v1_cls,
    ):
        core_v1_cls.return_value.list_node = AsyncMock(
            return_value=V1NodeList(items=nodes, metadata=V1ListMeta())
        )
        result = await connector.execute(_TARGET, "k8s.node.list", {})

    assert result.status == "ok"
    assert result.op_id == "k8s.node.list"
    payload = result.result
    assert isinstance(payload, dict)
    assert payload["rows"][0]["name"] == "cp-01"


@pytest.mark.asyncio
async def test_ls_via_dispatcher_shim_rejects_extra_params() -> None:
    """``k8s.ls`` declares ``additionalProperties=False``; junk params are rejected."""
    from meho_backplane.operations import typed_register as tr_module

    with patch.object(
        tr_module,
        "encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    ):
        await KubernetesConnector.register_operations()

    connector = _make_connector()
    result = await connector.execute(_TARGET, "k8s.ls", {"path": "/", "junk": True})
    assert result.status == "error"
    assert result.error is not None and result.error.startswith("invalid_params:")

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the K8s storage + custom-resource read tier (#2830).

Covers the six ops added by Initiative #2833 wave 2:

* ``k8s.storageclass.list`` / ``k8s.persistentvolume.list`` /
  ``k8s.persistentvolumeclaim.list`` (:mod:`ops_storage`)
* ``k8s.crd.list`` / ``k8s.cr.list`` / ``k8s.cr.info``
  (:mod:`ops_customresource`)

Two layers, matching the ops_core / ops_network discipline: pure
row-helper tests that pin the wire shape against synthetic
``kubernetes_asyncio`` models (or plain dicts, for CRs) without an event
loop, and handler tests that mock the API clients. Plus regression
guards for the ``cluster_kinds`` dead-end advertisements the tier
closes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client.models import (
    V1CustomResourceDefinition,
    V1CustomResourceDefinitionNames,
    V1CustomResourceDefinitionSpec,
    V1CustomResourceDefinitionVersion,
    V1ObjectMeta,
    V1ObjectReference,
    V1PersistentVolume,
    V1PersistentVolumeClaim,
    V1PersistentVolumeClaimSpec,
    V1PersistentVolumeClaimStatus,
    V1PersistentVolumeSpec,
    V1PersistentVolumeStatus,
    V1StorageClass,
)

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector, KubernetesTargetLike
from meho_backplane.connectors.kubernetes.ops import KUBERNETES_OPS
from meho_backplane.connectors.kubernetes.ops_core import K8S_CLUSTER_KINDS
from meho_backplane.connectors.kubernetes.ops_customresource import (
    CR_SPEC_EXCERPT_MAX_BYTES,
    CUSTOM_RESOURCE_OPS,
    crd_row,
    crd_version_row,
    custom_resource_row,
)
from meho_backplane.connectors.kubernetes.ops_storage import (
    STORAGE_OPS,
    persistentvolume_row,
    persistentvolumeclaim_row,
    storageclass_row,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Settings env the connector import chain reaches for.

    CI runs in a clean env; the KEYCLOAK_* / VAULT_ADDR triple must be
    set explicitly (see #743) or ``get_settings`` raises at import.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
        sub="op-storage-cr-test",
        name="Storage/CR Test Operator",
        email=None,
        raw_jwt="op.storage.jwt",
        tenant_id=__import__("uuid").UUID("00000000-0000-0000-0000-00000000b0b0"),
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# Model builders
# ---------------------------------------------------------------------------


def _make_storageclass(
    *,
    name: str = "ebs-fast",
    provisioner: str = "ebs.csi.aws.com",
    is_default: bool = False,
    reclaim_policy: str | None = "Delete",
    binding_mode: str | None = "WaitForFirstConsumer",
    allow_expansion: bool | None = True,
) -> V1StorageClass:
    annotations = {"storageclass.kubernetes.io/is-default-class": "true"} if is_default else None
    return V1StorageClass(
        metadata=V1ObjectMeta(name=name, annotations=annotations),
        provisioner=provisioner,
        reclaim_policy=reclaim_policy,
        volume_binding_mode=binding_mode,
        allow_volume_expansion=allow_expansion,
    )


def _make_pv(
    *,
    name: str = "pvc-abc",
    phase: str = "Bound",
    capacity: str | None = "10Gi",
    storage_class: str | None = "ebs-fast",
    claim_ns: str | None = "team-a",
    claim_name: str | None = "data-0",
) -> V1PersistentVolume:
    claim_ref = None
    if claim_name is not None:
        claim_ref = V1ObjectReference(namespace=claim_ns, name=claim_name)
    return V1PersistentVolume(
        metadata=V1ObjectMeta(name=name),
        spec=V1PersistentVolumeSpec(
            capacity={"storage": capacity} if capacity is not None else None,
            storage_class_name=storage_class,
            claim_ref=claim_ref,
            access_modes=["ReadWriteOnce"],
            persistent_volume_reclaim_policy="Delete",
        ),
        status=V1PersistentVolumeStatus(phase=phase),
    )


def _make_pvc(
    *,
    name: str = "data-0",
    namespace: str = "team-a",
    phase: str | None = "Bound",
    capacity: str | None = "10Gi",
    storage_class: str | None = "ebs-fast",
    volume_name: str | None = "pvc-abc",
) -> V1PersistentVolumeClaim:
    return V1PersistentVolumeClaim(
        metadata=V1ObjectMeta(name=name, namespace=namespace),
        spec=V1PersistentVolumeClaimSpec(
            storage_class_name=storage_class,
            volume_name=volume_name,
            access_modes=["ReadWriteOnce"],
        ),
        status=V1PersistentVolumeClaimStatus(
            phase=phase,
            capacity={"storage": capacity} if capacity is not None else None,
        ),
    )


def _make_crd() -> V1CustomResourceDefinition:
    return V1CustomResourceDefinition(
        spec=V1CustomResourceDefinitionSpec(
            group="metallb.io",
            names=V1CustomResourceDefinitionNames(kind="IPAddressPool", plural="ipaddresspools"),
            scope="Namespaced",
            versions=[
                V1CustomResourceDefinitionVersion(name="v1beta1", served=True, storage=True),
                V1CustomResourceDefinitionVersion(name="v1alpha1", served=False, storage=False),
            ],
        ),
    )


def _make_cr(*, namespace: str | None = "metallb-system", spec: Any = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": "default", "creationTimestamp": "2026-08-01T00:00:00Z"}
    if namespace is not None:
        metadata["namespace"] = namespace
    return {
        "apiVersion": "metallb.io/v1beta1",
        "kind": "IPAddressPool",
        "metadata": metadata,
        "spec": spec if spec is not None else {"addresses": ["192.168.1.0/24"]},
    }


# ---------------------------------------------------------------------------
# Registration surface
# ---------------------------------------------------------------------------


def test_storage_and_cr_ops_in_kubernetes_ops_tuple() -> None:
    op_ids = {op.op_id for op in KUBERNETES_OPS}
    assert {
        "k8s.storageclass.list",
        "k8s.persistentvolume.list",
        "k8s.persistentvolumeclaim.list",
        "k8s.crd.list",
        "k8s.cr.list",
        "k8s.cr.info",
    } <= op_ids


def test_storage_ops_are_safe_no_approval_reads() -> None:
    by_id = {op.op_id: op for op in STORAGE_OPS}
    for op_id in (
        "k8s.storageclass.list",
        "k8s.persistentvolume.list",
        "k8s.persistentvolumeclaim.list",
    ):
        op = by_id[op_id]
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert "read-only" in op.tags
        assert op.group_key == "storage"
        assert op.llm_instructions is not None


def test_custom_resource_ops_are_safe_no_approval_reads() -> None:
    by_id = {op.op_id: op for op in CUSTOM_RESOURCE_OPS}
    for op_id in ("k8s.crd.list", "k8s.cr.list", "k8s.cr.info"):
        op = by_id[op_id]
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert "read-only" in op.tags
        assert op.group_key == "custom_resources"
        assert op.llm_instructions is not None


def test_handler_attrs_resolve_to_async_methods() -> None:
    import inspect

    for op in (*STORAGE_OPS, *CUSTOM_RESOURCE_OPS):
        method = getattr(KubernetesConnector, op.handler_attr, None)
        assert method is not None, f"{op.op_id!r} declares missing handler {op.handler_attr!r}"
        assert inspect.iscoroutinefunction(method), f"{op.handler_attr!r} must be ``async def``"


def test_response_schemas_forbid_additional_properties() -> None:
    for op in (*STORAGE_OPS, *CUSTOM_RESOURCE_OPS):
        assert op.response_schema is not None
        assert op.response_schema.get("additionalProperties") is False


# ---------------------------------------------------------------------------
# Dead-end advertisement regression guards (acceptance criterion 3)
# ---------------------------------------------------------------------------


def test_every_cluster_kind_has_a_backing_list_op() -> None:
    """``k8s.ls /`` advertises these kinds; each must now have a real op."""
    op_ids = {op.op_id for op in KUBERNETES_OPS}
    kind_to_op = {
        "nodes": "k8s.node.list",
        "namespaces": "k8s.namespace.list",
        "persistentvolumes": "k8s.persistentvolume.list",
        "storageclasses": "k8s.storageclass.list",
    }
    for kind in K8S_CLUSTER_KINDS:
        assert kind in kind_to_op, f"{kind!r} advertised but not mapped to an op"
        assert kind_to_op[kind] in op_ids, f"{kind!r} is a dead-end advertisement"


@pytest.mark.asyncio
async def test_ls_forwards_persistentvolumeclaims_to_new_op() -> None:
    """``k8s.ls /<ns>/persistentvolumeclaims`` resolves to the new op, not unknown_op."""
    connector = _make_connector()
    forwarded = MagicMock()
    forwarded.model_dump.return_value = {"status": "ok"}
    connector.execute = AsyncMock(return_value=forwarded)  # type: ignore[method-assign]

    result = await connector._k8s_ls_namespace_kind(_TARGET, "team-a", "persistentvolumeclaims")

    connector.execute.assert_awaited_once_with(
        _TARGET, "k8s.persistentvolumeclaim.list", {"namespace": "team-a"}
    )
    assert result["forwarded_to"] == "k8s.persistentvolumeclaim.list"


# ---------------------------------------------------------------------------
# storageclass_row
# ---------------------------------------------------------------------------


def test_storageclass_row_flags_default_from_annotation() -> None:
    row = storageclass_row(_make_storageclass(name="ebs-fast", is_default=True))
    assert row["is_default"] is True
    assert row["name"] == "ebs-fast"


def test_storageclass_row_not_default_when_annotation_absent() -> None:
    row = storageclass_row(_make_storageclass(is_default=False))
    assert row["is_default"] is False


def test_storageclass_row_projects_provisioner_and_policy_fields() -> None:
    row = storageclass_row(
        _make_storageclass(
            provisioner="rancher.io/local-path",
            reclaim_policy="Delete",
            binding_mode="WaitForFirstConsumer",
            allow_expansion=False,
        )
    )
    assert row["provisioner"] == "rancher.io/local-path"
    assert row["reclaim_policy"] == "Delete"
    assert row["volume_binding_mode"] == "WaitForFirstConsumer"
    assert row["allow_expansion"] is False


# ---------------------------------------------------------------------------
# persistentvolume_row
# ---------------------------------------------------------------------------


def test_persistentvolume_row_bound_renders_claim_ref_ns_slash_name() -> None:
    row = persistentvolume_row(_make_pv(claim_ns="team-a", claim_name="data-0"))
    assert row["claim_ref"] == "team-a/data-0"
    assert row["phase"] == "Bound"
    assert row["capacity"] == "10Gi"
    assert row["storage_class"] == "ebs-fast"
    assert row["access_modes"] == ["ReadWriteOnce"]
    assert row["reclaim_policy"] == "Delete"


def test_persistentvolume_row_unbound_has_null_claim_ref() -> None:
    row = persistentvolume_row(_make_pv(phase="Available", claim_name=None))
    assert row["claim_ref"] is None
    assert row["phase"] == "Available"


# ---------------------------------------------------------------------------
# persistentvolumeclaim_row
# ---------------------------------------------------------------------------


def test_persistentvolumeclaim_row_bound_projects_capacity_and_volume() -> None:
    row = persistentvolumeclaim_row(_make_pvc(namespace="team-a", capacity="10Gi"))
    assert row["namespace"] == "team-a"
    assert row["status"] == "Bound"
    assert row["capacity"] == "10Gi"
    assert row["volume_name"] == "pvc-abc"
    assert row["storage_class"] == "ebs-fast"
    assert row["access_modes"] == ["ReadWriteOnce"]


def test_persistentvolumeclaim_row_pending_has_null_capacity() -> None:
    row = persistentvolumeclaim_row(_make_pvc(phase="Pending", capacity=None, volume_name=None))
    assert row["status"] == "Pending"
    assert row["capacity"] is None
    assert row["volume_name"] is None


# ---------------------------------------------------------------------------
# crd_row / crd_version_row
# ---------------------------------------------------------------------------


def test_crd_version_row_flat_shape() -> None:
    version = V1CustomResourceDefinitionVersion(name="v1beta1", served=True, storage=True)
    assert crd_version_row(version) == {"name": "v1beta1", "served": True, "storage": True}


def test_crd_row_projects_group_plural_scope_and_versions() -> None:
    row = crd_row(_make_crd())
    assert row["group"] == "metallb.io"
    assert row["kind"] == "IPAddressPool"
    assert row["plural"] == "ipaddresspools"
    assert row["scope"] == "Namespaced"
    assert [v["name"] for v in row["versions"]] == ["v1beta1", "v1alpha1"]
    assert row["versions"][0]["storage"] is True


# ---------------------------------------------------------------------------
# custom_resource_row + bounded spec excerpt (acceptance: "a test pins the bound")
# ---------------------------------------------------------------------------


def test_custom_resource_row_small_spec_is_full_json_not_truncated() -> None:
    row = custom_resource_row(_make_cr(spec={"addresses": ["192.168.1.0/24"]}))
    assert row["name"] == "default"
    assert row["namespace"] == "metallb-system"
    assert row["api_version"] == "metallb.io/v1beta1"
    assert row["kind"] == "IPAddressPool"
    assert row["spec_truncated"] is False
    assert json.loads(row["spec_excerpt"]) == {"addresses": ["192.168.1.0/24"]}


def test_custom_resource_row_large_spec_is_truncated_within_bound() -> None:
    """A spec over the byte cap sets spec_truncated and stays within the bound."""
    big_spec = {"addresses": ["10.0.0.0/24"] * 500}
    assert len(json.dumps(big_spec).encode("utf-8")) > CR_SPEC_EXCERPT_MAX_BYTES

    row = custom_resource_row(_make_cr(spec=big_spec))

    assert row["spec_truncated"] is True
    assert len(row["spec_excerpt"].encode("utf-8")) <= CR_SPEC_EXCERPT_MAX_BYTES


def test_custom_resource_row_no_spec_yields_none_excerpt() -> None:
    obj = {"apiVersion": "v1", "kind": "Foo", "metadata": {"name": "x"}}
    row = custom_resource_row(obj)
    assert row["spec_excerpt"] is None
    assert row["spec_truncated"] is False


def test_custom_resource_row_cluster_scoped_has_null_namespace() -> None:
    row = custom_resource_row(_make_cr(namespace=None))
    assert row["namespace"] is None


def test_custom_resource_row_drops_managed_fields_metadata() -> None:
    """Verbose managedFields must not leak into the bounded projection."""
    cr = _make_cr()
    cr["metadata"]["managedFields"] = [{"manager": "meho", "operation": "Apply"}] * 20
    cr["metadata"]["labels"] = {"app": "metallb"}
    row = custom_resource_row(cr)
    assert "managedFields" not in row
    assert "managed_fields" not in row
    assert row["labels"] == {"app": "metallb"}


# ---------------------------------------------------------------------------
# Handlers -- storage (mocked API clients)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_storageclass_list_returns_rows_and_total() -> None:
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.StorageV1Api") as sc_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = [
            _make_storageclass(name="ebs-fast", is_default=True),
            _make_storageclass(name="ebs-slow"),
        ]
        sc_cls.return_value.list_storage_class = AsyncMock(return_value=list_resp)
        result = await connector.k8s_storageclass_list(_make_operator(), _TARGET, {})

    assert result["total"] == 2
    assert result["rows"][0]["name"] == "ebs-fast"
    assert result["rows"][0]["is_default"] is True


@pytest.mark.asyncio
async def test_k8s_persistentvolume_list_returns_rows() -> None:
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = [
            _make_pv(name="pv-1"),
            _make_pv(name="pv-2", phase="Available", claim_name=None),
        ]
        core_cls.return_value.list_persistent_volume = AsyncMock(return_value=list_resp)
        result = await connector.k8s_persistentvolume_list(_make_operator(), _TARGET, {})

    assert result["total"] == 2
    assert result["rows"][1]["claim_ref"] is None


@pytest.mark.asyncio
async def test_k8s_pvc_list_namespaced_forwards_namespace() -> None:
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = [_make_pvc(name="data-0", namespace="team-a")]
        core_cls.return_value.list_namespaced_persistent_volume_claim = AsyncMock(
            return_value=list_resp
        )
        result = await connector.k8s_persistentvolumeclaim_list(
            _make_operator(), _TARGET, {"namespace": "team-a"}
        )
        core_cls.return_value.list_namespaced_persistent_volume_claim.assert_awaited_once_with(
            namespace="team-a"
        )

    assert result["total"] == 1
    assert result["rows"][0]["namespace"] == "team-a"


@pytest.mark.asyncio
async def test_k8s_pvc_list_all_namespaces_uses_cluster_api() -> None:
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CoreV1Api") as core_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = [
            _make_pvc(name="data-0", namespace="team-a"),
            _make_pvc(name="data-0", namespace="team-b"),
        ]
        core_cls.return_value.list_persistent_volume_claim_for_all_namespaces = AsyncMock(
            return_value=list_resp
        )
        result = await connector.k8s_persistentvolumeclaim_list(
            _make_operator(), _TARGET, {"all_namespaces": True}
        )
        core_cls.return_value.list_persistent_volume_claim_for_all_namespaces.assert_awaited_once_with()

    assert result["total"] == 2
    assert {r["namespace"] for r in result["rows"]} == {"team-a", "team-b"}


# ---------------------------------------------------------------------------
# Handlers -- custom resources (mocked API clients)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_crd_list_returns_rows() -> None:
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch(
            "meho_backplane.connectors.kubernetes.connector.client.ApiextensionsV1Api"
        ) as ext_cls,
    ):
        list_resp = MagicMock()
        list_resp.items = [_make_crd()]
        ext_cls.return_value.list_custom_resource_definition = AsyncMock(return_value=list_resp)
        result = await connector.k8s_crd_list(_make_operator(), _TARGET, {})

    assert result["total"] == 1
    assert result["rows"][0]["plural"] == "ipaddresspools"


@pytest.mark.asyncio
async def test_k8s_cr_list_namespaced_forwards_gvp_and_namespace() -> None:
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CustomObjectsApi") as co_cls,
    ):
        co_cls.return_value.list_namespaced_custom_object = AsyncMock(
            return_value={"items": [_make_cr()]}
        )
        result = await connector.k8s_cr_list(
            _make_operator(),
            _TARGET,
            {
                "group": "metallb.io",
                "version": "v1beta1",
                "plural": "ipaddresspools",
                "namespace": "metallb-system",
            },
        )
        co_cls.return_value.list_namespaced_custom_object.assert_awaited_once_with(
            group="metallb.io",
            version="v1beta1",
            namespace="metallb-system",
            plural="ipaddresspools",
        )

    assert result["total"] == 1
    assert result["rows"][0]["name"] == "default"


@pytest.mark.asyncio
async def test_k8s_cr_list_without_namespace_uses_cluster_api() -> None:
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CustomObjectsApi") as co_cls,
    ):
        co_cls.return_value.list_cluster_custom_object = AsyncMock(
            return_value={"items": [_make_cr(namespace=None)]}
        )
        result = await connector.k8s_cr_list(
            _make_operator(),
            _TARGET,
            {"group": "metallb.io", "version": "v1beta1", "plural": "ipaddresspools"},
        )
        co_cls.return_value.list_cluster_custom_object.assert_awaited_once_with(
            group="metallb.io", version="v1beta1", plural="ipaddresspools"
        )

    assert result["total"] == 1


@pytest.mark.asyncio
async def test_k8s_cr_list_missing_items_key_yields_empty_rows() -> None:
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CustomObjectsApi") as co_cls,
    ):
        co_cls.return_value.list_cluster_custom_object = AsyncMock(return_value={})
        result = await connector.k8s_cr_list(
            _make_operator(),
            _TARGET,
            {"group": "metallb.io", "version": "v1beta1", "plural": "ipaddresspools"},
        )

    assert result == {"rows": [], "total": 0}


@pytest.mark.asyncio
async def test_k8s_cr_info_namespaced_returns_single_object() -> None:
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CustomObjectsApi") as co_cls,
    ):
        co_cls.return_value.get_namespaced_custom_object = AsyncMock(return_value=_make_cr())
        result = await connector.k8s_cr_info(
            _make_operator(),
            _TARGET,
            {
                "group": "metallb.io",
                "version": "v1beta1",
                "plural": "ipaddresspools",
                "name": "default",
                "namespace": "metallb-system",
            },
        )
        co_cls.return_value.get_namespaced_custom_object.assert_awaited_once_with(
            group="metallb.io",
            version="v1beta1",
            namespace="metallb-system",
            plural="ipaddresspools",
            name="default",
        )

    # Single-object projection, not a rows/total envelope.
    assert "rows" not in result
    assert result["name"] == "default"
    assert json.loads(result["spec_excerpt"]) == {"addresses": ["192.168.1.0/24"]}


@pytest.mark.asyncio
async def test_k8s_cr_info_without_namespace_uses_cluster_api() -> None:
    connector = _make_connector()
    with (
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("meho_backplane.connectors.kubernetes.connector.client.CustomObjectsApi") as co_cls,
    ):
        co_cls.return_value.get_cluster_custom_object = AsyncMock(
            return_value=_make_cr(namespace=None)
        )
        result = await connector.k8s_cr_info(
            _make_operator(),
            _TARGET,
            {
                "group": "example.io",
                "version": "v1",
                "plural": "widgets",
                "name": "default",
            },
        )
        co_cls.return_value.get_cluster_custom_object.assert_awaited_once_with(
            group="example.io", version="v1", plural="widgets", name="default"
        )

    assert result["namespace"] is None

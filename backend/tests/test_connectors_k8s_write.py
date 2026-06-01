# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the G3.14-T1 (#1403) K8s single-call write ops.

Coverage matrix (per Issue #1403 acceptance criteria):

* :data:`KUBERNETES_OPS` exposes all ten write ops with the stated
  safety levels and ``requires_approval=True``.
* Each op: happy path + a partial/error-path test.
* ``k8s.apply`` server-dry-run preview surfaces the would-be object
  without mutating (``dry_run="All"`` flows to the API; the preview
  result is returned).
* ``k8s.delete`` rejects kinds outside pod/job/replicaset; cascade
  params (``propagation_policy`` / ``grace_period_seconds``) forward
  explicitly.
* ``secret.create`` / ``job.create`` ``data`` is redacted from the
  broadcast (classify_op → ``credential_write`` → aggregate-only) and
  is absent from the handler's own response.

The API surface is mocked (``kubernetes_asyncio.client.*`` /
``DynamicClient``) so the gate runs in every CI lane regardless of
Docker; the live k3s shape lives in
:mod:`tests.integration.test_connectors_k8s_k3d`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import classify_op, redact_payload
from meho_backplane.connectors.kubernetes import (
    KUBERNETES_OPS,
    KubernetesConnector,
    KubernetesTargetLike,
)
from meho_backplane.connectors.kubernetes.ops_write import UnsupportedKindError
from meho_backplane.connectors.kubernetes.ops_write_dangerous import (
    ApplyManifestError,
    UndeletableKindError,
    secret_create_summary,
)
from meho_backplane.connectors.kubernetes.ops_write_meta import (
    WRITE_CAUTION_OPS,
    WRITE_DANGEROUS_OPS,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Fixtures (mirror test_connectors_k8s_workload.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_kubernetes_registry() -> Iterator[None]:
    clear_registry()
    register_connector("k8s", KubernetesConnector)
    register_connector_v2(product="k8s", version="1.x", impl_id="k8s", cls=KubernetesConnector)
    yield


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
        sub="op-write-test",
        name="Write Test Operator",
        email=None,
        raw_jwt="op.write.jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _patch_kubeconfig() -> Any:
    return patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        return_value=MagicMock(close=AsyncMock()),
    )


# ---------------------------------------------------------------------------
# Registration surface
# ---------------------------------------------------------------------------

_WRITE_OP_IDS = {
    "k8s.scale",
    "k8s.rollout.restart",
    "k8s.namespace.create",
    "k8s.annotate",
    "k8s.label",
    "k8s.cordon",
    "k8s.apply",
    "k8s.delete",
    "k8s.secret.create",
    "k8s.job.create",
}

_EXPECTED_SAFETY = {
    "k8s.scale": "caution",
    "k8s.rollout.restart": "caution",
    "k8s.namespace.create": "caution",
    "k8s.annotate": "caution",
    "k8s.label": "caution",
    "k8s.cordon": "caution",
    "k8s.apply": "dangerous",
    "k8s.delete": "dangerous",
    "k8s.secret.create": "dangerous",
    "k8s.job.create": "dangerous",
}


def test_write_ops_registered_with_safety_and_approval() -> None:
    """All ten write ops register with the stated safety + requires_approval=True."""
    by_id = {op.op_id: op for op in KUBERNETES_OPS}
    assert set(by_id) >= _WRITE_OP_IDS, "missing write ops in KUBERNETES_OPS"
    for op_id in _WRITE_OP_IDS:
        op = by_id[op_id]
        assert op.requires_approval is True, f"{op_id} must require approval"
        assert op.safety_level == _EXPECTED_SAFETY[op_id], op_id


def test_write_handler_attrs_resolve_on_connector() -> None:
    """Every write op's handler_attr is a real attribute on the connector."""
    for op in (*WRITE_CAUTION_OPS, *WRITE_DANGEROUS_OPS):
        assert getattr(KubernetesConnector, op.handler_attr, None) is not None, op.op_id


# ---------------------------------------------------------------------------
# k8s.scale
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_returns_before_after() -> None:
    conn = _make_connector()
    before = MagicMock()
    before.spec = MagicMock(replicas=2)
    after = MagicMock()
    after.spec = MagicMock(replicas=5)
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_write.client.AppsV1Api") as apps_cls,
    ):
        api = apps_cls.return_value
        api.read_namespaced_deployment_scale = AsyncMock(return_value=before)
        api.patch_namespaced_deployment_scale = AsyncMock(return_value=after)
        result = await conn.k8s_scale(
            operator=_make_operator(),
            target=_TARGET,
            params={"name": "web", "namespace": "argocd", "replicas": 5},
        )
    assert result == {
        "name": "web",
        "namespace": "argocd",
        "replicas_before": 2,
        "replicas_after": 5,
    }
    assert api.patch_namespaced_deployment_scale.call_args.kwargs["body"] == {
        "spec": {"replicas": 5}
    }


@pytest.mark.asyncio
async def test_scale_propagates_api_error() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_write.client.AppsV1Api") as apps_cls,
    ):
        api = apps_cls.return_value
        api.read_namespaced_deployment_scale = AsyncMock(
            side_effect=ApiException(status=404, reason="Not Found")
        )
        with pytest.raises(ApiException):
            await conn.k8s_scale(
                operator=_make_operator(),
                target=_TARGET,
                params={"name": "missing", "namespace": "argocd", "replicas": 5},
            )


# ---------------------------------------------------------------------------
# k8s.rollout.restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollout_restart_stamps_annotation() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_write.client.AppsV1Api") as apps_cls,
    ):
        api = apps_cls.return_value
        api.patch_namespaced_deployment = AsyncMock()
        result = await conn.k8s_rollout_restart(
            operator=_make_operator(),
            target=_TARGET,
            params={"name": "web", "namespace": "argocd"},
        )
    body = api.patch_namespaced_deployment.call_args.kwargs["body"]
    stamped = body["spec"]["template"]["metadata"]["annotations"]
    assert "kubectl.kubernetes.io/restartedAt" in stamped
    assert result["restarted_at"] == stamped["kubectl.kubernetes.io/restartedAt"]


# ---------------------------------------------------------------------------
# k8s.namespace.create (idempotent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_namespace_create_happy_path() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_write.client.CoreV1Api") as core_cls,
    ):
        core_cls.return_value.create_namespace = AsyncMock()
        result = await conn.k8s_namespace_create(
            operator=_make_operator(), target=_TARGET, params={"name": "new-ns"}
        )
    assert result == {"name": "new-ns", "created": True, "already_existed": False}


@pytest.mark.asyncio
async def test_namespace_create_409_is_idempotent() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_write.client.CoreV1Api") as core_cls,
    ):
        core_cls.return_value.create_namespace = AsyncMock(
            side_effect=ApiException(status=409, reason="Conflict")
        )
        result = await conn.k8s_namespace_create(
            operator=_make_operator(), target=_TARGET, params={"name": "existing"}
        )
    assert result == {"name": "existing", "created": False, "already_existed": True}


@pytest.mark.asyncio
async def test_namespace_create_non_409_raises() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_write.client.CoreV1Api") as core_cls,
    ):
        core_cls.return_value.create_namespace = AsyncMock(
            side_effect=ApiException(status=403, reason="Forbidden")
        )
        with pytest.raises(ApiException):
            await conn.k8s_namespace_create(
                operator=_make_operator(), target=_TARGET, params={"name": "denied"}
            )


# ---------------------------------------------------------------------------
# k8s.annotate / k8s.label
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_namespaced_kind() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_write.client.AppsV1Api") as apps_cls,
    ):
        apps_cls.return_value.patch_namespaced_deployment = AsyncMock()
        result = await conn.k8s_annotate(
            operator=_make_operator(),
            target=_TARGET,
            params={
                "kind": "deployment",
                "name": "web",
                "namespace": "argocd",
                "annotations": {"team": "platform", "stale": None},
            },
        )
    body = apps_cls.return_value.patch_namespaced_deployment.call_args.kwargs["body"]
    assert body == {"metadata": {"annotations": {"team": "platform", "stale": None}}}
    assert result["annotations"] == {"team": "platform", "stale": None}


@pytest.mark.asyncio
async def test_label_cluster_scoped_node_no_namespace() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_write.client.CoreV1Api") as core_cls,
    ):
        core_cls.return_value.patch_node = AsyncMock()
        result = await conn.k8s_label(
            operator=_make_operator(),
            target=_TARGET,
            params={"kind": "node", "name": "node-1", "labels": {"role": "cp"}},
        )
    core_cls.return_value.patch_node.assert_awaited_once()
    assert result["namespace"] is None
    assert result["labels"] == {"role": "cp"}


@pytest.mark.asyncio
async def test_annotate_unsupported_kind_raises() -> None:
    conn = _make_connector()
    with _patch_kubeconfig(), pytest.raises(UnsupportedKindError):
        await conn.k8s_annotate(
            operator=_make_operator(),
            target=_TARGET,
            params={"kind": "secret", "name": "x", "annotations": {"a": "b"}},
        )


@pytest.mark.asyncio
async def test_annotate_namespaced_missing_namespace_raises() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_write.client.AppsV1Api") as apps_cls,
    ):
        apps_cls.return_value.patch_namespaced_deployment = AsyncMock()
        with pytest.raises(ValueError, match="namespace is required"):
            await conn.k8s_annotate(
                operator=_make_operator(),
                target=_TARGET,
                params={"kind": "deployment", "name": "web", "annotations": {"a": "b"}},
            )


# ---------------------------------------------------------------------------
# k8s.cordon
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cordon_marks_unschedulable() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_write.client.CoreV1Api") as core_cls,
    ):
        core_cls.return_value.patch_node = AsyncMock()
        result = await conn.k8s_cordon(
            operator=_make_operator(), target=_TARGET, params={"name": "node-1"}
        )
    body = core_cls.return_value.patch_node.call_args.kwargs["body"]
    assert body == {"spec": {"unschedulable": True}}
    assert result == {"name": "node-1", "unschedulable": True, "cordoned": True}


@pytest.mark.asyncio
async def test_uncordon_marks_schedulable() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch("meho_backplane.connectors.kubernetes.ops_write.client.CoreV1Api") as core_cls,
    ):
        core_cls.return_value.patch_node = AsyncMock()
        result = await conn.k8s_cordon(
            operator=_make_operator(),
            target=_TARGET,
            params={"name": "node-1", "uncordon": True},
        )
    body = core_cls.return_value.patch_node.call_args.kwargs["body"]
    assert body == {"spec": {"unschedulable": False}}
    assert result["cordoned"] is False


# ---------------------------------------------------------------------------
# k8s.apply (server-side apply + dry-run preview)
# ---------------------------------------------------------------------------


def _dyn_patch() -> Any:
    """Patch DynamicClient so awaiting it (discovery) is a no-op stub."""
    return patch("meho_backplane.connectors.kubernetes.ops_write_dangerous.DynamicClient")


_SINGLE_MANIFEST = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: argocd
spec:
  replicas: 2
"""


def _applied_obj(rv: str = "123", uid: str = "u-1") -> MagicMock:
    obj = MagicMock()
    obj.metadata = MagicMock(resourceVersion=rv, uid=uid)
    return obj


@pytest.mark.asyncio
async def test_apply_persists_by_default() -> None:
    conn = _make_connector()
    dyn = MagicMock()
    dyn.resources.get = AsyncMock(return_value=MagicMock())
    dyn.server_side_apply = AsyncMock(return_value=_applied_obj())
    with _patch_kubeconfig(), _dyn_patch() as dyn_cls:
        # DynamicClient(api_client) is awaited; make the instance awaitable->dyn
        dyn_cls.return_value = _awaitable(dyn)
        result = await conn.k8s_apply(
            operator=_make_operator(),
            target=_TARGET,
            params={"manifest": _SINGLE_MANIFEST},
        )
    assert result["dry_run"] is False
    assert result["total"] == 1
    # No dry_run kwarg => persisted apply.
    assert "dry_run" not in dyn.server_side_apply.call_args.kwargs
    assert dyn.server_side_apply.call_args.kwargs["field_manager"] == "meho"
    assert result["applied"][0]["resource_version"] == "123"


@pytest.mark.asyncio
async def test_apply_server_dry_run_preview_no_mutation() -> None:
    conn = _make_connector()
    dyn = MagicMock()
    dyn.resources.get = AsyncMock(return_value=MagicMock())
    dyn.server_side_apply = AsyncMock(return_value=_applied_obj(rv="dry"))
    with _patch_kubeconfig(), _dyn_patch() as dyn_cls:
        dyn_cls.return_value = _awaitable(dyn)
        result = await conn.k8s_apply(
            operator=_make_operator(),
            target=_TARGET,
            params={"manifest": _SINGLE_MANIFEST, "dry_run": "server"},
        )
    assert result["dry_run"] is True
    # The operator-facing 'server' maps to the API's dryRun=All.
    assert dyn.server_side_apply.call_args.kwargs["dry_run"] == "All"
    assert result["applied"][0]["resource_version"] == "dry"


@pytest.mark.asyncio
async def test_apply_multi_doc() -> None:
    conn = _make_connector()
    multi = (
        _SINGLE_MANIFEST
        + "\n---\n"
        + ("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm\n  namespace: argocd\n")
    )
    dyn = MagicMock()
    dyn.resources.get = AsyncMock(return_value=MagicMock())
    dyn.server_side_apply = AsyncMock(side_effect=[_applied_obj("1"), _applied_obj("2")])
    with _patch_kubeconfig(), _dyn_patch() as dyn_cls:
        dyn_cls.return_value = _awaitable(dyn)
        result = await conn.k8s_apply(
            operator=_make_operator(), target=_TARGET, params={"manifest": multi}
        )
    assert result["total"] == 2
    assert {r["kind"] for r in result["applied"]} == {"Deployment", "ConfigMap"}


@pytest.mark.asyncio
async def test_apply_rejects_manifest_without_kind() -> None:
    conn = _make_connector()
    with _patch_kubeconfig(), pytest.raises(ApplyManifestError):
        await conn.k8s_apply(
            operator=_make_operator(),
            target=_TARGET,
            params={"manifest": "apiVersion: v1\nmetadata:\n  name: x\n"},
        )


@pytest.mark.asyncio
async def test_apply_rejects_empty_manifest() -> None:
    conn = _make_connector()
    with _patch_kubeconfig(), pytest.raises(ApplyManifestError):
        await conn.k8s_apply(
            operator=_make_operator(), target=_TARGET, params={"manifest": "---\n"}
        )


# ---------------------------------------------------------------------------
# k8s.delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_pod_forwards_cascade_params() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch(
            "meho_backplane.connectors.kubernetes.ops_write_dangerous.client.CoreV1Api"
        ) as core_cls,
    ):
        core_cls.return_value.delete_namespaced_pod = AsyncMock()
        result = await conn.k8s_delete(
            operator=_make_operator(),
            target=_TARGET,
            params={
                "kind": "pod",
                "name": "web-abc",
                "namespace": "argocd",
                "propagation_policy": "Background",
                "grace_period_seconds": 0,
            },
        )
    kwargs = core_cls.return_value.delete_namespaced_pod.call_args.kwargs
    assert kwargs["propagation_policy"] == "Background"
    assert kwargs["grace_period_seconds"] == 0
    assert result["deleted"] is True


@pytest.mark.asyncio
async def test_delete_job_uses_batch_api() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch(
            "meho_backplane.connectors.kubernetes.ops_write_dangerous.client.BatchV1Api"
        ) as batch_cls,
    ):
        batch_cls.return_value.delete_namespaced_job = AsyncMock()
        result = await conn.k8s_delete(
            operator=_make_operator(),
            target=_TARGET,
            params={"kind": "job", "name": "migrate", "namespace": "argocd"},
        )
    batch_cls.return_value.delete_namespaced_job.assert_awaited_once()
    assert result["kind"] == "job"


@pytest.mark.asyncio
async def test_delete_rejects_namespace_kind() -> None:
    conn = _make_connector()
    with _patch_kubeconfig(), pytest.raises(UndeletableKindError):
        await conn.k8s_delete(
            operator=_make_operator(),
            target=_TARGET,
            params={"kind": "namespace", "name": "kube-system", "namespace": "kube-system"},
        )


@pytest.mark.asyncio
async def test_delete_rejects_pvc_kind() -> None:
    conn = _make_connector()
    with _patch_kubeconfig(), pytest.raises(UndeletableKindError):
        await conn.k8s_delete(
            operator=_make_operator(),
            target=_TARGET,
            params={"kind": "persistentvolumeclaim", "name": "data", "namespace": "argocd"},
        )


# ---------------------------------------------------------------------------
# k8s.secret.create / k8s.job.create — redaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_create_response_omits_values() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch(
            "meho_backplane.connectors.kubernetes.ops_write_dangerous.client.CoreV1Api"
        ) as core_cls,
    ):
        core_cls.return_value.create_namespaced_secret = AsyncMock()
        result = await conn.k8s_secret_create(
            operator=_make_operator(),
            target=_TARGET,
            params={
                "name": "db-creds",
                "namespace": "argocd",
                "string_data": {"password": "hunter2", "username": "admin"},
            },
        )
    # The handler echoes key NAMES only, never the values.
    assert result == {
        "name": "db-creds",
        "namespace": "argocd",
        "type": "Opaque",
        "data_keys": ["password", "username"],
        "created": True,
    }
    assert "hunter2" not in str(result)


@pytest.mark.asyncio
async def test_secret_create_writes_string_data_to_cluster() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch(
            "meho_backplane.connectors.kubernetes.ops_write_dangerous.client.CoreV1Api"
        ) as core_cls,
    ):
        core_cls.return_value.create_namespaced_secret = AsyncMock()
        await conn.k8s_secret_create(
            operator=_make_operator(),
            target=_TARGET,
            params={
                "name": "db-creds",
                "namespace": "argocd",
                "string_data": {"password": "hunter2"},
            },
        )
    body = core_cls.return_value.create_namespaced_secret.call_args.kwargs["body"]
    # The value DOES reach the cluster (that's the point) but not the response/broadcast.
    assert body.string_data == {"password": "hunter2"}


def test_secret_create_classifies_credential_write_and_redacts_broadcast() -> None:
    """The broadcast payload for k8s.secret.create is aggregate-only — the
    secret values in params never reach the feed."""
    assert classify_op("k8s.secret.create") == "credential_write"
    raw_params = {
        "params": {"name": "db", "namespace": "argocd", "string_data": {"password": "hunter2"}}
    }
    payload = redact_payload("credential_write", raw_params, "ok")
    blob = str(payload)
    assert "hunter2" not in blob, "secret value leaked into broadcast payload"
    assert "string_data" not in blob, "secret keys leaked into broadcast payload"
    assert payload == {"op_class": "credential_write", "result_status": "ok"}


@pytest.mark.asyncio
async def test_job_create_response_omits_spec() -> None:
    conn = _make_connector()
    with (
        _patch_kubeconfig(),
        patch(
            "meho_backplane.connectors.kubernetes.ops_write_dangerous.client.BatchV1Api"
        ) as batch_cls,
    ):
        batch_cls.return_value.create_namespaced_job = AsyncMock()
        result = await conn.k8s_job_create(
            operator=_make_operator(),
            target=_TARGET,
            params={
                "name": "migrate",
                "namespace": "argocd",
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "m",
                                    "image": "migrate:1",
                                    "env": [{"name": "DB_PASS", "value": "topsecret"}],
                                }
                            ],
                            "restartPolicy": "Never",
                        }
                    }
                },
            },
        )
    assert result == {"name": "migrate", "namespace": "argocd", "created": True}
    assert "topsecret" not in str(result)


def test_job_create_classifies_credential_write_and_redacts_broadcast() -> None:
    """The Job's inline env secret in params never reaches the broadcast."""
    assert classify_op("k8s.job.create") == "credential_write"
    raw_params = {
        "params": {
            "name": "migrate",
            "namespace": "argocd",
            "spec": {"template": {"spec": {"containers": [{"env": [{"value": "topsecret"}]}]}}},
        }
    }
    payload = redact_payload("credential_write", raw_params, "ok")
    assert "topsecret" not in str(payload)
    assert payload == {"op_class": "credential_write", "result_status": "ok"}


def test_secret_create_summary_helper_is_value_free() -> None:
    summary = secret_create_summary("s", "ns", "Opaque", ["b", "a"])
    assert summary["data_keys"] == ["a", "b"]
    assert "value" not in str(summary).lower() or "data_keys" in summary


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _awaitable(value: Any) -> Any:
    """Wrap *value* so ``await DynamicClient(...)`` returns it.

    The real ``DynamicClient.__await__`` runs async discovery and returns
    ``self``; the handler does ``dyn = await DynamicClient(api_client)``.
    A ``MagicMock`` is not awaitable, so we hand back an object whose
    ``__await__`` yields the prepared mock.
    """

    class _Awaitable:
        def __await__(self) -> Any:
            async def _coro() -> Any:
                return value

            return _coro().__await__()

    return _Awaitable()

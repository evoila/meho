# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.secret.update`` and ``k8s.secret.delete``: change or delete an existing Secret.

What these tests prove:

* **Registration.** Both ops are registered in the ``config`` group with the
  stated risk levels and ``requires_approval=True``; ``k8s.secret.update``
  is pinned ``credential_write``; both have a preview builder.
* **The update handler** (a fake ``CoreV1Api``): ``patch`` sends only the
  given keys and keeps the others; ``replace`` sends the read Secret back
  with the new maps (type, labels and ``resourceVersion`` ride along) and
  reports the removed keys; a missing Secret names ``k8s.secret.create``; an
  immutable Secret says delete and create again; a 409 in ``replace`` mode
  is a clear conflict error. A ref-sourced value reaches the cluster but
  never the params or the result.
* **The delete handler**: one delete call; a 404 is a clear error.
* **The previews.** The update preview shows key names, store paths and, in
  ``replace`` mode, the keys that go away, never a value. The delete preview
  lists the Pods and CronJobs that use the Secret as ``children`` and is
  empty when nothing uses it; a missing Secret fails the preview.
* **The governed flow.** A made-up value given inline and by ``*_secret_ref``
  is in no result, audit row (``payload`` and ``raw_payload``), approval row
  or preview. A ``k8s.secret.delete`` call without a matching
  ``preview_hash`` is refused, an agent's call is refused, and a missing
  Secret is refused before anything waits for approval.

All names and values are obvious fakes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from kubernetes_asyncio import client as k8s
from kubernetes_asyncio.client.exceptions import ApiException
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import _CREDENTIAL_WRITE_OPS, classify_op
from meho_backplane.connectors.kubernetes import (
    KUBERNETES_OPS,
    KubernetesConnector,
    KubernetesTargetLike,
)
from meho_backplane.connectors.kubernetes import ops_secret_write_preview as preview_module
from meho_backplane.connectors.kubernetes.ops_secret_write import (
    SECRET_WRITE_OPS,
    KubernetesSecretConflictError,
    KubernetesSecretImmutableError,
    KubernetesSecretNotFoundError,
    secret_key_names,
)
from meho_backplane.connectors.kubernetes.ops_write_dangerous import KubernetesSecretRefError
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    PreviewContext,
    blast_radius_missing_reason,
    build_proposed_effect,
)
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations._vendor_text import VENDOR_TEXT_WITHHELD
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_UPDATE = "k8s.secret.update"
_DELETE = "k8s.secret.delete"
_CONNECTOR_ID = "k8s-1.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000004957")
#: Secret refs named in params must sit in the operator's tenant subtree.
_REF_ROOT = f"tenants/{_TENANT_ID}"

#: Made-up values. None of them may show up in a result, preview or audit row.
_INLINE_VALUE = "made-up-inline-value-DO-NOT-ECHO"
_REF_VALUE = "made-up-store-value-DO-NOT-ECHO"


# ---------------------------------------------------------------------------
# Fixtures + helpers (mirror test_connectors_k8s_write.py)
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
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    clear_registry()


@dataclass
class _StubTarget:
    """The target shape the resolver, the dispatcher and the connector read."""

    name: str = "cluster-a"
    host: str = "cluster-a.test.invalid"
    port: int | None = 6443
    secret_ref: str = "k8s/cluster-a"
    product: str = "k8s"
    fingerprint: Any = None
    preferred_impl_id: str | None = None
    auth_model: str | None = "shared_service_account"
    fqdn: str | None = None
    id: object = field(default_factory=uuid4)
    tenant_id: object = _TENANT_ID


_TARGET = _StubTarget()


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


def _operator(
    sub: str = "op-requester", *, principal_kind: PrincipalKind = PrincipalKind.USER
) -> Operator:
    return Operator(
        sub=sub,
        name="Secret Write Test Operator",
        email=None,
        raw_jwt="op.secret.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


def _patch_kubeconfig() -> Any:
    return patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        return_value=MagicMock(close=AsyncMock()),
    )


def _core_v1_patch() -> Any:
    """Patch ``CoreV1Api`` where the handlers and the previews build it (one module object)."""
    return patch("meho_backplane.connectors.kubernetes.ops_secret_write.client.CoreV1Api")


def _existing_secret(
    *, keys: tuple[str, ...] = ("password", "username"), immutable: bool | None = None
) -> k8s.V1Secret:
    return k8s.V1Secret(
        metadata=k8s.V1ObjectMeta(
            name="db-creds",
            namespace="apps",
            resource_version="7",
            labels={"app": "db"},
            annotations={"note": "kept"},
        ),
        type="Opaque",
        data=dict.fromkeys(keys, "ZmFrZQ=="),
        immutable=immutable,
    )


def _core_v1(secret: k8s.V1Secret | Exception | None = None) -> MagicMock:
    """A fake ``CoreV1Api`` whose read answers *secret* (or raises it)."""
    api = MagicMock()
    read = AsyncMock(return_value=secret)
    if isinstance(secret, Exception):
        read = AsyncMock(side_effect=secret)
    api.read_namespaced_secret = read
    api.patch_namespaced_secret = AsyncMock()
    api.replace_namespaced_secret = AsyncMock()
    api.delete_namespaced_secret = AsyncMock()
    api.list_namespaced_pod = AsyncMock(return_value=k8s.V1PodList(items=[]))
    return api


async def _update(core: MagicMock, params: dict[str, Any]) -> dict[str, Any]:
    conn = _make_connector()
    with _patch_kubeconfig(), _core_v1_patch() as core_cls:
        core_cls.return_value = core
        return await conn.k8s_secret_update(operator=_operator(), target=_TARGET, params=params)


async def _delete(core: MagicMock, params: dict[str, Any]) -> dict[str, Any]:
    conn = _make_connector()
    with _patch_kubeconfig(), _core_v1_patch() as core_cls:
        core_cls.return_value = core
        return await conn.k8s_secret_delete(operator=_operator(), target=_TARGET, params=params)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_secret_write_ops_registered_with_risk_level_and_approval() -> None:
    by_id = {op.op_id: op for op in KUBERNETES_OPS}
    assert by_id[_UPDATE].safety_level == "dangerous"
    assert by_id[_UPDATE].requires_approval is True
    assert by_id[_UPDATE].group_key == "config"
    assert by_id[_DELETE].safety_level == "destructive"
    assert by_id[_DELETE].requires_approval is True
    assert by_id[_DELETE].group_key == "config"
    assert "destructive" in by_id[_DELETE].tags


def test_handler_attrs_resolve_on_connector() -> None:
    for op in SECRET_WRITE_OPS:
        assert getattr(KubernetesConnector, op.handler_attr, None) is not None, op.op_id


def test_update_is_a_credential_write_and_delete_a_plain_write() -> None:
    assert _UPDATE in _CREDENTIAL_WRITE_OPS
    assert classify_op(_UPDATE) == "credential_write"
    assert classify_op(_DELETE) == "write"


def test_preview_builders_are_registered() -> None:
    assert _PREVIEW_BUILDERS[_UPDATE] is preview_module._secret_update_preview
    assert _PREVIEW_BUILDERS[_DELETE] is preview_module._secret_delete_preview


def test_secret_key_names_merges_data_and_string_data() -> None:
    secret = k8s.V1Secret(data={"b": "x"}, string_data={"a": "y"})
    assert secret_key_names(secret) == ["a", "b"]
    assert secret_key_names(k8s.V1Secret()) == []


# ---------------------------------------------------------------------------
# k8s.secret.update — the handler
# ---------------------------------------------------------------------------


async def test_patch_sets_given_keys_and_keeps_the_others() -> None:
    core = _core_v1(_existing_secret())
    result = await _update(
        core, {"name": "db-creds", "namespace": "apps", "string_data": {"password": _INLINE_VALUE}}
    )

    core.patch_namespaced_secret.assert_awaited_once_with(
        name="db-creds", namespace="apps", body={"stringData": {"password": _INLINE_VALUE}}
    )
    core.replace_namespaced_secret.assert_not_awaited()
    assert result == {
        "name": "db-creds",
        "namespace": "apps",
        "type": "Opaque",
        "mode": "patch",
        "data_keys": ["password", "username"],
        "keys_set": ["password"],
        "keys_removed": [],
        "updated": True,
    }
    assert _INLINE_VALUE not in json.dumps(result)


async def test_patch_sends_data_and_string_data_separately() -> None:
    core = _core_v1(_existing_secret(keys=("a",)))
    result = await _update(
        core,
        {
            "name": "db-creds",
            "namespace": "apps",
            "mode": "patch",
            "string_data": {"b": "plain"},
            "data": {"c": "YmFzZTY0"},
        },
    )
    body = core.patch_namespaced_secret.call_args.kwargs["body"]
    assert body == {"stringData": {"b": "plain"}, "data": {"c": "YmFzZTY0"}}
    assert result["data_keys"] == ["a", "b", "c"]
    assert result["keys_set"] == ["b", "c"]


async def test_replace_keeps_only_the_given_keys_and_the_metadata() -> None:
    core = _core_v1(_existing_secret())
    result = await _update(
        core,
        {
            "name": "db-creds",
            "namespace": "apps",
            "mode": "replace",
            "string_data": {"token": _INLINE_VALUE},
        },
    )

    core.patch_namespaced_secret.assert_not_awaited()
    call = core.replace_namespaced_secret.call_args
    assert call.kwargs["name"] == "db-creds"
    assert call.kwargs["namespace"] == "apps"
    body = call.kwargs["body"]
    assert body.string_data == {"token": _INLINE_VALUE}
    assert body.data is None
    # The read object goes back: type, labels, annotations and the
    # resourceVersion that makes a concurrent change fail with 409.
    assert body.type == "Opaque"
    assert body.metadata.labels == {"app": "db"}
    assert body.metadata.annotations == {"note": "kept"}
    assert body.metadata.resource_version == "7"
    assert result["mode"] == "replace"
    assert result["data_keys"] == ["token"]
    assert result["keys_set"] == ["token"]
    assert result["keys_removed"] == ["password", "username"]
    assert _INLINE_VALUE not in json.dumps(result)


async def test_missing_secret_names_the_create_op_and_writes_nothing() -> None:
    core = _core_v1(ApiException(status=404, reason="Not Found"))
    with pytest.raises(KubernetesSecretNotFoundError, match=r"k8s\.secret\.create"):
        await _update(core, {"name": "db-creds", "namespace": "apps", "string_data": {"a": "b"}})
    core.patch_namespaced_secret.assert_not_awaited()
    core.replace_namespaced_secret.assert_not_awaited()


async def test_other_read_errors_propagate() -> None:
    core = _core_v1(ApiException(status=403, reason="Forbidden"))
    with pytest.raises(ApiException):
        await _update(core, {"name": "db-creds", "namespace": "apps", "string_data": {"a": "b"}})


async def test_immutable_secret_says_delete_and_create_again() -> None:
    core = _core_v1(_existing_secret(immutable=True))
    with pytest.raises(KubernetesSecretImmutableError, match=r"k8s\.secret\.delete"):
        await _update(core, {"name": "db-creds", "namespace": "apps", "string_data": {"a": "b"}})
    core.patch_namespaced_secret.assert_not_awaited()


async def test_replace_conflict_is_a_clear_error() -> None:
    core = _core_v1(_existing_secret())
    core.replace_namespaced_secret = AsyncMock(
        side_effect=ApiException(status=409, reason="Conflict")
    )
    with pytest.raises(KubernetesSecretConflictError, match="nothing was written") as info:
        await _update(
            core,
            {"name": "db-creds", "namespace": "apps", "mode": "replace", "string_data": {"a": "b"}},
        )
    assert info.value.http_status == 409


async def test_ref_sourced_value_reaches_the_cluster_but_not_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch, secret={"token": _REF_VALUE})
    core = _core_v1(_existing_secret())
    params = {
        "name": "db-creds",
        "namespace": "apps",
        "string_data_secret_ref": {"bearer": f"{_REF_ROOT}/apps/api#token"},
    }
    result = await _update(core, params)

    body = core.patch_namespaced_secret.call_args.kwargs["body"]
    assert body == {"stringData": {"bearer": _REF_VALUE}}
    assert result["keys_set"] == ["bearer"]
    assert _REF_VALUE not in json.dumps(result)
    assert _REF_VALUE not in json.dumps(params)
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == f"{_REF_ROOT}/apps/api"


async def test_ref_without_the_field_names_the_update_op(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_client(monkeypatch, secret={"other": "x"})
    core = _core_v1(_existing_secret())
    with pytest.raises(KubernetesSecretRefError, match=r"k8s\.secret\.update"):
        await _update(
            core,
            {
                "name": "db-creds",
                "namespace": "apps",
                "string_data_secret_ref": {"t": f"{_REF_ROOT}/apps/x#t"},
            },
        )


# ---------------------------------------------------------------------------
# k8s.secret.delete — the handler
# ---------------------------------------------------------------------------


async def test_delete_calls_the_api_once_and_names_the_secret() -> None:
    core = _core_v1()
    result = await _delete(core, {"name": "old-creds", "namespace": "apps"})
    core.delete_namespaced_secret.assert_awaited_once_with(name="old-creds", namespace="apps")
    assert result == {"name": "old-creds", "namespace": "apps", "deleted": True}


async def test_delete_of_a_missing_secret_is_a_clear_error() -> None:
    core = _core_v1()
    core.delete_namespaced_secret = AsyncMock(side_effect=ApiException(status=404, reason="nf"))
    with pytest.raises(KubernetesSecretNotFoundError, match="nothing was deleted"):
        await _delete(core, {"name": "old-creds", "namespace": "apps"})


# ---------------------------------------------------------------------------
# Previews — the builders
# ---------------------------------------------------------------------------


@dataclass
class _FakeDescriptor:
    op_id: str


def _ctx(
    op_id: str, params: dict[str, Any], *, connector: KubernetesConnector | None = None
) -> PreviewContext:
    return PreviewContext(
        descriptor=_FakeDescriptor(op_id=op_id),  # type: ignore[arg-type]
        connector_instance=connector,
        operator=_operator(),
        target=_TARGET if connector is not None else None,
        params=params,
        connector_id=_CONNECTOR_ID,
    )


async def test_update_preview_without_a_connector_shows_params_only() -> None:
    effect = await build_proposed_effect(
        _ctx(
            _UPDATE,
            {
                "name": "db-creds",
                "namespace": "apps",
                "string_data": {"password": _INLINE_VALUE},
                "string_data_secret_ref": {"token": f"vault:{_REF_ROOT}/apps/api#token"},
                "string_data_secret_mount": "kv-apps",
            },
        )
    )
    assert effect is not None
    assert effect["op_class"] == "credential_write"
    assert effect["preview"] == {
        "resource": "secret",
        "name": "db-creds",
        "namespace": "apps",
        "mode": "patch",
        "keys_set": ["password", "token"],
        "inline_key_names": ["password"],
        "string_data_secret_ref": {"token": f"vault:{_REF_ROOT}/apps/api#token"},
        "string_data_secret_mount": "kv-apps",
    }
    assert _INLINE_VALUE not in json.dumps(effect)


async def test_update_preview_reads_current_keys_and_names_removed_keys() -> None:
    conn = _make_connector()
    with _patch_kubeconfig(), _core_v1_patch() as core_cls:
        core_cls.return_value = _core_v1(_existing_secret())
        effect = await build_proposed_effect(
            _ctx(
                _UPDATE,
                {
                    "name": "db-creds",
                    "namespace": "apps",
                    "mode": "replace",
                    "string_data": {"token": _INLINE_VALUE},
                },
                connector=conn,
            )
        )
    assert effect is not None
    preview = effect["preview"]
    assert preview["type"] == "Opaque"
    assert preview["current_key_names"] == ["password", "username"]
    assert preview["keys_set"] == ["token"]
    assert preview["keys_removed"] == ["password", "username"]
    assert preview["immutable"] is False
    assert _INLINE_VALUE not in json.dumps(effect)


async def test_update_preview_of_a_missing_secret_says_so() -> None:
    conn = _make_connector()
    with _patch_kubeconfig(), _core_v1_patch() as core_cls:
        core_cls.return_value = _core_v1(ApiException(status=404, reason="Not Found"))
        effect = await build_proposed_effect(
            _ctx(
                _UPDATE,
                {"name": "db-creds", "namespace": "apps", "string_data": {"a": "b"}},
                connector=conn,
            )
        )
    assert effect is not None
    assert effect["preview_unavailable"] is True
    assert "k8s.secret.create" in effect["preview_error"]


def _pod(name: str, spec: k8s.V1PodSpec) -> k8s.V1Pod:
    return k8s.V1Pod(metadata=k8s.V1ObjectMeta(name=name, namespace="apps"), spec=spec)


def _container(name: str, **kwargs: Any) -> k8s.V1Container:
    return k8s.V1Container(name=name, image="app:1", **kwargs)


def _env_from_secret(var: str, secret: str) -> k8s.V1EnvVar:
    return k8s.V1EnvVar(
        name=var,
        value_from=k8s.V1EnvVarSource(secret_key_ref=k8s.V1SecretKeySelector(name=secret, key="k")),
    )


def _workloads(secret_name: str) -> dict[str, Any]:
    """Fake list answers: a Pod and a CronJob use the Secret; a Deployment does not."""
    pod_using = _pod(
        "web-0",
        k8s.V1PodSpec(
            containers=[_container("web", env=[_env_from_secret("DB_PASSWORD", secret_name)])],
            volumes=[
                k8s.V1Volume(name="creds", secret=k8s.V1SecretVolumeSource(secret_name=secret_name))
            ],
            image_pull_secrets=[k8s.V1LocalObjectReference(name="other-pull")],
        ),
    )
    pod_other = _pod(
        "idle-0",
        k8s.V1PodSpec(containers=[_container("idle", env=[_env_from_secret("X", "other")])]),
    )
    cronjob = k8s.V1CronJob(
        metadata=k8s.V1ObjectMeta(name="nightly", namespace="apps"),
        spec=k8s.V1CronJobSpec(
            schedule="0 1 * * *",
            job_template=k8s.V1JobTemplateSpec(
                spec=k8s.V1JobSpec(
                    template=k8s.V1PodTemplateSpec(
                        spec=k8s.V1PodSpec(
                            containers=[_container("run")],
                            init_containers=[
                                _container(
                                    "init",
                                    env_from=[
                                        k8s.V1EnvFromSource(
                                            secret_ref=k8s.V1SecretEnvSource(name=secret_name)
                                        )
                                    ],
                                )
                            ],
                        )
                    )
                )
            ),
        ),
    )
    deployment = k8s.V1Deployment(
        metadata=k8s.V1ObjectMeta(name="api", namespace="apps"),
        spec=k8s.V1DeploymentSpec(
            selector=k8s.V1LabelSelector(match_labels={"app": "api"}),
            template=k8s.V1PodTemplateSpec(
                spec=k8s.V1PodSpec(containers=[_container("api")]),
            ),
        ),
    )
    return {
        "pods": k8s.V1PodList(items=[pod_using, pod_other]),
        "cronjobs": k8s.V1CronJobList(items=[cronjob]),
        "deployments": k8s.V1DeploymentList(items=[deployment]),
    }


def _workload_api_patches(lists: dict[str, Any]) -> list[Any]:
    """Patch the Apps and Batch list calls the delete preview makes."""
    apps = MagicMock()
    apps.list_namespaced_deployment = AsyncMock(return_value=lists["deployments"])
    apps.list_namespaced_stateful_set = AsyncMock(return_value=k8s.V1StatefulSetList(items=[]))
    apps.list_namespaced_daemon_set = AsyncMock(return_value=k8s.V1DaemonSetList(items=[]))
    batch = MagicMock()
    batch.list_namespaced_cron_job = AsyncMock(return_value=lists["cronjobs"])
    prefix = "meho_backplane.connectors.kubernetes.ops_secret_write_preview.client."
    return [
        patch(prefix + "AppsV1Api", return_value=apps),
        patch(prefix + "BatchV1Api", return_value=batch),
    ]


async def _delete_preview(core: MagicMock, lists: dict[str, Any]) -> dict[str, Any] | None:
    conn = _make_connector()
    core.list_namespaced_pod = AsyncMock(return_value=lists["pods"])
    apps_patch, batch_patch = _workload_api_patches(lists)
    with _patch_kubeconfig(), _core_v1_patch() as core_cls, apps_patch, batch_patch:
        core_cls.return_value = core
        return await build_proposed_effect(
            _ctx(_DELETE, {"name": "db-creds", "namespace": "apps"}, connector=conn)
        )


def test_secret_uses_in_pod_spec_covers_every_reference_kind() -> None:
    spec = k8s.V1PodSpec(
        containers=[
            _container(
                "c",
                env=[_env_from_secret("A", "s")],
                env_from=[k8s.V1EnvFromSource(secret_ref=k8s.V1SecretEnvSource(name="s"))],
            )
        ],
        init_containers=[_container("i", env=[_env_from_secret("B", "s")])],
        volumes=[
            k8s.V1Volume(name="v", secret=k8s.V1SecretVolumeSource(secret_name="s")),
            k8s.V1Volume(
                name="p",
                projected=k8s.V1ProjectedVolumeSource(
                    sources=[k8s.V1VolumeProjection(secret=k8s.V1SecretProjection(name="s"))]
                ),
            ),
        ],
        image_pull_secrets=[k8s.V1LocalObjectReference(name="s")],
    )
    assert preview_module.secret_uses_in_pod_spec(spec, "s") == [
        "env",
        "env_from",
        "image_pull_secret",
        "projected_volume",
        "volume",
    ]
    assert preview_module.secret_uses_in_pod_spec(spec, "other") == []
    assert preview_module.secret_uses_in_pod_spec(None, "s") == []


async def test_delete_preview_lists_the_pod_and_the_cronjob_that_use_the_secret() -> None:
    effect = await _delete_preview(_core_v1(_existing_secret()), _workloads("db-creds"))

    assert effect is not None, "no preview"
    preview = effect["preview"]
    assert preview["key_names"] == ["password", "username"]
    blast = preview["blast_radius"]
    assert blast["object"] == {
        "kind": "secret",
        "namespace": "apps",
        "name": "db-creds",
        "type": "Opaque",
        "key_names": ["password", "username"],
    }
    assert blast["children"] == [
        {"kind": "pod", "name": "web-0", "uses": ["env", "volume"]},
        {"kind": "cronjob", "name": "nightly", "uses": ["env_from"]},
    ]
    assert blast["irreversibility"] == "permanent"
    assert blast_radius_missing_reason(preview) is None
    assert "ZmFrZQ==" not in json.dumps(effect)


async def test_delete_preview_children_is_empty_when_nothing_uses_the_secret() -> None:
    effect = await _delete_preview(_core_v1(_existing_secret()), _workloads("some-other-secret"))

    assert effect is not None
    blast = effect["preview"]["blast_radius"]
    assert blast["children"] == []
    assert blast_radius_missing_reason(effect["preview"]) is None


async def test_delete_preview_of_a_missing_secret_fails_the_blast_radius_gate() -> None:
    effect = await _delete_preview(
        _core_v1(ApiException(status=404, reason="Not Found")), _workloads("db-creds")
    )

    assert effect is not None
    assert effect["preview_unavailable"] is True
    reason = blast_radius_missing_reason(effect)
    assert reason is not None
    assert "does not exist" in reason


async def test_delete_preview_declines_without_a_connector() -> None:
    assert await preview_module._secret_delete_preview(_ctx(_DELETE, {"name": "x"})) is None


# ---------------------------------------------------------------------------
# The governed flow through the dispatcher
# ---------------------------------------------------------------------------


@pytest.fixture
async def _registered_ops() -> None:
    from meho_backplane.operations import typed_register as tr_module
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    reset_handler_cache()
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await KubernetesConnector.register_operations()


@pytest.fixture
def _fake_api_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """The dispatcher builds its own connector; give it a fake API client."""
    api_client = MagicMock(close=AsyncMock())
    monkeypatch.setattr(KubernetesConnector, "_get_api_client", AsyncMock(return_value=api_client))
    return api_client


async def _pending_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


async def _audit_blobs() -> str:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(AuditLog))).scalars().all()
    return json.dumps([[r.payload, r.raw_payload] for r in rows], default=str)


_LEAK_PARAMS: dict[str, Any] = {
    "name": "db-creds",
    "namespace": "apps",
    "string_data": {"password": _INLINE_VALUE},
    "string_data_secret_ref": {"token": f"{_REF_ROOT}/apps/api#token"},
}


async def test_update_values_never_reach_result_audit_approval_row_or_preview(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None, _fake_api_client: MagicMock
) -> None:
    install_fake_client(monkeypatch, secret={"token": _REF_VALUE})
    core = _core_v1(_existing_secret())

    with _core_v1_patch() as core_cls:
        core_cls.return_value = core
        # 1) A person's call waits for approval; nothing is written.
        parked = await dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_UPDATE,
            target=_TARGET,
            params=dict(_LEAK_PARAMS),
        )
        assert parked.status == "awaiting_approval", parked
        core.patch_namespaced_secret.assert_not_awaited()

        # 2) The approval row holds no params (they are in the hand-off store)
        #    and its preview shows key names only.
        request_id = UUID(parked.extras["approval_request_id"])
        async with get_sessionmaker()() as s:
            row = await s.get(ApprovalRequest, request_id)
            assert row is not None
            assert row.params is None
            assert row.execution_handle is not None
            effect = dict(row.proposed_effect)
        assert effect["preview"]["keys_set"] == ["password", "token"]
        assert effect["preview"]["current_key_names"] == ["password", "username"]
        blob = json.dumps(effect)
        assert _INLINE_VALUE not in blob
        assert _REF_VALUE not in blob

        # 3) The approved run writes the values to the cluster and returns key names.
        ran = await dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_UPDATE,
            target=_TARGET,
            params=dict(_LEAK_PARAMS),
            _approved=True,
        )
    assert ran.status == "ok", ran.error
    body = core.patch_namespaced_secret.call_args.kwargs["body"]
    assert body == {"stringData": {"password": _INLINE_VALUE, "token": _REF_VALUE}}
    result_blob = json.dumps(ran.result)
    assert ran.result["data_keys"] == ["password", "token", "username"]
    assert _INLINE_VALUE not in result_blob
    assert _REF_VALUE not in result_blob

    # 4) No audit row (payload or raw_payload) carries a value.
    audit = await _audit_blobs()
    assert _UPDATE in audit
    assert _INLINE_VALUE not in audit
    assert _REF_VALUE not in audit


# ``k8s.secret.update`` sends a secret, so the dispatcher withholds the text of
# a raised error (#4854). MEHO's own guidance must still reach the caller
# through ``extras.remediation``.

_UPDATE_PARAMS: dict[str, Any] = {
    "name": "db-creds",
    "namespace": "apps",
    "string_data": {"password": _INLINE_VALUE},
}


async def _approved_update(core: MagicMock, params: dict[str, Any]) -> Any:
    with _core_v1_patch() as core_cls:
        core_cls.return_value = core
        return await dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_UPDATE,
            target=_TARGET,
            params=params,
            _approved=True,
        )


def _assert_message_withheld_but_guidance_kept(
    result: Any, *, exception_class: str, guidance: str
) -> None:
    assert result.status == "error", result
    assert result.error == f"connector_error: {exception_class}"
    assert result.extras["exception_class"] == exception_class
    assert result.extras["exception_message"] == VENDOR_TEXT_WITHHELD
    assert guidance in result.extras["remediation"]
    assert _INLINE_VALUE not in json.dumps(result.model_dump(mode="json"), default=str)


async def test_dispatch_of_a_missing_secret_keeps_the_create_guidance(
    _registered_ops: None, _fake_api_client: MagicMock
) -> None:
    core = _core_v1(ApiException(status=404, reason="Not Found"))
    result = await _approved_update(core, dict(_UPDATE_PARAMS))
    _assert_message_withheld_but_guidance_kept(
        result,
        exception_class="KubernetesSecretNotFoundError",
        guidance="use k8s.secret.create to create it",
    )
    assert "Secret apps/db-creds does not exist" in result.extras["remediation"]
    core.patch_namespaced_secret.assert_not_awaited()
    audit = await _audit_blobs()
    assert "use k8s.secret.create to create it" in audit
    assert _INLINE_VALUE not in audit


async def test_dispatch_of_an_immutable_secret_keeps_the_delete_guidance(
    _registered_ops: None, _fake_api_client: MagicMock
) -> None:
    core = _core_v1(_existing_secret(immutable=True))
    result = await _approved_update(core, dict(_UPDATE_PARAMS))
    _assert_message_withheld_but_guidance_kept(
        result,
        exception_class="KubernetesSecretImmutableError",
        guidance=f"Delete it with {_DELETE} and create it again with k8s.secret.create",
    )
    core.patch_namespaced_secret.assert_not_awaited()
    core.replace_namespaced_secret.assert_not_awaited()


async def test_dispatch_of_a_replace_conflict_keeps_the_retry_guidance(
    _registered_ops: None, _fake_api_client: MagicMock
) -> None:
    core = _core_v1(_existing_secret())
    core.replace_namespaced_secret = AsyncMock(
        side_effect=ApiException(status=409, reason="Conflict")
    )
    result = await _approved_update(core, {**_UPDATE_PARAMS, "mode": "replace"})
    _assert_message_withheld_but_guidance_kept(
        result,
        exception_class="KubernetesSecretConflictError",
        guidance=f"nothing was written. Run {_UPDATE} again",
    )
    assert result.extras["http_status"] == 409
    assert result.extras["upstream_message"] == VENDOR_TEXT_WITHHELD


async def test_delete_without_a_preview_hash_is_refused(
    _registered_ops: None, _fake_api_client: MagicMock
) -> None:
    core = _core_v1(_existing_secret())
    with _core_v1_patch() as core_cls:
        core_cls.return_value = core
        result = await dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_DELETE,
            target=_TARGET,
            params={"name": "db-creds", "namespace": "apps"},
        )
    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_binding_required"
    core.delete_namespaced_secret.assert_not_awaited()
    assert await _pending_count() == 0


async def test_delete_with_a_wrong_preview_hash_is_refused(
    _registered_ops: None, _fake_api_client: MagicMock
) -> None:
    core = _core_v1(_existing_secret())
    apps_patch, batch_patch = _workload_api_patches(_workloads("other"))
    with _core_v1_patch() as core_cls, apps_patch, batch_patch:
        core_cls.return_value = core
        result = await dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_DELETE,
            target=_TARGET,
            params={"name": "db-creds", "namespace": "apps"},
            preview_hash="deadbeef" * 8,
        )
    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_hash_mismatch"
    core.delete_namespaced_secret.assert_not_awaited()
    assert await _pending_count() == 0


async def test_delete_with_the_bound_hash_waits_for_approval_with_the_blast_radius(
    _registered_ops: None, _fake_api_client: MagicMock
) -> None:
    core = _core_v1(_existing_secret())
    lists = _workloads("db-creds")
    core.list_namespaced_pod = AsyncMock(return_value=lists["pods"])
    apps_patch, batch_patch = _workload_api_patches(lists)
    params = {"name": "db-creds", "namespace": "apps"}
    with _core_v1_patch() as core_cls, apps_patch, batch_patch:
        core_cls.return_value = core
        preview = await preview_dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_DELETE,
            target=_TARGET,
            params=params,
        )
        assert preview["status"] == "ok", preview
        result = await dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_DELETE,
            target=_TARGET,
            params=params,
            preview_hash=preview["preview_hash"],
        )
    assert result.status == "awaiting_approval", result
    core.delete_namespaced_secret.assert_not_awaited()
    request_id = UUID(result.extras["approval_request_id"])
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        assert row.preview_hash == preview["preview_hash"]
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    assert effect["blast_radius"]["object"]["name"] == "db-creds"
    assert [c["kind"] for c in effect["blast_radius"]["children"]] == ["pod", "cronjob"]


async def test_delete_of_a_missing_secret_is_refused_before_approval(
    _registered_ops: None, _fake_api_client: MagicMock
) -> None:
    core = _core_v1(ApiException(status=404, reason="Not Found"))
    params = {"name": "gone", "namespace": "apps"}
    with _core_v1_patch() as core_cls:
        core_cls.return_value = core
        preview = await preview_dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_DELETE,
            target=_TARGET,
            params=params,
        )
        result = await dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_DELETE,
            target=_TARGET,
            params=params,
            preview_hash=preview.get("preview_hash"),
        )
    assert result.status == "denied", result
    assert result.extras["error_code"] == "blast_radius_required"
    assert "does not exist" in (result.error or "")
    core.delete_namespaced_secret.assert_not_awaited()
    assert await _pending_count() == 0


async def test_agent_cannot_delete_a_secret(
    _registered_ops: None, _fake_api_client: MagicMock
) -> None:
    core = _core_v1(_existing_secret())
    with _core_v1_patch() as core_cls:
        core_cls.return_value = core
        result = await dispatch(
            operator=_operator("agent-1", principal_kind=PrincipalKind.AGENT),
            connector_id=_CONNECTOR_ID,
            op_id=_DELETE,
            target=_TARGET,
            params={"name": "db-creds", "namespace": "apps"},
        )
    assert result.status == "denied", result
    core.delete_namespaced_secret.assert_not_awaited()
    assert await _pending_count() == 0

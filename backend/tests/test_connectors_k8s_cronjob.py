# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``k8s.cronjob.run``: start a CronJob's job now.

The Kubernetes API is a fake ``BatchV1Api`` that hands back real
``kubernetes_asyncio`` model objects, so the Job the handler builds is
checked field by field: the kubectl annotation, the template's labels and
annotations, the owner reference and the copied spec. The approval preview
runs through the real registered builder.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.client.models import (
    V1Container,
    V1CronJob,
    V1CronJobSpec,
    V1CronJobStatus,
    V1EnvFromSource,
    V1EnvVar,
    V1Job,
    V1JobSpec,
    V1JobTemplateSpec,
    V1ObjectMeta,
    V1ObjectReference,
    V1PodSpec,
    V1PodTemplateSpec,
    V1SecretEnvSource,
    V1SecurityContext,
)

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import (
    KUBERNETES_OPS,
    KubernetesConnector,
    KubernetesTargetLike,
)
from meho_backplane.connectors.kubernetes.ops_cronjob import (
    INSTANTIATE_ANNOTATION,
    build_job_from_cronjob,
    model_to_json,
)
from meho_backplane.connectors.kubernetes.ops_workload import WorkloadNotFoundError
from meho_backplane.connectors.kubernetes.ops_write_preview import _cronjob_run_preview
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.settings import get_settings

_BATCH_V1 = "meho_backplane.connectors.kubernetes.ops_cronjob.client.BatchV1Api"

#: A made-up secret. The preview must never show it.
_SECRET = "made-up-env-Value-4958"
#: A short made-up password on a command line.
_SHORT = "Xq9"


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


_TARGET = _StubTarget(
    name="k3s-unit", host="k3s-unit.test.invalid", port=6443, secret_ref="k8s/k3s-unit"
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
        sub="op-cronjob-test",
        name="CronJob Test Operator",
        email=None,
        raw_jwt="op.cronjob.jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000004958"),
        tenant_role=TenantRole.OPERATOR,
    )


def _patch_kubeconfig() -> Any:
    return patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        return_value=MagicMock(close=AsyncMock()),
    )


def _cronjob(
    *,
    suspend: bool = False,
    annotations: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    active: list[str] | None = None,
) -> V1CronJob:
    container = V1Container(
        name="backup",
        image="registry.example/backup:1.2",
        command=["sh", "-c", "run-backup"],
        env=[V1EnvVar(name="DB_PASSWORD", value=_SECRET)],
        env_from=[V1EnvFromSource(secret_ref=V1SecretEnvSource(name="db-creds"))],
    )
    pod_spec = V1PodSpec(
        containers=[container],
        init_containers=[V1Container(name="wait", image="busybox:1.36")],
        restart_policy="Never",
    )
    template = V1JobTemplateSpec(
        metadata=V1ObjectMeta(annotations=annotations, labels=labels),
        spec=V1JobSpec(template=V1PodTemplateSpec(spec=pod_spec), backoff_limit=2),
    )
    return V1CronJob(
        metadata=V1ObjectMeta(name="db-backup", namespace="data", uid="uid-db-backup"),
        spec=V1CronJobSpec(
            schedule="0 2 * * *",
            job_template=template,
            suspend=suspend,
            concurrency_policy="Forbid",
        ),
        status=V1CronJobStatus(
            active=[V1ObjectReference(name=name) for name in active] if active else None
        ),
    )


def _fake_batch(batch_cls: MagicMock, cronjob: V1CronJob | Exception) -> MagicMock:
    """Wire a fake ``BatchV1Api``: the read returns *cronjob*, the create echoes the body."""
    api = batch_cls.return_value
    if isinstance(cronjob, Exception):
        api.read_namespaced_cron_job = AsyncMock(side_effect=cronjob)
    else:
        api.read_namespaced_cron_job = AsyncMock(return_value=cronjob)

    async def _create(*, namespace: str, body: V1Job) -> V1Job:
        name = body.metadata.name or f"{body.metadata.generate_name}k7x2q"
        return V1Job(metadata=V1ObjectMeta(name=name, namespace=namespace), spec=body.spec)

    api.create_namespaced_job = AsyncMock(side_effect=_create)
    return api


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_cronjob_run_is_registered_caution_with_approval() -> None:
    op = next(o for o in KUBERNETES_OPS if o.op_id == "k8s.cronjob.run")
    assert op.safety_level == "caution"
    assert op.requires_approval is True
    assert op.group_key == "workload"
    assert op.handler_attr == "k8s_cronjob_run"
    assert op.llm_instructions is not None
    assert "k8s.job.info" in op.llm_instructions["next_step"]


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cronjob_run_builds_the_job_like_kubectl() -> None:
    cronjob = _cronjob(
        annotations={"team": "data", INSTANTIATE_ANNOTATION: "template-wins"},
        labels={"app": "backup"},
    )
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        api = _fake_batch(batch_cls, cronjob)
        result = await _make_connector().k8s_cronjob_run(
            _make_operator(),
            _TARGET,
            {"cronjob": "db-backup", "namespace": "data", "job_name": "db-backup-check"},
        )

    assert result == {
        "job_name": "db-backup-check",
        "namespace": "data",
        "cronjob": "db-backup",
        "created": True,
    }
    api.read_namespaced_cron_job.assert_awaited_once_with(name="db-backup", namespace="data")
    body: V1Job = api.create_namespaced_job.await_args.kwargs["body"]
    assert api.create_namespaced_job.await_args.kwargs["namespace"] == "data"
    assert body.api_version == "batch/v1"
    assert body.kind == "Job"
    assert body.metadata.name == "db-backup-check"
    assert body.metadata.generate_name is None
    assert body.metadata.namespace == "data"
    # kubectl copies the template annotations over the manual marker.
    assert body.metadata.annotations == {"team": "data", INSTANTIATE_ANNOTATION: "template-wins"}
    assert body.metadata.labels == {"app": "backup"}
    (owner,) = body.metadata.owner_references
    assert owner.api_version == "batch/v1"
    assert owner.kind == "CronJob"
    assert owner.name == "db-backup"
    assert owner.uid == "uid-db-backup"
    assert owner.controller is True
    assert owner.block_owner_deletion is None
    assert body.spec is cronjob.spec.job_template.spec


@pytest.mark.asyncio
async def test_cronjob_run_without_job_name_uses_generate_name() -> None:
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        api = _fake_batch(batch_cls, _cronjob())
        result = await _make_connector().k8s_cronjob_run(
            _make_operator(), _TARGET, {"cronjob": "db-backup", "namespace": "data"}
        )

    body: V1Job = api.create_namespaced_job.await_args.kwargs["body"]
    assert body.metadata.name is None
    assert body.metadata.generate_name == "db-backup-manual-"
    assert body.metadata.annotations == {INSTANTIATE_ANNOTATION: "manual"}
    assert body.metadata.labels is None
    # The result carries the name the API server chose.
    assert result["job_name"] == "db-backup-manual-k7x2q"
    assert result["created"] is True


@pytest.mark.asyncio
async def test_cronjob_run_missing_cronjob_creates_nothing() -> None:
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        api = _fake_batch(batch_cls, ApiException(status=404, reason="Not Found"))
        with pytest.raises(WorkloadNotFoundError, match="'nope' not found in namespace 'data'"):
            await _make_connector().k8s_cronjob_run(
                _make_operator(), _TARGET, {"cronjob": "nope", "namespace": "data"}
            )
    api.create_namespaced_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_cronjob_run_other_api_errors_pass_through() -> None:
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        api = _fake_batch(batch_cls, ApiException(status=403, reason="Forbidden"))
        with pytest.raises(ApiException):
            await _make_connector().k8s_cronjob_run(
                _make_operator(), _TARGET, {"cronjob": "db-backup", "namespace": "data"}
            )
    api.create_namespaced_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_cronjob_run_suspended_cronjob_still_runs() -> None:
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        api = _fake_batch(batch_cls, _cronjob(suspend=True))
        result = await _make_connector().k8s_cronjob_run(
            _make_operator(), _TARGET, {"cronjob": "db-backup", "namespace": "data"}
        )
    api.create_namespaced_job.assert_awaited_once()
    assert result["created"] is True


def test_build_job_from_cronjob_without_template_metadata() -> None:
    cronjob = _cronjob()
    cronjob.spec.job_template.metadata = None
    job = build_job_from_cronjob(cronjob)
    assert job.metadata.annotations == {INSTANTIATE_ANNOTATION: "manual"}
    assert job.metadata.labels is None
    assert job.metadata.generate_name == "db-backup-manual-"


def test_model_to_json_uses_api_field_names_and_drops_none() -> None:
    data = model_to_json(_cronjob().spec.job_template.spec.template.spec)
    assert data["restartPolicy"] == "Never"
    assert data["containers"][0]["envFrom"] == [{"secretRef": {"name": "db-creds"}}]
    assert "activeDeadlineSeconds" not in data
    assert model_to_json([1, "a", None]) == [1, "a", None]


# ---------------------------------------------------------------------------
# Approval preview
# ---------------------------------------------------------------------------


@dataclass
class _FakeDescriptor:
    op_id: str


@pytest.mark.asyncio
async def test_cronjob_run_preview_shows_job_template_and_active_jobs_not_values() -> None:
    cronjob = _cronjob(active=["db-backup-29300000", "db-backup-29301440"])
    pod_spec = cronjob.spec.job_template.spec.template.spec
    pod_spec.node_name = "node-01"
    pod_spec.host_pid = True
    pod_spec.containers[0].security_context = V1SecurityContext(privileged=True)
    pod_spec.containers[0].args = [
        f"--token={_SECRET}",
        f"PGPASSWORD={_SHORT} pg_dump -h db app",
        "--password",
        _SHORT,
        f"curl -u admin:{_SHORT} https://bob:{_SHORT}@example.invalid",  # gitleaks:allow
    ]
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="k8s.cronjob.run"),  # type: ignore[arg-type]
        connector_instance=_make_connector(),
        operator=_make_operator(),
        target=_TARGET,
        params={"cronjob": "db-backup", "namespace": "data"},
    )
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        api = _fake_batch(batch_cls, cronjob)
        effect = await build_proposed_effect(ctx)

    assert effect is not None
    api.create_namespaced_job.assert_not_awaited()
    assert _SECRET not in json.dumps(effect)
    assert _SHORT not in json.dumps(effect)
    assert "pg_dump -h db app" in json.dumps(effect)
    preview = effect["preview"]
    assert preview["resource"] == "cronjob_run"
    assert preview["cronjob"] == "db-backup"
    assert preview["namespace"] == "data"
    assert preview["job_name"] == "db-backup-manual-<random>"
    assert preview["schedule"] == "0 2 * * *"
    assert preview["suspend"] is False
    assert preview["concurrency_policy"] == "Forbid"
    assert preview["active_jobs"] == ["db-backup-29300000", "db-backup-29301440"]
    job_spec = preview["job_spec"]
    assert job_spec["backoffLimit"] == 2
    pod = job_spec["template"]["spec"]
    assert pod["nodeName"] == "node-01"
    assert pod["hostPID"] is True
    (container,) = pod["containers"]
    assert container["image"] == "registry.example/backup:1.2"
    assert container["command"] == ["sh", "-c", "run-backup"]
    assert container["securityContext"] == {"privileged": True}
    assert container["env"] == [{"name": "DB_PASSWORD", "value": "***REDACTED***"}]
    assert container["envFrom"] == [{"secretRef": {"name": "db-creds"}}]
    assert pod["initContainers"] == [{"name": "wait", "image": "busybox:1.36"}]


@pytest.mark.asyncio
async def test_cronjob_run_preview_uses_job_name_and_declines_without_connector() -> None:
    params = {"cronjob": "db-backup", "namespace": "data", "job_name": "db-backup-check"}

    def _ctx(connector: KubernetesConnector | None) -> PreviewContext:
        return PreviewContext(
            descriptor=_FakeDescriptor(op_id="k8s.cronjob.run"),  # type: ignore[arg-type]
            connector_instance=connector,
            operator=_make_operator(),
            target=_TARGET,
            params=params,
        )

    assert await _cronjob_run_preview(_ctx(None)) is None

    ctx = _ctx(_make_connector())
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        _fake_batch(batch_cls, _cronjob(suspend=True))
        preview = await _cronjob_run_preview(ctx)
    assert preview is not None
    assert preview["job_name"] == "db-backup-check"
    assert preview["suspend"] is True
    assert preview["active_jobs"] == []

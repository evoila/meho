# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the K8s Job read ops ``k8s.job.list`` / ``k8s.job.info``.

* Registration: both ops sit in :data:`KUBERNETES_OPS`, are safe reads
  in the ``workload`` group, and their handler attributes resolve to
  async methods on :class:`KubernetesConnector`.
* Pure helpers: :func:`job_state` for a running / complete / failed /
  suspended Job, :func:`job_row`, :func:`label_selector_string`,
  :func:`job_pod_selector`, :func:`job_info` (pods newest first, cut at
  :data:`JOB_INFO_MAX_PODS`).
* Handlers against a mocked ``BatchV1Api`` / ``CoreV1Api``: routing,
  pagination, prefix resolution, and the pod lookup by the Job's
  selector.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from kubernetes_asyncio.client.models import (
    V1Container,
    V1Job,
    V1JobCondition,
    V1JobList,
    V1JobSpec,
    V1JobStatus,
    V1LabelSelector,
    V1LabelSelectorRequirement,
    V1ListMeta,
    V1ObjectMeta,
    V1Pod,
    V1PodList,
    V1PodSpec,
    V1PodStatus,
    V1PodTemplateSpec,
)

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import (
    KUBERNETES_OPS,
    KubernetesConnector,
    KubernetesTargetLike,
)
from meho_backplane.connectors.kubernetes.ops_job import (
    JOB_INFO_MAX_PODS,
    JOB_NAME_LABEL,
    JOB_OPS,
    job_info,
    job_pod_selector,
    job_row,
    job_state,
    label_selector_string,
)
from meho_backplane.connectors.kubernetes.ops_workload import (
    AmbiguousPrefixError,
    WorkloadNotFoundError,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.settings import get_settings

_NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
_BATCH_V1 = "meho_backplane.connectors.kubernetes.ops_job.client.BatchV1Api"
_CORE_V1 = "meho_backplane.connectors.kubernetes.ops_job.client.CoreV1Api"


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
    id: object = field(default_factory=uuid4)
    tenant_id: object = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget(name="k8s-test", host="k8s.test.invalid", port=6443, secret_ref="k8s/test")


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
        sub="op-job-test",
        name="Job Test Operator",
        email=None,
        raw_jwt="op.job.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _patch_kubeconfig() -> Any:
    return patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        return_value=MagicMock(close=AsyncMock()),
    )


# ---------------------------------------------------------------------------
# Synthetic model builders
# ---------------------------------------------------------------------------


def _condition(ctype: str, status: str = "True", reason: str | None = None) -> V1JobCondition:
    return V1JobCondition(
        type=ctype,
        status=status,
        reason=reason,
        message=f"{ctype} condition",
        last_transition_time=_NOW,
    )


def _make_job(
    *,
    name: str = "meho-migrate",
    namespace: str = "meho",
    active: int = 0,
    succeeded: int = 0,
    failed: int = 0,
    completions: int | None = 1,
    backoff_limit: int | None = 6,
    active_deadline_seconds: int | None = None,
    conditions: list[V1JobCondition] | None = None,
    start_time: datetime | None = None,
    completion_time: datetime | None = None,
    selector: V1LabelSelector | str | None = "default",
    image: str = "ghcr.io/example/meho:1.0",
    status: V1JobStatus | None = None,
) -> V1Job:
    template = V1PodTemplateSpec(
        spec=V1PodSpec(containers=[V1Container(name="migrate", image=image)])
    )
    if selector == "default":
        selector = V1LabelSelector(match_labels={"batch.kubernetes.io/controller-uid": "uid-1"})
    if status is None:
        status = V1JobStatus(
            active=active,
            succeeded=succeeded,
            failed=failed,
            conditions=conditions or [],
            start_time=start_time,
            completion_time=completion_time,
        )
    return V1Job(
        metadata=V1ObjectMeta(
            name=name, namespace=namespace, creation_timestamp=_NOW - timedelta(minutes=5)
        ),
        spec=V1JobSpec(
            completions=completions,
            backoff_limit=backoff_limit,
            active_deadline_seconds=active_deadline_seconds,
            selector=selector,
            template=template,
        ),
        status=status,
    )


def _make_pod(name: str, *, created: datetime, phase: str = "Succeeded") -> V1Pod:
    return V1Pod(
        metadata=V1ObjectMeta(name=name, namespace="meho", creation_timestamp=created),
        spec=V1PodSpec(containers=[V1Container(name="migrate", image="x:1")], node_name="node-1"),
        status=V1PodStatus(phase=phase, container_statuses=[], pod_ip="10.0.0.1"),
    )


def _job_list(jobs: list[V1Job], continue_token: str | None = None) -> V1JobList:
    return V1JobList(items=jobs, metadata=V1ListMeta(_continue=continue_token))


def _pod_list(pods: list[V1Pod]) -> V1PodList:
    return V1PodList(items=pods, metadata=V1ListMeta())


# ---------------------------------------------------------------------------
# Registration surface
# ---------------------------------------------------------------------------


def test_job_ops_in_kubernetes_ops_tuple() -> None:
    op_ids = {op.op_id for op in KUBERNETES_OPS}
    assert {"k8s.job.list", "k8s.job.info"} <= op_ids
    assert len({op.op_id for op in JOB_OPS}) == 2


def test_job_ops_are_safe_workload_reads_with_full_guidance() -> None:
    for op in JOB_OPS:
        assert op.safety_level == "safe"
        assert op.requires_approval is False
        assert op.group_key == "workload"
        assert "read-only" in op.tags
        assert op.llm_instructions is not None
        for key in ("when_to_call", "output_shape", "next_step"):
            assert op.llm_instructions[key]
        assert op.parameter_schema["examples"]


def test_job_handler_attrs_resolve_to_async_methods() -> None:
    import inspect

    connector = _make_connector()
    for op in JOB_OPS:
        handler = getattr(connector, op.handler_attr)
        assert inspect.iscoroutinefunction(handler), op.handler_attr


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        (_make_job(active=1), "running"),
        (_make_job(succeeded=1, conditions=[_condition("Complete")]), "complete"),
        (
            _make_job(failed=2, conditions=[_condition("Failed", reason="BackoffLimitExceeded")]),
            "failed",
        ),
        (_make_job(conditions=[_condition("Suspended")]), "suspended"),
        (_make_job(conditions=[_condition("Complete", status="False")]), "running"),
        (_make_job(conditions=[_condition("Complete"), _condition("Failed")]), "failed"),
    ],
    ids=["running", "complete", "failed", "suspended", "false-condition", "failed-wins"],
)
def test_job_state_from_conditions(job: V1Job, expected: str) -> None:
    assert job_state(job) == expected


def test_job_row_projects_full_shape() -> None:
    start = _NOW - timedelta(minutes=4)
    done = _NOW - timedelta(minutes=1)
    job = _make_job(
        succeeded=1,
        completions=1,
        conditions=[_condition("Complete")],
        start_time=start,
        completion_time=done,
    )
    row = job_row(job, now=_NOW)
    assert row == {
        "name": "meho-migrate",
        "namespace": "meho",
        "age_seconds": 300,
        "state": "complete",
        "pods_active": 0,
        "pods_succeeded": 1,
        "pods_failed": 0,
        "completions": 1,
        "start_time": "2026-10-24T11:56:00Z",
        "completion_time": "2026-10-24T11:59:00Z",
    }


def test_job_row_without_status_defaults_to_zero_counts() -> None:
    job = _make_job(completions=None)
    job.status = None
    row = job_row(job, now=_NOW)
    assert row["state"] == "running"
    assert (row["pods_active"], row["pods_succeeded"], row["pods_failed"]) == (0, 0, 0)
    assert row["completions"] is None
    assert row["start_time"] is None
    assert row["completion_time"] is None


def test_label_selector_string_renders_labels_and_expressions() -> None:
    selector = V1LabelSelector(
        match_labels={"job-name": "backup", "app": "db"},
        match_expressions=[
            V1LabelSelectorRequirement(key="tier", operator="In", values=["a", "b"]),
            V1LabelSelectorRequirement(key="env", operator="NotIn", values=["dev"]),
            V1LabelSelectorRequirement(key="owned", operator="Exists"),
            V1LabelSelectorRequirement(key="legacy", operator="DoesNotExist"),
        ],
    )
    assert (
        label_selector_string(selector)
        == "app=db,job-name=backup,tier in (a,b),env notin (dev),owned,!legacy"
    )
    assert label_selector_string(None) is None
    assert label_selector_string(V1LabelSelector()) is None


def test_job_pod_selector_prefers_spec_selector_and_falls_back_to_job_name_label() -> None:
    assert job_pod_selector(_make_job()) == "batch.kubernetes.io/controller-uid=uid-1"
    assert job_pod_selector(_make_job(selector=None)) == f"{JOB_NAME_LABEL}=meho-migrate"


def test_job_info_carries_conditions_limits_containers_and_pods() -> None:
    job = _make_job(
        failed=2,
        backoff_limit=1,
        active_deadline_seconds=600,
        conditions=[_condition("Failed", reason="BackoffLimitExceeded")],
    )
    pods = [
        _make_pod("meho-migrate-aaaaa", created=_NOW - timedelta(minutes=4), phase="Failed"),
        _make_pod("meho-migrate-bbbbb", created=_NOW - timedelta(minutes=2), phase="Failed"),
    ]
    info = job_info(job, pods, now=_NOW)
    assert info["state"] == "failed"
    assert info["pods_failed"] == 2
    assert info["conditions"] == [
        {
            "type": "Failed",
            "status": "True",
            "reason": "BackoffLimitExceeded",
            "message": "Failed condition",
            "last_transition_time": "2026-10-24T12:00:00Z",
        }
    ]
    assert info["backoff_limit"] == 1
    assert info["active_deadline_seconds"] == 600
    assert info["containers"] == [{"name": "migrate", "image": "ghcr.io/example/meho:1.0"}]
    assert info["pod_selector"] == "batch.kubernetes.io/controller-uid=uid-1"
    assert [p["name"] for p in info["pods"]] == ["meho-migrate-bbbbb", "meho-migrate-aaaaa"]
    assert set(info["pods"][0]) == {
        "name",
        "namespace",
        "status",
        "ready",
        "restarts",
        "age_seconds",
        "node",
        "ip",
    }
    assert info["pods_truncated"] is False


def test_job_info_cuts_pods_to_cap_newest_first() -> None:
    pods = [
        _make_pod(f"p-{i:03d}", created=_NOW - timedelta(seconds=i))
        for i in range(JOB_INFO_MAX_PODS + 10)
    ]
    info = job_info(_make_job(), list(reversed(pods)), now=_NOW)
    assert len(info["pods"]) == JOB_INFO_MAX_PODS
    assert info["pods"][0]["name"] == "p-000"
    assert info["pods"][-1]["name"] == f"p-{JOB_INFO_MAX_PODS - 1:03d}"
    assert info["pods_truncated"] is True


# ---------------------------------------------------------------------------
# k8s.job.list handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_job_list_returns_rows_and_total() -> None:
    jobs = [_make_job(name="migrate"), _make_job(name="backup", active=1)]
    connector = _make_connector()
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        batch_cls.return_value.list_namespaced_job = AsyncMock(return_value=_job_list(jobs))
        result = await connector.k8s_job_list(_make_operator(), _TARGET, {"namespace": "meho"})

    assert result["total"] == 2
    assert {row["name"] for row in result["rows"]} == {"migrate", "backup"}
    assert "next_continue" not in result
    kwargs = batch_cls.return_value.list_namespaced_job.call_args.kwargs
    assert kwargs == {"namespace": "meho"}


@pytest.mark.asyncio
async def test_k8s_job_list_all_namespaces_routes_to_all_namespaces_api() -> None:
    connector = _make_connector()
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        batch_cls.return_value.list_job_for_all_namespaces = AsyncMock(
            return_value=_job_list([_make_job()])
        )
        batch_cls.return_value.list_namespaced_job = AsyncMock()
        result = await connector.k8s_job_list(
            _make_operator(), _TARGET, {"all_namespaces": True, "label_selector": "app=backup"}
        )

    assert result["total"] == 1
    batch_cls.return_value.list_namespaced_job.assert_not_awaited()
    kwargs = batch_cls.return_value.list_job_for_all_namespaces.call_args.kwargs
    assert kwargs == {"label_selector": "app=backup"}


@pytest.mark.asyncio
async def test_k8s_job_list_pagination_returns_next_continue() -> None:
    connector = _make_connector()
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        batch_cls.return_value.list_namespaced_job = AsyncMock(
            return_value=_job_list([_make_job()], continue_token="tok-2")
        )
        result = await connector.k8s_job_list(
            _make_operator(), _TARGET, {"namespace": "meho", "limit": 1, "continue_token": "tok-1"}
        )

    assert result["next_continue"] == "tok-2"
    kwargs = batch_cls.return_value.list_namespaced_job.call_args.kwargs
    assert kwargs == {"namespace": "meho", "limit": 1, "_continue": "tok-1"}


# ---------------------------------------------------------------------------
# k8s.job.info handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_job_info_exact_match_lists_pods_by_job_selector() -> None:
    jobs = [_make_job(name="meho-migrate"), _make_job(name="meho-migrate-old")]
    pods = [_make_pod("meho-migrate-x1", created=_NOW)]
    connector = _make_connector()
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls, patch(_CORE_V1) as core_cls:
        batch_cls.return_value.list_namespaced_job = AsyncMock(return_value=_job_list(jobs))
        core_cls.return_value.list_namespaced_pod = AsyncMock(return_value=_pod_list(pods))
        result = await connector.k8s_job_info(
            _make_operator(), _TARGET, {"job_name": "meho-migrate", "namespace": "meho"}
        )

    assert result["name"] == "meho-migrate"
    assert [p["name"] for p in result["pods"]] == ["meho-migrate-x1"]
    core_cls.return_value.list_namespaced_pod.assert_awaited_once_with(
        namespace="meho", label_selector="batch.kubernetes.io/controller-uid=uid-1"
    )


@pytest.mark.asyncio
async def test_k8s_job_info_unique_prefix_resolves() -> None:
    jobs = [_make_job(name="meho-migrate-28471"), _make_job(name="backup-1")]
    connector = _make_connector()
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls, patch(_CORE_V1) as core_cls:
        batch_cls.return_value.list_namespaced_job = AsyncMock(return_value=_job_list(jobs))
        core_cls.return_value.list_namespaced_pod = AsyncMock(return_value=_pod_list([]))
        result = await connector.k8s_job_info(
            _make_operator(), _TARGET, {"job_name": "meho-migrate", "namespace": "meho"}
        )
    assert result["name"] == "meho-migrate-28471"
    assert result["pods"] == []
    assert result["pods_truncated"] is False


@pytest.mark.asyncio
async def test_k8s_job_info_ambiguous_prefix_raises_with_candidates() -> None:
    jobs = [_make_job(name="backup-1"), _make_job(name="backup-2")]
    connector = _make_connector()
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls, patch(_CORE_V1):
        batch_cls.return_value.list_namespaced_job = AsyncMock(return_value=_job_list(jobs))
        with pytest.raises(AmbiguousPrefixError) as excinfo:
            await connector.k8s_job_info(
                _make_operator(), _TARGET, {"job_name": "backup", "namespace": "meho"}
            )
    assert excinfo.value.candidates == ["backup-1", "backup-2"]


@pytest.mark.asyncio
async def test_k8s_job_info_not_found_raises() -> None:
    connector = _make_connector()
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls, patch(_CORE_V1):
        batch_cls.return_value.list_namespaced_job = AsyncMock(return_value=_job_list([]))
        with pytest.raises(WorkloadNotFoundError):
            await connector.k8s_job_info(
                _make_operator(), _TARGET, {"job_name": "nope", "namespace": "meho"}
            )


@pytest.mark.asyncio
async def test_k8s_job_info_truncates_many_pods_through_the_handler() -> None:
    pods = [
        _make_pod(f"p-{i:03d}", created=_NOW - timedelta(seconds=i))
        for i in range(JOB_INFO_MAX_PODS + 1)
    ]
    connector = _make_connector()
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls, patch(_CORE_V1) as core_cls:
        batch_cls.return_value.list_namespaced_job = AsyncMock(
            return_value=_job_list([_make_job()])
        )
        core_cls.return_value.list_namespaced_pod = AsyncMock(return_value=_pod_list(pods))
        result = await connector.k8s_job_info(
            _make_operator(), _TARGET, {"job_name": "meho-migrate", "namespace": "meho"}
        )
    assert len(result["pods"]) == JOB_INFO_MAX_PODS
    assert result["pods_truncated"] is True

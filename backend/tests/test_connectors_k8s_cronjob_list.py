# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the K8s CronJob read op ``k8s.cronjob.list``."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator
from kubernetes_asyncio.client.models import (
    V1CronJob,
    V1CronJobList,
    V1CronJobSpec,
    V1CronJobStatus,
    V1JobTemplateSpec,
    V1ListMeta,
    V1ObjectMeta,
)

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KUBERNETES_OPS, KubernetesConnector
from meho_backplane.connectors.kubernetes.kubeconfig import KubernetesTargetLike
from meho_backplane.connectors.kubernetes.ops_cronjob_list import (
    CRONJOB_LIST_OPS,
    cronjob_row,
)
from meho_backplane.settings import get_settings

_BATCH_V1 = "meho_backplane.connectors.kubernetes.ops_cronjob_list.client.BatchV1Api"
_LAST = datetime(2026, 11, 1, 3, 0, 0, tzinfo=UTC)


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


_TARGET = _StubTarget(name="k8s-test", host="k8s.test.invalid", port=6443, secret_ref="k8s/test")


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


def _make_operator() -> Operator:
    return Operator(
        sub="op-cronjob-test",
        name="CronJob Test Operator",
        email="op@example.test",
        raw_jwt="stub-jwt",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


def _patch_kubeconfig() -> Any:
    return patch.object(
        KubernetesConnector,
        "_get_api_client",
        AsyncMock(return_value=object()),
    )


def _cronjob(
    name: str = "backup",
    *,
    schedule: str | None = "0 3 * * *",
    suspend: bool | None = None,
    last: datetime | None = None,
) -> V1CronJob:
    return V1CronJob(
        metadata=V1ObjectMeta(name=name, namespace="meho"),
        spec=V1CronJobSpec(schedule=schedule, suspend=suspend, job_template=V1JobTemplateSpec()),
        status=V1CronJobStatus(last_schedule_time=last),
    )


def _cronjob_list(items: list[V1CronJob], token: str | None = None) -> V1CronJobList:
    return V1CronJobList(items=items, metadata=V1ListMeta(_continue=token))


def test_cronjob_list_op_is_collected_as_a_safe_workload_read() -> None:
    assert "k8s.cronjob.list" in {op.op_id for op in KUBERNETES_OPS}
    (op,) = CRONJOB_LIST_OPS
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key == "workload"
    assert op.llm_instructions is not None
    assert inspect.iscoroutinefunction(getattr(_make_connector(), op.handler_attr))
    Draft202012Validator.check_schema(op.parameter_schema)
    Draft202012Validator.check_schema(op.response_schema)


def test_cronjob_row_fields() -> None:
    row = cronjob_row(_cronjob(suspend=True, last=_LAST))
    assert row == {
        "name": "backup",
        "namespace": "meho",
        "schedule": "0 3 * * *",
        "suspended": True,
        "last_schedule_time": "2026-11-01T03:00:00Z",
    }


def test_cronjob_row_edge_inputs_match_the_response_schema() -> None:
    """Unset suspend, never ran, and a missing status or spec still give a valid row."""
    (op,) = CRONJOB_LIST_OPS
    row_schema = op.response_schema["properties"]["rows"]["items"]
    validator = Draft202012Validator(row_schema)

    never_ran = cronjob_row(_cronjob(suspend=None, last=None))
    assert never_ran["suspended"] is False
    assert never_ran["last_schedule_time"] is None

    bare = cronjob_row(
        SimpleNamespace(metadata=V1ObjectMeta(name="x", namespace="meho"), spec=None, status=None)  # type: ignore[arg-type]
    )
    assert bare["schedule"] is None
    assert bare["suspended"] is False
    assert bare["last_schedule_time"] is None

    for row in (never_ran, bare, cronjob_row(_cronjob(last=_LAST))):
        validator.validate(row)


@pytest.mark.asyncio
async def test_k8s_cronjob_list_returns_rows_for_a_namespace() -> None:
    items = [_cronjob("backup"), _cronjob("report", suspend=True)]
    connector = _make_connector()
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        batch_cls.return_value.list_namespaced_cron_job = AsyncMock(
            return_value=_cronjob_list(items)
        )
        result = await connector.k8s_cronjob_list(_make_operator(), _TARGET, {"namespace": "meho"})

    assert result["total"] == 2
    assert [r["name"] for r in result["rows"]] == ["backup", "report"]
    assert "next_continue" not in result
    kwargs = batch_cls.return_value.list_namespaced_cron_job.call_args.kwargs
    assert kwargs == {"namespace": "meho"}


@pytest.mark.asyncio
async def test_k8s_cronjob_list_empty_namespace_is_an_empty_list() -> None:
    connector = _make_connector()
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        batch_cls.return_value.list_namespaced_cron_job = AsyncMock(
            return_value=V1CronJobList(items=[], metadata=V1ListMeta())
        )
        result = await connector.k8s_cronjob_list(_make_operator(), _TARGET, {"namespace": "meho"})
    assert result == {"rows": [], "total": 0}


@pytest.mark.asyncio
async def test_k8s_cronjob_list_all_namespaces_uses_the_cluster_wide_call() -> None:
    connector = _make_connector()
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        batch_cls.return_value.list_cron_job_for_all_namespaces = AsyncMock(
            return_value=_cronjob_list([_cronjob()])
        )
        batch_cls.return_value.list_namespaced_cron_job = AsyncMock()
        result = await connector.k8s_cronjob_list(
            _make_operator(), _TARGET, {"all_namespaces": True, "label_selector": "app=backup"}
        )

    assert result["total"] == 1
    batch_cls.return_value.list_namespaced_cron_job.assert_not_awaited()
    kwargs = batch_cls.return_value.list_cron_job_for_all_namespaces.call_args.kwargs
    assert kwargs == {"label_selector": "app=backup"}


@pytest.mark.asyncio
async def test_k8s_cronjob_list_second_page_passes_and_returns_the_cursor() -> None:
    connector = _make_connector()
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        batch_cls.return_value.list_namespaced_cron_job = AsyncMock(
            return_value=_cronjob_list([_cronjob()], token="tok-2")
        )
        result = await connector.k8s_cronjob_list(
            _make_operator(), _TARGET, {"namespace": "meho", "limit": 1, "continue_token": "tok-1"}
        )

    assert result["next_continue"] == "tok-2"
    kwargs = batch_cls.return_value.list_namespaced_cron_job.call_args.kwargs
    assert kwargs == {"namespace": "meho", "limit": 1, "_continue": "tok-1"}


@pytest.mark.asyncio
async def test_k8s_cronjob_list_api_failure_is_raised_not_an_empty_list() -> None:
    connector = _make_connector()
    with _patch_kubeconfig(), patch(_BATCH_V1) as batch_cls:
        batch_cls.return_value.list_namespaced_cron_job = AsyncMock(
            side_effect=RuntimeError("forbidden")
        )
        with pytest.raises(RuntimeError, match="forbidden"):
            await connector.k8s_cronjob_list(_make_operator(), _TARGET, {"namespace": "meho"})

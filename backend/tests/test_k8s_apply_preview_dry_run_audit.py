# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit row of a ``k8s.apply`` preview that sends a dry run.

Each test asks for the preview through a public entry point: the REST route
``POST /api/v1/operations/preview`` or the MCP tool ``preview_operation``,
with ``AUDIT_WRAPPER_ROWS_RETIRED`` off and on.

* With ``K8S_APPLY_PREVIEW_DRY_RUN`` on, the preview writes one audit row of
  the class read: the caller, the tenant, the target, the operation and the
  number of documents. It writes no row of the apply and no approval
  request. With the wrapper rows retired that row is the only one; with them
  kept the request row is written as before.
* The row is written also when the cluster does not answer in time and when
  the cluster login fails.
* With the switch off, the preview writes the same rows as before.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.api.v1.operations import router as operations_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.main import app as production_app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.middleware import RequestContextMiddleware, verify_jwt_and_bind
from meho_backplane.operations import _composite_preview, meta_tools
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import post_mcp
from tests.test_k8s_apply_card_through_dispatch import (
    _CONNECTOR_ID,
    _kubernetes_registry,
    _login,
    _Target,
)
from tests.test_k8s_apply_preview_dry_run import _setup, _StandInCluster
from tests.test_ui_approvals import _bff_env

__all__ = ["_bff_env", "_kubernetes_registry"]

_MANIFEST = "---\n".join(
    f"apiVersion: v1\nkind: ConfigMap\nmetadata: {{name: cm-{n}, namespace: prod}}\n"
    for n in range(3)
)
_TARGET = _Target(id=uuid.UUID("00000000-0000-0000-0000-0000000a7a11"))


@pytest.fixture(params=[False, True], ids=["wrapper-rows-kept", "wrapper-rows-retired"])
def retired(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[bool]:
    monkeypatch.setenv("REQUEST_USAGE_RETENTION_DAYS", "30")
    monkeypatch.setenv("AUDIT_WRAPPER_ROWS_RETIRED", "true" if request.param else "false")
    monkeypatch.setattr(
        meta_tools, "_resolve_target_or_error", AsyncMock(return_value=(_TARGET, None))
    )
    get_settings.cache_clear()
    yield request.param
    get_settings.cache_clear()


def _switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K8S_APPLY_PREVIEW_DRY_RUN", "true")
    get_settings.cache_clear()


def _rows(caller: Operator) -> list[AuditLog]:
    async def _do() -> list[AuditLog]:
        async with get_sessionmaker()() as session:
            result = await session.execute(
                select(AuditLog)
                .where(AuditLog.operator_sub == caller.sub)
                .order_by(AuditLog.occurred_at)
            )
            return list(result.scalars().all())

    return asyncio.run(_do())


def _approval_requests() -> int:
    async def _do() -> int:
        async with get_sessionmaker()() as session:
            count = await session.execute(select(func.count()).select_from(ApprovalRequest))
            return int(count.scalar_one())

    return asyncio.run(_do())


def _body() -> dict[str, Any]:
    return {
        "connector_id": _CONNECTOR_ID,
        "op_id": "k8s.apply",
        "target": _TARGET.name,
        "params": {"manifest": _MANIFEST},
    }


def _rest_preview(caller: Operator) -> dict[str, Any]:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(operations_router)

    async def _verify() -> Operator:
        structlog.contextvars.bind_contextvars(
            operator_sub=caller.sub, tenant_id=str(caller.tenant_id)
        )
        return caller

    app.dependency_overrides[verify_jwt_and_bind] = _verify
    with TestClient(app) as client:
        response = client.post("/api/v1/operations/preview", json=_body())
    assert response.status_code == 200, response.text
    answer: dict[str, Any] = response.json()
    return answer


def _mcp_preview(caller: Operator) -> dict[str, Any]:
    async def _verify() -> Operator:
        structlog.contextvars.bind_contextvars(
            operator_sub=caller.sub, tenant_id=str(caller.tenant_id)
        )
        return caller

    production_app.dependency_overrides[verify_mcp_jwt_and_bind] = _verify
    try:
        # No app start: the start checks every connector, and this test
        # registers only the Kubernetes one.
        response = post_mcp(
            TestClient(production_app),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "preview_operation", "arguments": _body()},
            },
        )
    finally:
        production_app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
    assert response.status_code == 200, response.text
    answer: dict[str, Any] = response.json()["result"]["structuredContent"]
    return answer


_PREVIEWS = {"rest": _rest_preview, "mcp": _mcp_preview}


def _check_read_row(row: AuditLog, caller: Operator, result_status: str) -> None:
    assert row.event_class == "operation_execution"
    assert (row.method, row.path) == ("PREVIEW", "k8s.apply")
    assert row.payload["op_class"] == "read"
    assert row.payload["op_id"] == "k8s.apply"
    assert row.payload["documents"] == 3
    assert row.payload["result_status"] == result_status
    assert row.operator_sub == caller.sub
    assert row.tenant_id == caller.tenant_id
    assert row.target_id == _TARGET.id


def _check_rows(rows: list[AuditLog], caller: Operator, retired: bool, result_status: str) -> None:
    reads = [row for row in rows if row.method == "PREVIEW"]
    assert len(reads) == 1, [(r.event_class, r.method, r.path) for r in rows]
    _check_read_row(reads[0], caller, result_status)
    assert not [row for row in rows if row.method == "DISPATCH"]
    others = [row.event_class for row in rows if row.method != "PREVIEW"]
    assert others == ([] if retired else ["legacy_request"])


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_a_dry_run_preview_writes_one_read_row(
    monkeypatch: pytest.MonkeyPatch, retired: bool, surface: str
) -> None:
    _switch_on(monkeypatch)
    cluster = _StandInCluster()
    _setup(monkeypatch, cluster)
    caller = _login(f"audit-{surface}")
    requests_before = _approval_requests()

    answer = _PREVIEWS[surface](caller)

    assert answer["plan_shown"] is True, answer
    assert len(cluster.requests) == 3
    _check_rows(_rows(caller), caller, retired, "ok")
    assert _approval_requests() == requests_before


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_a_cluster_that_does_not_answer_in_time_is_recorded(
    monkeypatch: pytest.MonkeyPatch, retired: bool, surface: str
) -> None:
    _switch_on(monkeypatch)
    monkeypatch.setattr(_composite_preview, "TARGET_READ_TIMEOUT_SECONDS", 0.2)
    cluster = _StandInCluster(hang=True)
    _setup(monkeypatch, cluster)
    caller = _login(f"audit-late-{surface}")

    answer = _PREVIEWS[surface](caller)

    assert answer["plan_note"] == _composite_preview.TARGET_READ_TIMEOUT_NOTE
    _check_rows(_rows(caller), caller, retired, "timeout")


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_a_failed_cluster_login_is_recorded(
    monkeypatch: pytest.MonkeyPatch, retired: bool, surface: str
) -> None:
    _switch_on(monkeypatch)
    cluster = _StandInCluster()
    _setup(monkeypatch, cluster)
    monkeypatch.setattr(
        KubernetesConnector,
        "_get_api_client",
        AsyncMock(side_effect=RuntimeError("the cluster login was refused")),
    )
    caller = _login(f"audit-login-{surface}")

    answer = _PREVIEWS[surface](caller)

    assert answer["plan_shown"] is False
    assert "the cluster login was refused" in answer["plan_note"]
    _check_rows(_rows(caller), caller, retired, "error")


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_with_the_switch_off_the_rows_are_as_before(
    monkeypatch: pytest.MonkeyPatch, retired: bool, surface: str
) -> None:
    cluster = _StandInCluster()
    _setup(monkeypatch, cluster)
    caller = _login(f"audit-off-{surface}")

    answer = _PREVIEWS[surface](caller)

    assert answer["plan_shown"] is False
    assert cluster.requests == []
    rows = _rows(caller)
    assert [row.event_class for row in rows] == ([] if retired else ["legacy_request"])

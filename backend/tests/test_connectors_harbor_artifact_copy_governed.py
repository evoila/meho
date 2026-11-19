# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``harbor.artifact.copy`` through the normal operation and approval path.

The call waits for approval and copies nothing. After one approval by a
different person, the copy runs once, under the approver, and writes its
audit row. ``harbor.artifact.list`` then reads the copied digest back. The
Harbor HTTP calls are faked on a ``HarborConnector`` subclass, so the
dispatcher, the approval queue and the audit write run as in production.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.harbor import HarborConnector
from meho_backplane.connectors.harbor.ops_artifact_copy import (
    register_harbor_artifact_copy_operation,
)
from meho_backplane.connectors.harbor.typed_ops import register_harbor_typed_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "harbor-rest-2.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-00000000a0c5")
_OP_ID = "harbor.artifact.copy"
_TARGET_NAME = "test-harbor"
_DIGEST = "sha256:" + "c" * 64
_PARAMS = {"project": "apps", "repository": "nginx", "from": "cache/library/nginx:1.27"}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_caches() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
def _stub_embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


class _FakeHarbor(HarborConnector):
    """A HarborConnector whose HTTP calls are answered from memory.

    The source tag points to ``_DIGEST``. The target holds nothing until the
    copy request arrives; after it, the target lists the copied digest.
    """

    def __init__(self) -> None:
        super().__init__()
        self.copies: list[tuple[str, dict[str, Any] | None]] = []
        self.target_artifacts: list[dict[str, Any]] = []

    async def _get_json(  # type: ignore[override]
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        if path.startswith("/api/v2.0/projects/cache/"):
            return {"digest": _DIGEST}
        if path.endswith("/projects/apps/repositories/nginx/artifacts"):
            return list(self.target_artifacts)
        held = [a for a in self.target_artifacts if path.endswith(a["digest"].replace(":", "%3A"))]
        if held:
            return held[0]
        request = httpx.Request("GET", f"https://harbor.example.test{path}")
        raise httpx.HTTPStatusError(
            "not found", request=request, response=httpx.Response(404, request=request)
        )

    async def _post_json(  # type: ignore[override]
        self, target: Any, path: str, *, operator: Operator, params: Any = None, **_: Any
    ) -> dict[str, Any]:
        self.copies.append((path, params))
        digest = str(params["from"]).rsplit("@", 1)[1]
        self.target_artifacts.append({"digest": digest, "tags": [{"name": "1.27"}]})
        return {}

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:  # type: ignore[override]
        return {}


def _operator(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name="harbor copy governed flow",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_target() -> None:
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="harbor",
                host="harbor.example.test",
                port=443,
                fqdn=None,
                secret_ref="meho/testing/harbor/test-harbor",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "2.x"},
                preferred_impl_id="harbor-rest",
                notes="seeded by test_connectors_harbor_artifact_copy_governed",
            )
        )
        await s.commit()


async def test_copy_waits_for_approval_runs_once_audits_and_reads_back(
    _stub_embedding: AsyncMock,
) -> None:
    harbor = _FakeHarbor()
    await register_harbor_typed_operations(embedding_service=_stub_embedding)
    await register_harbor_artifact_copy_operation(embedding_service=_stub_embedding)
    _CONNECTOR_INSTANCE_CACHE[HarborConnector] = harbor  # type: ignore[assignment]
    await _seed_target()

    requester = _operator("op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "target": _TARGET_NAME,
        "params": dict(_PARAMS),
    }

    # 1) The call waits for approval. Nothing is copied yet.
    call = await call_operation(requester, args)
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])
    assert harbor.copies == []

    # 2) A different person approves once; the copy runs once, by digest.
    approver = _operator("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["copied"] is True
    assert resume.result["digest"] == _DIGEST
    assert resume.result["read_back"] == "harbor.artifact.list"
    assert harbor.copies == [
        (
            "/api/v2.0/projects/apps/repositories/nginx/artifacts",
            {"from": f"cache/library/nginx@{_DIGEST}"},
        )
    ]

    # 3) The copy wrote its audit row, under the approver.
    async with get_sessionmaker()() as s:
        executed = (
            (await s.execute(select(AuditLog).where(AuditLog.path == _OP_ID))).scalars().all()
        )
    assert [a.operator_sub for a in executed] == ["op-approver"]

    # 4) harbor.artifact.list reads the copied digest back from the target.
    listed = await call_operation(
        requester,
        {
            "connector_id": _CONNECTOR_ID,
            "op_id": "harbor.artifact.list",
            "target": _TARGET_NAME,
            "params": {"project_name": "apps", "repository_name": "nginx"},
        },
    )
    assert listed["status"] == "ok", listed
    assert [a["digest"] for a in listed["result"]] == [_DIGEST]

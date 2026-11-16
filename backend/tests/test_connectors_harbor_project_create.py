# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``harbor.project.create`` with a faked Harbor API."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.harbor import HarborConnector
from meho_backplane.connectors.harbor.ops_project_create import (
    _PROJECT_CREATE_PARAMETER_SCHEMA,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_PROJECTS_URL = "https://harbor.test.invalid/api/v2.0/projects"


@pytest.fixture(autouse=True)
def _clean_harbor_registry() -> None:
    clear_registry()
    register_connector_v2(
        product=HarborConnector.product,
        version=HarborConnector.version,
        impl_id=HarborConnector.impl_id,
        cls=HarborConnector,
    )
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget(
    name="harbor-test",
    host="harbor.test.invalid",
    port=443,
    secret_ref="targets/harbor/harbor-test",
)


def _operator() -> Operator:
    return Operator(
        sub="op-project-create",
        name="Harbor Project Operator",
        email=None,
        raw_jwt="op.project.create.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0b4"),
        tenant_role=TenantRole.OPERATOR,
    )


def _install_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_client(
        monkeypatch,
        secret={"username": "svc-harbor-canary", "password": "p4ss-canary-project-create"},
    )


@pytest.mark.asyncio
async def test_project_create_posts_private_project_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    sent: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> respx.MockResponse:
        sent.update(json.loads(request.content))
        return respx.MockResponse(201)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(_PROJECTS_URL).mock(side_effect=_capture)
        result = await connector.project_create(
            _operator(), _TARGET, {"project_name": "demo-images"}
        )

    assert sent == {"project_name": "demo-images", "metadata": {"public": "false"}}
    assert result == {
        "project_name": "demo-images",
        "created": True,
        "already_existed": False,
        "read_back": "harbor.project.info",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_project_create_public_and_storage_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    sent: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> respx.MockResponse:
        sent.update(json.loads(request.content))
        return respx.MockResponse(201)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(_PROJECTS_URL).mock(side_effect=_capture)
        await connector.project_create(
            _operator(),
            _TARGET,
            {"project_name": "shared", "public": True, "storage_limit": 1073741824},
        )

    assert sent["metadata"] == {"public": "true"}
    assert sent["storage_limit"] == 1073741824
    await connector.aclose()


@pytest.mark.asyncio
async def test_project_create_taken_name_reports_created_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_PROJECTS_URL).mock(
            return_value=respx.MockResponse(
                409, json={"errors": [{"code": "CONFLICT", "message": "already exists"}]}
            )
        )
        result = await connector.project_create(
            _operator(), _TARGET, {"project_name": "demo-images"}
        )

    assert route.call_count == 1
    assert result["created"] is False
    assert result["already_existed"] is True
    assert result["read_back"] == "harbor.project.info"
    await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 500, 502])
async def test_project_create_other_errors_are_raised(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_PROJECTS_URL).mock(return_value=respx.MockResponse(status))
        with pytest.raises(httpx.HTTPStatusError):
            await connector.project_create(_operator(), _TARGET, {"project_name": "demo-images"})
    assert route.call_count == 1
    await connector.aclose()


def test_project_create_schema_refuses_bad_input() -> None:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(_PROJECT_CREATE_PARAMETER_SCHEMA)
    assert not list(validator.iter_errors({"project_name": "demo-images"}))
    for bad in (
        {},
        {"project_name": ""},
        {"project_name": "  "},
        {"project_name": "x", "public": "false"},
        {"project_name": "x", "storage_limit": -2},
        {"project_name": "x", "extra": 1},
    ):
        assert list(validator.iter_errors(bad)), bad


# ---------------------------------------------------------------------------
# The shared dispatcher: wait for approval, create once, audit, read back
# ---------------------------------------------------------------------------


class _FakeHarborProjects:
    """A small fake of Harbor's project API that keeps the projects it made."""

    def __init__(self) -> None:
        self.projects: dict[str, dict[str, Any]] = {}
        self.post_count = 0

    def create(self, request: httpx.Request) -> respx.MockResponse:
        self.post_count += 1
        body = json.loads(request.content)
        name = body["project_name"]
        if name in self.projects:
            return respx.MockResponse(409)
        self.projects[name] = {"name": name, "metadata": body["metadata"]}
        return respx.MockResponse(201)

    def info(self, request: httpx.Request, name: str) -> respx.MockResponse:
        if name not in self.projects:
            return respx.MockResponse(404)
        return respx.MockResponse(200, json=self.projects[name])


async def _seed_dispatch_target(tenant_id: UUID) -> None:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import Target as TargetORM

    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid4(),
                tenant_id=tenant_id,
                name=_TARGET.name,
                aliases=[],
                product="harbor",
                host=_TARGET.host,
                port=443,
                fqdn=None,
                secret_ref=_TARGET.secret_ref,
                auth_model=AuthModel.SHARED_SERVICE_ACCOUNT.value,
                vpn_required=False,
                extras={},
                fingerprint={"version": "2.x"},
                preferred_impl_id="harbor-rest",
                notes="seeded by test_connectors_harbor_project_create",
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_project_create_waits_for_approval_then_audits_and_reads_back_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import select

    from meho_backplane.connectors.harbor.ops_project_create import (
        register_harbor_project_create_operation,
    )
    from meho_backplane.connectors.harbor.typed_ops import register_harbor_typed_operations
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import ApprovalRequest, AuditLog
    from meho_backplane.operations import reset_dispatcher_caches
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
    from meho_backplane.operations.approval_queue import (
        approve_request,
        resume_dispatch_after_approval,
    )
    from meho_backplane.operations.meta_tools import call_operation

    _install_vault(monkeypatch)
    reset_dispatcher_caches()
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_harbor_typed_operations(embedding_service=embedding)
    await register_harbor_project_create_operation(embedding_service=embedding)
    connector = HarborConnector()
    _CONNECTOR_INSTANCE_CACHE[HarborConnector] = connector
    requester = _operator()
    await _seed_dispatch_target(requester.tenant_id)

    harbor = _FakeHarborProjects()
    args = {
        "connector_id": "harbor-rest-2.x",
        "target": _TARGET.name,
        "params": {"project_name": "demo-images"},
    }
    try:
        with respx.mock(assert_all_called=False) as mock:
            mock.post(_PROJECTS_URL).mock(side_effect=harbor.create)
            mock.get(url__regex=rf"^{_PROJECTS_URL}/(?P<name>[^/?]+)$").mock(
                side_effect=harbor.info
            )

            # 1) The call waits for approval. Harbor gets no POST yet.
            call = await call_operation(requester, {**args, "op_id": "harbor.project.create"})
            assert call["status"] == "awaiting_approval", call
            request_id = UUID(call["extras"]["approval_request_id"])
            assert harbor.post_count == 0

            # 2) A second person approves once, and the create runs once.
            approver = Operator(
                sub="op-project-approver",
                name="Harbor Project Approver",
                email=None,
                raw_jwt="op.project.approve.jwt",
                tenant_id=requester.tenant_id,
                tenant_role=TenantRole.OPERATOR,
            )
            async with get_sessionmaker()() as s:
                approved = await approve_request(s, request_id, operator=approver, params=None)
                await s.commit()
            assert approved.status == "approved"
            async with get_sessionmaker()() as s:
                row = await s.get(ApprovalRequest, request_id)
                assert row is not None
                resume = await resume_dispatch_after_approval(
                    operator=approver, request=row, params=None
                )
            assert resume.status == "ok", resume.error
            assert resume.result["created"] is True
            assert harbor.post_count == 1

            # 3) The audit row of the create records success.
            async with get_sessionmaker()() as s:
                audit_rows = list(
                    (
                        await s.execute(
                            select(AuditLog).where(AuditLog.path == "harbor.project.create")
                        )
                    )
                    .scalars()
                    .all()
                )
            assert [
                r.payload.get("result_status") for r in audit_rows if r.method == "DISPATCH"
            ] == ["ok"]

            # 4) harbor.project.info reads the project back as private.
            info = await call_operation(
                requester,
                {**args, "op_id": "harbor.project.info"},
            )
            assert info["status"] == "ok", info
            assert info["result"]["name"] == "demo-images"
            assert info["result"]["metadata"]["public"] == "false"
            assert harbor.post_count == 1
    finally:
        _CONNECTOR_INSTANCE_CACHE.pop(HarborConnector, None)
        reset_dispatcher_caches()
        await connector.aclose()

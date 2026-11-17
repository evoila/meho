# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``harbor.robot.create`` with ``vault_target`` through park, approval and run.

* The stored approval request shows where the new secret goes.
* A person approves each call: no standing grant and no run approval
  covers it.
* When Vault refuses the write after the approval, the caller's answer and
  the audit row say whether the new robot was deleted, and the secret
  appears in neither.

Harbor is faked with respx and Vault with the in-process fake. The secret is
a made-up value.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import hvac.exceptions
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.harbor import HarborConnector
from meho_backplane.connectors.harbor.ops import register_harbor_robot_operations
from meho_backplane.connectors.harbor.session import HarborTargetLike
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.op_classes import needs_per_call_approval
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.run_approval_schemas import RunApprovalEntry
from meho_backplane.operations.run_approvals import _entry_refusal
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
)
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

from ._vault_fakes import install_fake_client

_OP_ID = "harbor.robot.create"
_CONNECTOR_ID = "harbor-rest-2.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-00000000a0b4")
_TARGET_NAME = "harbor-vault-approval"
_HOST = "harbor.test.invalid"
_ROBOTS_URL = f"https://{_HOST}/api/v2.0/robots"
_ROBOT_URL = f"{_ROBOTS_URL}/7"
_NEW_SECRET = "made-up-approved-robot-secret-zz9"
_DEST_PATH = f"tenants/{_TENANT_ID}/ci/harbor"
_PARAMS: dict[str, Any] = {
    "name": "bot",
    "project": "proj",
    "duration": -1,
    "vault_target": f"vault:{_DEST_PATH}#secret",
}


async def _stub_credentials_loader(
    _target: HarborTargetLike, _operator: Operator
) -> dict[str, str]:
    return {"username": "admin", "password": "test-password"}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def harbor(embedding: AsyncMock) -> AsyncIterator[HarborConnector]:
    reset_dispatcher_caches()
    await register_harbor_robot_operations(embedding_service=embedding)
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="harbor",
                version="2.11.0",
                host=_HOST,
                port=443,
                secret_ref=f"harbor/{_TARGET_NAME}",
                auth_model="shared_service_account",
            )
        )
        await session.commit()
    connector = HarborConnector(credentials_loader=_stub_credentials_loader)
    _CONNECTOR_INSTANCE_CACHE[HarborConnector] = connector
    yield connector
    await connector.aclose()
    reset_dispatcher_caches()


def _person(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name=f"Person {sub}",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _install_vault(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Any:
    fake = install_fake_client(
        monkeypatch, secret={"username": "svc", "password": "svc-pass"}, **kwargs
    )
    fake.sys.default_capabilities = ["create", "read", "update", "patch"]
    return fake


async def _park(requester: Operator) -> UUID:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    parked = await dispatch(
        operator=requester,
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=target,
        params=dict(_PARAMS),
    )
    assert parked.status == "awaiting_approval", parked.error
    return UUID(parked.extras["approval_request_id"])


async def _approve_and_run(request_id: UUID, approver: Operator) -> Any:
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver)
        await session.commit()
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row)


async def _error_audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.tenant_id == _TENANT_ID)))
            .scalars()
            .all()
        )
    return [
        r
        for r in rows
        if isinstance(r.payload, dict)
        and r.payload.get("op_id") == _OP_ID
        and r.payload.get("result_status") == "error"
    ]


async def _everything_stored() -> str:
    async with get_sessionmaker()() as session:
        audits = (await session.execute(select(AuditLog))).scalars().all()
        approvals = (await session.execute(select(ApprovalRequest))).scalars().all()
    return json.dumps(
        [
            {c.name: getattr(row, c.name) for c in type(row).__table__.columns}
            for row in [*audits, *approvals]
        ],
        default=str,
    )


# ---------------------------------------------------------------------------
# The approval request shows where the secret goes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parked_request_shows_where_the_secret_goes(
    harbor: HarborConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_vault(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        create = mock.post(_ROBOTS_URL).respond(201, json={})
        request_id = await _park(_person("op-requester"))
    assert not create.called

    async with get_sessionmaker()() as session:
        pending = await session.get(ApprovalRequest, request_id)
    assert pending is not None
    preview = pending.proposed_effect["preview"]
    assert isinstance(preview, dict)
    assert preview["secret_goes_to"] == {
        "kind": "vault",
        "mount": "secret",
        "path": _DEST_PATH,
        "field": "secret",
        "note": (
            "An existing value in this field is replaced. The other fields of the Vault item stay."
        ),
    }


# ---------------------------------------------------------------------------
# A person approves each call
# ---------------------------------------------------------------------------


def test_robot_create_needs_a_person_for_each_call() -> None:
    assert needs_per_call_approval(_OP_ID)
    assert not needs_per_call_approval("harbor.robot.delete")


@pytest.mark.asyncio
async def test_no_standing_grant_and_no_run_approval_cover_robot_create(
    harbor: HarborConnector,
) -> None:
    with pytest.raises(GrantValidationError, match="a person approves each call"):
        await ServicePrincipalGrantService().create(
            _TENANT_ID,
            "fixture-admin",
            ServiceGrantCreate(
                principal_sub="svc:robot-maker",
                op_id=_OP_ID,
                connector_id=_CONNECTOR_ID,
                target_id=None,
                reason="fixture: try to grant harbor.robot.create",
                expires_at=None,
            ),
        )
    refusal = await _entry_refusal(
        _TENANT_ID, RunApprovalEntry(connector_id=_CONNECTOR_ID, op_id=_OP_ID)
    )
    assert refusal is not None


# ---------------------------------------------------------------------------
# Vault refuses the write after the approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("delete_status", "deleted"), [(200, True), (500, False)])
async def test_refused_vault_write_says_whether_the_robot_was_deleted(
    harbor: HarborConnector,
    monkeypatch: pytest.MonkeyPatch,
    delete_status: int,
    deleted: bool,
) -> None:
    _install_vault(
        monkeypatch, patch_exc=hvac.exceptions.Forbidden(f"permission denied {_NEW_SECRET}")
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_ROBOTS_URL).respond(
            201, json={"id": 7, "name": "robot$proj+bot", "secret": _NEW_SECRET}
        )
        mock.get(_ROBOT_URL).respond(
            200,
            json={
                "id": 7,
                "name": "robot$proj+bot",
                "level": "project",
                "permissions": [{"kind": "project", "namespace": "proj", "access": []}],
            },
        )
        delete = mock.delete(_ROBOT_URL).respond(delete_status)
        request_id = await _park(_person("op-requester"))
        result = await _approve_and_run(request_id, _person("op-approver"))

    assert delete.called
    assert result.status == "error"
    assert result.extras["error_code"] == "robot_secret_not_stored"
    assert result.extras["robot_id"] == 7
    assert result.extras["robot_deleted"] is deleted
    assert result.result == {"robot_id": 7, "robot_deleted": deleted}
    assert (result.error or "").startswith("robot_secret_not_stored:")
    assert ("was deleted" in (result.error or "")) is deleted

    rows = await _error_audit_rows()
    assert len(rows) == 1
    assert rows[0].payload["error"]["error_code"] == "robot_secret_not_stored"
    assert rows[0].payload["error"]["robot_id"] == 7
    assert rows[0].payload["error"]["robot_deleted"] is deleted

    assert _NEW_SECRET not in result.model_dump_json()
    assert _NEW_SECRET not in await _everything_stored()

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The sender withdraws its own pending approval request (#4987).

Covers the service (:func:`withdraw_request`), the REST route
``POST /api/v1/approvals/{id}/withdraw``, what approve / reject / decide do
on a withdrawn request, and the agent waiter. The rules:

* Only the sender may withdraw (``operator.sub == principal_sub``); anyone
  else, an approver included, gets 403. A person, an agent and a service
  principal can each withdraw their own request.
* Only a ``pending`` request; a closed one gets 409.
* One ``approval.withdraw`` audit row, which is not a decision row.
* Approve / reject / decide on a withdrawn request answer 409, and the
  operation never runs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from meho_backplane.agent.approval_wait import resume_or_surface_awaiting_approval
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequestStatus, AuditEventClass, AuditLog
from meho_backplane.main import app
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalNotFoundError,
    ApprovalRequestAlreadyDecidedError,
    WithdrawForbiddenError,
    create_pending_request,
    get_request,
    reject_request,
    withdraw_request,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000004987")
_OTHER_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000004988")
_SENDER_SUB = "addon:sender"
_APPROVER_SUB = "op:approver"
_OP_ID = "vsphere.vm.power_off"
_CONNECTOR_ID = "vmware-rest-9.0"
_PARAMS = {"vm": "vm-42"}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _operator(
    *,
    sub: str = _SENDER_SUB,
    role: TenantRole = TenantRole.OPERATOR,
    tenant_id: uuid.UUID = _TENANT_ID,
    kind: PrincipalKind = PrincipalKind.SERVICE,
) -> Operator:
    return Operator(
        sub=sub,
        name="Test Principal",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
        principal_kind=kind,
    )


async def _park(
    *, sub: str = _SENDER_SUB, tenant_id: uuid.UUID = _TENANT_ID, status: str | None = None
) -> uuid.UUID:
    """Park one request as *sub*; optionally force its status. Return its id."""
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_operator(sub=sub, tenant_id=tenant_id),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
        )
        if status is not None:
            request.status = status
        await s.commit()
        return request.id


async def _audit_rows(request_id: uuid.UUID, path: str) -> list[AuditLog]:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.path == path))).scalars().all()
    return [r for r in rows if r.payload.get("approval_request_id") == str(request_id)]


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    key = make_rsa_keypair("kid-withdraw")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


def _headers(
    key: Any, *, sub: str, role: TenantRole = TenantRole.OPERATOR, kind: str | None = None
) -> dict[str, str]:
    # A fresh sign-in, so a decision is refused for the withdrawn request,
    # not for a missing sign-in time (#3979).
    token = mint_token(
        key,
        sub=sub,
        tenant_role=role.value,
        tenant_id=str(_TENANT_ID),
        principal_kind=kind,
        fresh_sign_in=True,
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sender_withdraws_and_one_non_decision_audit_row_is_written() -> None:
    request_id = await _park()

    async with get_sessionmaker()() as s:
        row = await withdraw_request(s, request_id, operator=_operator(), reason="plan changed")
        await s.commit()

    assert row.status == ApprovalRequestStatus.WITHDRAWN.value
    async with get_sessionmaker()() as s:
        stored = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
    assert stored.status == "withdrawn"
    assert stored.withdrawn_at is not None
    assert stored.withdraw_reason == "plan changed"
    assert stored.reviewed_by is None
    assert stored.decided_at is None

    rows = await _audit_rows(request_id, "approval.withdraw")
    assert len(rows) == 1
    audit = rows[0]
    assert audit.operator_sub == _SENDER_SUB
    assert audit.event_class == AuditEventClass.OPERATION_EXECUTION
    assert audit.payload["withdrawn_by"] == _SENDER_SUB
    assert audit.payload["reason"] == "plan changed"
    assert "reviewed_by" not in audit.payload
    assert "decision" not in audit.payload
    assert await _audit_rows(request_id, "approval.decision") == []


@pytest.mark.asyncio
async def test_non_sender_cannot_withdraw_even_an_approver() -> None:
    request_id = await _park()
    async with get_sessionmaker()() as s:
        with pytest.raises(WithdrawForbiddenError):
            await withdraw_request(
                s,
                request_id,
                operator=_operator(
                    sub=_APPROVER_SUB, role=TenantRole.TENANT_ADMIN, kind=PrincipalKind.USER
                ),
            )
    async with get_sessionmaker()() as s:
        stored = await get_request(s, tenant_id=_TENANT_ID, request_id=request_id)
    assert stored.status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["approved", "rejected", "expired", "withdrawn"])
async def test_closed_request_cannot_be_withdrawn(status: str) -> None:
    request_id = await _park(status=status)
    async with get_sessionmaker()() as s:
        with pytest.raises(ApprovalRequestAlreadyDecidedError):
            await withdraw_request(s, request_id, operator=_operator())


@pytest.mark.asyncio
async def test_cross_tenant_withdraw_is_not_found() -> None:
    request_id = await _park(tenant_id=_OTHER_TENANT_ID)
    async with get_sessionmaker()() as s:
        with pytest.raises(ApprovalNotFoundError):
            await withdraw_request(s, request_id, operator=_operator())


@pytest.mark.asyncio
async def test_reject_after_withdraw_is_refused() -> None:
    request_id = await _park(status="withdrawn")
    async with get_sessionmaker()() as s:
        with pytest.raises(ApprovalRequestAlreadyDecidedError):
            await reject_request(
                s, request_id, operator=_operator(sub=_APPROVER_SUB, kind=PrincipalKind.USER)
            )


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "role"),
    [
        ("user", TenantRole.OPERATOR),
        ("service", TenantRole.READ_ONLY),
        ("agent", TenantRole.READ_ONLY),
    ],
)
async def test_route_each_principal_kind_withdraws_its_own_request(
    client: tuple[httpx.AsyncClient, Any], kind: str, role: TenantRole
) -> None:
    ac, key = client
    request_id = await _park()

    response = await ac.post(
        f"/api/v1/approvals/{request_id}/withdraw",
        json={"reason": "no longer needed"},
        headers=_headers(key, sub=_SENDER_SUB, role=role, kind=kind),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "withdrawn"
    assert body["withdraw_reason"] == "no longer needed"
    assert body["withdrawn_at"] is not None
    assert body["reviewed_by"] is None
    assert body["decided_at"] is None


@pytest.mark.asyncio
async def test_route_non_sender_gets_403(client: tuple[httpx.AsyncClient, Any]) -> None:
    ac, key = client
    request_id = await _park()

    response = await ac.post(
        f"/api/v1/approvals/{request_id}/withdraw",
        json={},
        headers=_headers(key, sub=_APPROVER_SUB, role=TenantRole.TENANT_ADMIN),
    )

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "not_request_sender"}


@pytest.mark.asyncio
async def test_route_closed_request_gets_409(client: tuple[httpx.AsyncClient, Any]) -> None:
    ac, key = client
    request_id = await _park(status="rejected")

    response = await ac.post(
        f"/api/v1/approvals/{request_id}/withdraw",
        json={},
        headers=_headers(key, sub=_SENDER_SUB),
    )

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "approval_request_already_rejected"}


@pytest.mark.asyncio
async def test_route_unknown_request_gets_404(client: tuple[httpx.AsyncClient, Any]) -> None:
    ac, key = client
    response = await ac.post(
        f"/api/v1/approvals/{uuid.uuid4()}/withdraw",
        json={},
        headers=_headers(key, sub=_SENDER_SUB),
    )
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("approve", {"params": _PARAMS}),
        ("reject", {}),
        ("decide", {"decision": "approved"}),
        ("decide", {"decision": "rejected"}),
    ],
)
async def test_route_decisions_on_withdrawn_request_get_409_and_nothing_runs(
    client: tuple[httpx.AsyncClient, Any], path: str, body: dict[str, Any]
) -> None:
    ac, key = client
    request_id = await _park(status="withdrawn")

    resume = AsyncMock()
    with patch("meho_backplane.api.v1.approvals.resume_dispatch_after_approval", resume):
        response = await ac.post(
            f"/api/v1/approvals/{request_id}/{path}",
            json=body,
            headers=_headers(key, sub=_APPROVER_SUB, kind="user"),
        )

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "approval_request_already_withdrawn"}
    resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_list_filters_withdrawn(client: tuple[httpx.AsyncClient, Any]) -> None:
    ac, key = client
    withdrawn_id = await _park(status="withdrawn")
    await _park()

    response = await ac.get(
        "/api/v1/approvals?status=withdrawn", headers=_headers(key, sub=_APPROVER_SUB)
    )

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [str(withdrawn_id)]


# ---------------------------------------------------------------------------
# Agent waiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_waiter_stops_on_withdrawn_and_does_not_run() -> None:
    request_id = await _park(status="withdrawn")
    envelope = {
        "status": "awaiting_approval",
        "op_id": _OP_ID,
        "extras": {"approval_request_id": str(request_id)},
    }

    with patch(
        "meho_backplane.agent.approval_wait._resume_approved_or_already_resumed",
        AsyncMock(),
    ) as resume:
        result = await resume_or_surface_awaiting_approval(
            operator=_operator(),
            call_arguments={"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "params": _PARAMS},
            awaiting_envelope=envelope,
            timeout_seconds=5.0,
        )

    resume.assert_not_awaited()
    assert result["extras"]["error_code"] == "approval_withdrawn"
    assert result["extras"]["decision"] == "withdrawn"

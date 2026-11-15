# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who may approve the Keycloak writes that change tenant rights.

MEHO reads tenant rights from the login token. Five Keycloak writes change
what goes into that token, or who can sign in as whom: user create,
password reset, client create, client update and protocol-mapper create.

These tests take each op's risk level from the connector's registration
table, park a request at that level (the way the dispatcher stamps it), and
check the approval rules:

* a reviewer (an operator, or a read-only person with the approver flag)
  cannot approve another person's request;
* a MEHO-SA-Operator or a tenant admin can;
* the single-operator self-approval switch does not let the requester
  approve their own request.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak.ops_write import WRITE_OPS
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalGroupForbiddenError,
    SelfApprovalForbiddenError,
    approve_request,
    create_pending_request,
)
from meho_backplane.settings import get_settings
from tests.test_api_v1_approvals_fresh_sign_in import (
    _TENANT_A,
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)

_IDENTITY_WRITES = (
    "keycloak.user.create",
    "keycloak.user.reset_password",
    "keycloak.client.create",
    "keycloak.client.update",
    "keycloak.protocol_mapper.create",
)
_LEVELS = {op.op_id: op.safety_level for op in WRITE_OPS}
_PARAMS: dict[str, Any] = {"representation": {"username": "new-user"}}
_REQUESTER = "person-requester"
_OTHER = "person-other"


def _person(
    sub: str,
    *,
    role: TenantRole = TenantRole.OPERATOR,
    sa_operator: bool = False,
    approver: bool = False,
) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=role,
        approver=approver,
        self_approver=sa_operator,
    )


async def _park(op_id: str) -> uuid.UUID:
    """Park *op_id* by the requester, with the level its registration declares."""
    effect = {"op_id": op_id, "safety_level": _LEVELS[op_id]}
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_person(_REQUESTER),
            connector_id="keycloak-admin-26.x",
            op_id=op_id,
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            proposed_effect=effect,
        )
        await session.commit()
        return request.id


async def _approve(request_id: uuid.UUID, operator: Operator) -> None:
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=operator, params=_PARAMS)
        await session.commit()


async def _status(request_id: uuid.UUID) -> str:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row.status


@pytest.mark.parametrize("op_id", _IDENTITY_WRITES)
def test_identity_writes_register_as_dangerous(op_id: str) -> None:
    assert _LEVELS[op_id] == "dangerous"


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _IDENTITY_WRITES)
@pytest.mark.parametrize(
    "reviewer",
    [_person(_OTHER), _person(_OTHER, role=TenantRole.READ_ONLY, approver=True)],
    ids=["operator", "read-only-approver"],
)
async def test_a_reviewer_cannot_approve_an_identity_write(op_id: str, reviewer: Operator) -> None:
    request_id = await _park(op_id)

    with pytest.raises(ApprovalGroupForbiddenError):
        await _approve(request_id, reviewer)

    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _IDENTITY_WRITES)
@pytest.mark.parametrize(
    "approver",
    [_person(_OTHER, sa_operator=True), _person(_OTHER, role=TenantRole.TENANT_ADMIN)],
    ids=["sa-operator", "tenant-admin"],
)
async def test_an_sa_operator_or_tenant_admin_approves_an_identity_write(
    op_id: str, approver: Operator
) -> None:
    request_id = await _park(op_id)

    await _approve(request_id, approver)

    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _IDENTITY_WRITES)
async def test_the_self_approval_switch_does_not_cover_an_identity_write(
    op_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true")
    get_settings.cache_clear()
    request_id = await _park(op_id)

    with pytest.raises(SelfApprovalForbiddenError) as exc:
        await _approve(request_id, _person(_REQUESTER))

    assert exc.value.high_risk is True
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who may approve a change that gives a login more rights.

* The login a group or role change is for is never the approver.
* In MEHO's own login realm the change needs a second person who is a
  tenant admin (a platform admin for the platform admin group or role).
* A normal change for another login keeps the usual rules.
* The decision audit row names the login the change is for.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select

import meho_backplane.connectors.keycloak  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalRoleFloorError,
    ApprovalRule,
    approve_request,
    create_pending_request,
)
from meho_backplane.operations.approval_rights import approve_right
from tests._oidc_jwt_helpers import APPROVAL_CLIENT_ID  # noqa: F401
from tests.test_api_v1_approvals_fresh_sign_in import (
    _TENANT_A,
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)

_ADD = "keycloak.group.member.add"
_USER = "11111111-1111-1111-1111-111111111111"
_OTHER_USER = "22222222-2222-2222-2222-222222222222"
_GROUP = "33333333-3333-3333-3333-333333333333"
_REQUESTER = "person-requester"
_REALM = "meho"
_LOGIN_REALMS = "meho_backplane.connectors.keycloak.ops_write_grant._backplane_login_realms"


def _person(
    sub: str,
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    sa_operator: bool = False,
    platform_admin: bool = False,
) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=role,
        self_approver=sa_operator,
        platform_admin=platform_admin,
    )


def _grant(
    *, login_realm: bool = True, platform_admin: bool | None = False, user_id: str | None = None
) -> dict[str, Any]:
    return {
        "op_class": "write",
        "safety_level": "dangerous",
        "preview": {
            "grant": {
                "user_id": user_id,
                "login_realm": login_realm,
                "platform_admin": platform_admin,
            }
        },
    }


async def _park(
    params: dict[str, Any],
    effect: dict[str, Any] | None,
    *,
    op_id: str = _ADD,
    requester: str = _REQUESTER,
    requester_sa: bool = False,
) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_person(requester, role=TenantRole.OPERATOR, sa_operator=requester_sa),
            connector_id="keycloak-1.x",
            op_id=op_id,
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            proposed_effect=effect,
        )
        await session.commit()
        return request.id


async def _approve(request_id: uuid.UUID, operator: Operator, params: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=operator, params=params)
        await session.commit()


async def _status(request_id: uuid.UUID) -> str:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row.status


async def _decision_payload(request_id: uuid.UUID) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(AuditLog.payload).where(AuditLog.path == "approval.decision")
        )
        for (payload,) in rows:
            if payload.get("approval_request_id") == str(request_id):
                return dict(payload)
    raise AssertionError("no approval.decision audit row")


@pytest.mark.asyncio
@pytest.mark.parametrize("login_realm", [True, False])
async def test_a_login_cannot_approve_its_own_group_add(login_realm: bool) -> None:
    """The second login of one person approves the add of itself: refused."""
    params = {"group_id": _GROUP, "user_id": _USER}
    request_id = await _park(params, _grant(login_realm=login_realm))

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _person(_USER), params)

    assert "your own login" in exc.value.reason_text
    assert "second login of the same person does not count" in exc.value.reason_text
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_a_user_name_is_resolved_before_the_approval_compares_it() -> None:
    """A request that names the user by user name still names the id the approver has."""
    params = {"group_id": _GROUP, "username": "second-login"}
    request_id = await _park(params, _grant(user_id=_USER))

    with pytest.raises(ApprovalRoleFloorError):
        await _approve(request_id, _person(_USER), params)

    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_an_sa_operator_cannot_approve_their_own_change_in_the_login_realm() -> None:
    params = {"group_id": _GROUP, "user_id": _OTHER_USER}
    request_id = await _park(params, _grant(), requester_sa=True)

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(
            request_id, _person(_REQUESTER, role=TenantRole.OPERATOR, sa_operator=True), params
        )

    assert "A second person must approve it" in exc.value.reason_text
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_an_sa_operator_who_is_not_a_tenant_admin_cannot_approve_in_the_login_realm() -> None:
    params = {"group_id": _GROUP, "user_id": _OTHER_USER}
    request_id = await _park(params, _grant())

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(
            request_id, _person("person-sa", role=TenantRole.OPERATOR, sa_operator=True), params
        )

    assert "tenant admin" in exc.value.reason_text
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_a_second_tenant_admin_approves_and_the_audit_row_names_the_user() -> None:
    params = {"group_id": _GROUP, "user_id": _OTHER_USER}
    request_id = await _park(params, _grant())

    await _approve(request_id, _person("person-admin"), params)

    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    payload = await _decision_payload(request_id)
    assert payload["approval_rule"] == ApprovalRule.SA_OPERATOR_OR_ADMIN.value
    assert payload["grant_user_id"] == _OTHER_USER
    assert payload["grant_for_approver"] is False
    assert payload["grant_in_login_realm"] is True


@pytest.mark.asyncio
async def test_the_platform_admin_group_needs_a_platform_admin() -> None:
    params = {"group_name": "MEHO-Platform-Admins", "user_id": _OTHER_USER}
    request_id = await _park(params, _grant(platform_admin=True))

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _person("person-admin"), params)
    assert "platform admin" in exc.value.reason_text
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value

    await _approve(request_id, _person("person-platform", platform_admin=True), params)
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
async def test_a_request_without_facts_gets_the_strict_rule() -> None:
    params = {"group_id": _GROUP, "user_id": _OTHER_USER}
    request_id = await _park(params, None)

    with pytest.raises(ApprovalRoleFloorError):
        await _approve(request_id, _person("person-admin"), params)


@pytest.mark.asyncio
async def test_a_change_for_another_login_outside_the_login_realm_keeps_the_usual_rules() -> None:
    params = {"group_id": _GROUP, "user_id": _OTHER_USER}
    request_id = await _park(params, _grant(login_realm=False), requester_sa=True)

    await _approve(
        request_id, _person(_REQUESTER, role=TenantRole.OPERATOR, sa_operator=True), params
    )

    payload = await _decision_payload(request_id)
    assert payload["approval_rule"] == ApprovalRule.SA_OPERATOR_SELF_APPROVAL.value
    assert payload["grant_for_approver"] is False


@pytest.mark.asyncio
async def test_other_operations_are_not_affected() -> None:
    params = {"name": "demo", "user_id": _USER}
    request_id = await _park(params, {"safety_level": "dangerous"}, op_id="demo.change")

    await _approve(request_id, _person(_USER), params)  # not the requester, not a grant op

    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    assert "grant_user_id" not in await _decision_payload(request_id)


@pytest.mark.asyncio
async def test_the_approve_right_read_shows_the_same_refusal() -> None:
    params = {"group_id": _GROUP, "user_id": _USER}
    request_id = await _park(params, _grant())
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
        assert request is not None
        right = approve_right(_person(_USER), request)

    assert right.may_approve is False
    assert "your own login" in right.message


# ---------------------------------------------------------------------------
# The facts the Keycloak connector puts on the waiting request
# ---------------------------------------------------------------------------


@dataclass
class _FakeDescriptor:
    op_id: str


@dataclass
class _FakeTarget:
    extras: dict[str, Any]


def _ctx(
    op_id: str, params: dict[str, Any], *, realm: str, connector: Any = None
) -> PreviewContext:
    return PreviewContext(
        descriptor=_FakeDescriptor(op_id=op_id),  # type: ignore[arg-type]
        connector_instance=connector,
        operator=_person("requester", role=TenantRole.OPERATOR),
        target=_FakeTarget(extras={"managed_realm": realm}),
        params=params,
        connector_id="keycloak-1.x",
    )


async def _grant_block(ctx: PreviewContext) -> dict[str, Any]:
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    return effect["preview"]["grant"]


@pytest.mark.asyncio
async def test_the_connector_resolves_a_user_name_to_its_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})
    connector = KeycloakConnector()

    async def _find(_t: Any, _realm: str, username: str, *, operator: Any) -> str | None:
        return _USER if username == "second-login" else None

    connector._find_user_uuid = _find  # type: ignore[method-assign]
    ctx = _ctx(
        _ADD,
        {"group_name": "role-x", "username": "second-login"},
        realm=_REALM,
        connector=connector,
    )

    block = await _grant_block(ctx)

    assert block == {"user_id": _USER, "login_realm": True, "platform_admin": False}


@pytest.mark.asyncio
async def test_the_connector_marks_the_platform_admin_group_and_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})

    group = await _grant_block(
        _ctx(_ADD, {"group_name": "meho-platform-admins", "user_id": _USER}, realm=_REALM)
    )
    role = await _grant_block(
        _ctx(
            "keycloak.role_mapping.assign", {"id": _USER, "roles": ["platform_admin"]}, realm=_REALM
        )
    )
    plain = await _grant_block(
        _ctx("keycloak.role_mapping.assign", {"id": _USER, "roles": ["viewer"]}, realm=_REALM)
    )

    assert group["platform_admin"] is True
    assert role["platform_admin"] is True
    assert plain["platform_admin"] is False


@pytest.mark.asyncio
async def test_the_connector_says_when_the_realm_is_not_the_login_realm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})

    block = await _grant_block(
        _ctx(_ADD, {"group_name": "g", "user_id": _USER}, realm="customer-realm")
    )

    assert block["login_realm"] is False


@pytest.mark.asyncio
async def test_a_group_that_cannot_be_read_stays_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})

    block = await _grant_block(_ctx(_ADD, {"group_id": _GROUP, "user_id": _USER}, realm=_REALM))

    assert block["platform_admin"] is None

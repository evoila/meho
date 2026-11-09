# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who may approve a change that gives a login more rights.

* The login a group or role change is for is never the approver.
* A change whose login or group MEHO could not establish cannot be approved.
* A group attribute change is never approved by a person in the group.
* In MEHO's own login realm the change needs a second person who is a
  tenant admin (a platform admin for the platform admin group or role).
* A normal change for another login keeps the usual rules.
* The decision audit row names the login the change is for.
* Right before the approved change runs, the login and the group must
  still have the ids the approver saw.
* Right before the approved change runs, the approver must still be
  outside the group whose attributes change, and the change must not need
  a stricter approval than the one it got.
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
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import FoundObject
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalRoleFloorError,
    ApprovalRule,
    approve_request,
    create_pending_request,
    refuse_resume_if_preview_changed,
)
from meho_backplane.operations.approval_rerun import _object_changed
from meho_backplane.operations.approval_rights import approve_right
from meho_backplane.operations.grant_approval import (
    GRANT_REASON_APPROVER_IN_GROUP,
    GRANT_REASON_CHANGED,
    GRANT_REASON_STRICTER,
    GRANT_REASON_UNREADABLE,
    grant_changed_since_request,
)
from tests._oidc_jwt_helpers import APPROVAL_CLIENT_ID  # noqa: F401
from tests.test_api_v1_approvals_fresh_sign_in import (
    _TENANT_A,
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)

_ADD = "keycloak.group.member.add"
_ATTRS = "keycloak.group.update_attributes"
_ROLES = "keycloak.role_mapping.assign"
_USER = "11111111-1111-1111-1111-111111111111"
_OTHER_USER = "22222222-2222-2222-2222-222222222222"
_GROUP = "33333333-3333-3333-3333-333333333333"
_PLATFORM_GROUP = "44444444-4444-4444-4444-444444444444"
_REQUESTER = "person-requester"
_REALM = "meho"
_PLATFORM_NAME = "MEHO-Platform-Admins"
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
    *,
    login_realm: bool = True,
    platform_admin: bool | None = False,
    user_id: str | None = None,
    group_id: str | None = _GROUP,
    **more: Any,
) -> dict[str, Any]:
    return {
        "op_class": "write",
        "safety_level": "dangerous",
        "preview": {
            "grant": {
                "user_id": user_id,
                "group_id": group_id,
                "login_realm": login_realm,
                "platform_admin": platform_admin,
                **more,
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


async def _row(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row


async def _status(request_id: uuid.UUID) -> str:
    return (await _row(request_id)).status


async def _decision_payload(request_id: uuid.UUID) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(AuditLog.payload).where(AuditLog.path == "approval.decision")
        )
        for (payload,) in rows:
            if payload.get("approval_request_id") == str(request_id):
                return dict(payload)
    raise AssertionError("no approval.decision audit row")


# ---------------------------------------------------------------------------
# Who may approve
# ---------------------------------------------------------------------------


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
@pytest.mark.parametrize("op_id", [_ADD, _ROLES])
async def test_a_change_for_an_unknown_login_cannot_be_approved(op_id: str) -> None:
    """The user name could not be resolved when the request was parked.

    Nobody can approve it, also not a second tenant admin: MEHO cannot tell
    whether the change is for the approver. The handler would resolve the
    name again when the change runs, so an approval here would trust a
    lookup that nobody checked.
    """
    params = {"group_id": _GROUP, "username": "unknown-login", "roles": ["viewer"]}
    request_id = await _park(params, _grant(user_id=None), op_id=op_id)

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _person("person-admin"), params)

    assert "could not establish which login" in exc.value.reason_text
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_a_change_for_an_unknown_group_cannot_be_approved() -> None:
    params = {"group_name": "role-x", "user_id": _OTHER_USER}
    request_id = await _park(params, _grant(group_id=None, platform_admin=False))

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _person("person-platform", platform_admin=True), params)

    assert "could not establish which group" in exc.value.reason_text
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_a_person_in_the_group_cannot_approve_a_change_of_its_attributes() -> None:
    """The attributes of a group give its members their rights."""
    params = {"id": _GROUP, "attributes": {"tenant_role": ["admin"]}}
    request_id = await _park(params, _grant(members=[_OTHER_USER, _USER]), op_id=_ATTRS)

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _person(_USER), params)

    assert "you are in this group" in exc.value.reason_text
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_a_group_change_with_unknown_members_cannot_be_approved() -> None:
    params = {"id": _GROUP, "attributes": {"tenant_role": ["admin"]}}
    request_id = await _park(params, _grant(members=None), op_id=_ATTRS)

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _person("person-admin"), params)

    assert "could not read who is in this group" in exc.value.reason_text
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_a_tenant_admin_outside_the_group_approves_its_attribute_change() -> None:
    params = {"id": _GROUP, "attributes": {"tenant_role": ["admin"]}}
    request_id = await _park(params, _grant(members=[_OTHER_USER]), op_id=_ATTRS)

    await _approve(request_id, _person("person-admin"), params)

    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    payload = await _decision_payload(request_id)
    assert payload["grant_group_id"] == _GROUP
    assert payload["grant_user_id"] is None


@pytest.mark.asyncio
async def test_a_group_change_outside_the_login_realm_needs_no_member_list() -> None:
    """The approver's MEHO login is not in a group of another realm."""
    params = {"id": _GROUP, "attributes": {"tenant_role": ["admin"]}}
    request_id = await _park(params, _grant(login_realm=False, members=None), op_id=_ATTRS)

    await _approve(request_id, _person("person-admin"), params)

    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value


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
    assert payload["grant_group_id"] == _GROUP
    assert payload["grant_for_approver"] is False
    assert payload["grant_in_login_realm"] is True


@pytest.mark.asyncio
async def test_the_platform_admin_group_needs_a_platform_admin() -> None:
    params = {"group_name": _PLATFORM_NAME, "user_id": _OTHER_USER}
    request_id = await _park(params, _grant(platform_admin=True, group_id=_PLATFORM_GROUP))

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
    request = await _row(request_id)

    right = approve_right(_person(_USER), request)

    assert right.may_approve is False
    assert "your own login" in right.message


# ---------------------------------------------------------------------------
# Right before the approved change runs: the same login, the same group
# ---------------------------------------------------------------------------


def _live_grant(
    monkeypatch: pytest.MonkeyPatch, grant: dict[str, Any] | None, *, builds: bool = True
) -> list[dict[str, Any]]:
    """The card MEHO builds right before the run carries *grant*."""
    calls: list[dict[str, Any]] = []

    async def _fake_live_card(operator: Operator, **kwargs: Any) -> dict[str, Any] | None:
        calls.append(kwargs)
        if not builds:
            return None
        return {"op_class": "write", "preview": {"grant": grant} if grant else {}}

    import meho_backplane.operations.approval_object_check as object_check_module

    monkeypatch.setattr(object_check_module, "live_card", _fake_live_card)
    return calls


@pytest.mark.asyncio
async def test_the_run_is_refused_when_the_user_name_now_names_another_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user name moved to another account while the request waited."""
    params = {"group_id": _GROUP, "username": "second-login"}
    request = await _row(await _park(params, _grant(user_id=_OTHER_USER)))
    _live_grant(monkeypatch, {"user_id": _USER, "group_id": _GROUP})

    reason = await grant_changed_since_request(_person("person-admin"), request, None, params)

    assert reason == GRANT_REASON_CHANGED


@pytest.mark.asyncio
async def test_the_run_is_refused_when_the_group_name_now_names_another_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params = {"group_name": "role-x", "user_id": _OTHER_USER}
    request = await _row(await _park(params, _grant(user_id=_OTHER_USER)))
    _live_grant(monkeypatch, {"user_id": _OTHER_USER, "group_id": _PLATFORM_GROUP})

    reason = await grant_changed_since_request(_person("person-admin"), request, None, params)

    assert reason == GRANT_REASON_CHANGED


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [None, {"user_id": None, "group_id": _GROUP}])
async def test_the_run_is_refused_when_the_login_cannot_be_read_again(
    monkeypatch: pytest.MonkeyPatch, live: dict[str, Any] | None
) -> None:
    params = {"group_id": _GROUP, "username": "second-login"}
    request = await _row(await _park(params, _grant(user_id=_OTHER_USER)))
    _live_grant(monkeypatch, live, builds=live is not None)

    reason = await grant_changed_since_request(_person("person-admin"), request, None, params)

    assert reason == GRANT_REASON_UNREADABLE


@pytest.mark.asyncio
async def test_the_run_goes_ahead_when_the_ids_are_the_approved_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params = {"group_name": "role-x", "username": "second-login"}
    request = await _row(await _park(params, _grant(user_id=_OTHER_USER)))
    _live_grant(
        monkeypatch,
        {
            "user_id": _OTHER_USER.upper(),
            "group_id": _GROUP,
            "login_realm": True,
            "platform_admin": False,
        },
    )

    reason = await grant_changed_since_request(_person("person-admin"), request, None, params)

    assert reason is None


@pytest.mark.asyncio
async def test_a_call_that_is_not_a_change_of_rights_is_not_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params = {"name": "demo"}
    request = await _row(await _park(params, {"safety_level": "dangerous"}, op_id="demo.change"))
    calls = _live_grant(monkeypatch, None, builds=False)

    reason = await grant_changed_since_request(_person("person-admin"), request, None, params)

    assert reason is None
    assert calls == []


@pytest.mark.asyncio
async def test_the_resume_check_refuses_a_moved_login_without_a_preview_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared check of both resume paths refuses, and stores the refusal."""
    params = {"group_id": _GROUP, "username": "second-login"}
    request = await _row(await _park(params, _grant(user_id=_OTHER_USER)))
    assert request.preview_hash is None
    _live_grant(monkeypatch, {"user_id": _USER, "group_id": _GROUP})

    refusal = await refuse_resume_if_preview_changed(_person("person-admin"), request, None, params)

    assert refusal is not None
    assert refusal.status == "denied"
    assert refusal.extras is not None
    assert refusal.extras["error_code"] == "object_changed_since_approval"
    assert refusal.extras["reason"] == GRANT_REASON_CHANGED
    stored = await _row(request.id)
    assert stored.resume_result is not None
    assert stored.resume_result["extras"]["error_code"] == "object_changed_since_approval"


_ATTR_PARAMS: dict[str, Any] = {"id": _GROUP, "attributes": {"tenant_role": ["admin"]}}
_APPROVER = "55555555-5555-5555-5555-555555555555"


def _live_group(members: list[str] | None, **more: Any) -> dict[str, Any]:
    return {
        "group_id": _GROUP,
        "login_realm": True,
        "platform_admin": False,
        "members": members,
        **more,
    }


async def _approved_group_change() -> ApprovalRequest:
    """A group attribute change that the approver passed while outside the group."""
    request_id = await _park(_ATTR_PARAMS, _grant(members=[_OTHER_USER]), op_id=_ATTRS)
    await _approve(request_id, _person(_APPROVER), _ATTR_PARAMS)
    return await _row(request_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("approver", [_APPROVER, _APPROVER.upper()])
async def test_the_run_is_refused_when_the_approver_joined_the_group(
    monkeypatch: pytest.MonkeyPatch, approver: str
) -> None:
    """The saved member list does not have the approver. The live one does."""
    request = await _approved_group_change()
    _live_grant(monkeypatch, _live_group([_OTHER_USER, approver]))

    # The run uses the identity of the requester, not the approver.
    reason = await grant_changed_since_request(_person(_REQUESTER), request, None, _ATTR_PARAMS)

    assert reason == GRANT_REASON_APPROVER_IN_GROUP


@pytest.mark.asyncio
async def test_the_resume_check_refuses_when_the_approver_joined_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = await _approved_group_change()
    _live_grant(monkeypatch, _live_group([_APPROVER]))

    refusal = await refuse_resume_if_preview_changed(
        _person(_REQUESTER), request, None, _ATTR_PARAMS
    )

    assert refusal is not None
    assert refusal.extras is not None
    assert refusal.extras["error_code"] == "object_changed_since_approval"
    assert refusal.extras["reason"] == GRANT_REASON_APPROVER_IN_GROUP


@pytest.mark.asyncio
async def test_a_retry_is_refused_when_the_approver_joined_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = await _approved_group_change()
    _live_grant(monkeypatch, _live_group([_APPROVER]))

    assert await _object_changed(_person(_REQUESTER), request, None, _ATTR_PARAMS) is True


@pytest.mark.asyncio
async def test_a_retry_goes_ahead_when_the_approver_is_outside_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = await _approved_group_change()
    _live_grant(monkeypatch, _live_group([_OTHER_USER]))

    assert await _object_changed(_person(_REQUESTER), request, None, _ATTR_PARAMS) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [_live_group(None), {**_live_group(None), "members": "odd"}])
async def test_the_run_is_refused_when_members_cannot_be_read_again(
    monkeypatch: pytest.MonkeyPatch, live: dict[str, Any]
) -> None:
    request = await _approved_group_change()
    _live_grant(monkeypatch, live)

    reason = await grant_changed_since_request(_person(_REQUESTER), request, None, _ATTR_PARAMS)

    assert reason == GRANT_REASON_UNREADABLE


@pytest.mark.asyncio
async def test_the_run_is_refused_when_the_approver_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group change with no approver on the row is refused, never allowed."""
    request_id = await _park(_ATTR_PARAMS, _grant(members=[_OTHER_USER]), op_id=_ATTRS)
    request = await _row(request_id)
    assert request.reviewed_by is None
    _live_grant(monkeypatch, _live_group([_OTHER_USER]))

    reason = await grant_changed_since_request(_person(_REQUESTER), request, None, _ATTR_PARAMS)

    assert reason == GRANT_REASON_UNREADABLE


@pytest.mark.asyncio
async def test_a_group_change_outside_the_login_realm_reads_no_members_at_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = await _park(_ATTR_PARAMS, _grant(login_realm=False, members=None), op_id=_ATTRS)
    await _approve(request_id, _person(_APPROVER), _ATTR_PARAMS)
    _live_grant(monkeypatch, _live_group(None, login_realm=False))

    reason = await grant_changed_since_request(
        _person(_REQUESTER), await _row(request_id), None, _ATTR_PARAMS
    )

    assert reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approved", "live_change"),
    [
        ({"platform_admin": False}, {"platform_admin": True}),
        ({"platform_admin": False}, {"platform_admin": None}),
        ({"login_realm": False, "members": None}, {"login_realm": True}),
        ({"login_realm": False, "members": None}, {"login_realm": None}),
    ],
)
async def test_the_run_is_refused_when_the_change_needs_a_stricter_approval_now(
    monkeypatch: pytest.MonkeyPatch, approved: dict[str, Any], live_change: dict[str, Any]
) -> None:
    grant = _grant(**{"members": [_OTHER_USER], **approved})
    request_id = await _park(_ATTR_PARAMS, grant, op_id=_ATTRS)
    await _approve(request_id, _person(_APPROVER), _ATTR_PARAMS)
    _live_grant(monkeypatch, {**_live_group([_OTHER_USER]), **live_change})

    reason = await grant_changed_since_request(
        _person(_REQUESTER), await _row(request_id), None, _ATTR_PARAMS
    )

    assert reason == GRANT_REASON_STRICTER


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


def _groups_by_id(monkeypatch: pytest.MonkeyPatch, names: dict[str, str]) -> None:
    """Reading a group by id gives its name; an id not in *names* fails."""

    async def _find(_c: Any, _o: Any, _t: Any, params: dict[str, Any]) -> FoundObject:
        group_id = params["id"]
        if group_id not in names:
            raise RuntimeError("read failed")
        return FoundObject(realms=None, object_id=group_id, name=names[group_id], rep={})  # type: ignore[arg-type]

    monkeypatch.setattr("meho_backplane.connectors.keycloak.ops_write_grant.find_group", _find)


def _connector_with(
    *,
    groups_by_name: dict[str, str] | None = None,
    users: dict[str, str] | None = None,
    members: dict[str, list[dict[str, Any]]] | None = None,
    user_lookup_fails: bool = False,
) -> tuple[KeycloakConnector, list[dict[str, Any]]]:
    """A connector whose lookups answer from the given maps; records member reads."""
    connector = KeycloakConnector()
    reads: list[dict[str, Any]] = []

    async def _find_group(
        _t: Any, _realm: str, name: str, parent_id: str | None, *, operator: Any
    ) -> dict[str, Any] | None:
        found = (groups_by_name or {}).get(name)
        return {"id": found, "name": name} if found else None

    async def _find_user(_t: Any, _realm: str, username: str, *, operator: Any) -> str | None:
        if user_lookup_fails:
            raise RuntimeError("read failed")
        return (users or {}).get(username)

    async def _get_admin_list(
        _t: Any, path: str, *, operator: Any, params: dict[str, Any] | None = None
    ) -> list[Any]:
        assert params is not None
        reads.append(params)
        rows = (members or {}).get(path.rsplit("/", 2)[-2])
        if rows is None:
            raise RuntimeError("read failed")
        first = int(params["first"])
        return rows[first : first + int(params["max"])]

    connector._find_group = _find_group  # type: ignore[method-assign]
    connector._find_user_uuid = _find_user  # type: ignore[method-assign]
    connector._get_admin_list = _get_admin_list  # type: ignore[method-assign]
    return connector, reads


@pytest.mark.asyncio
async def test_the_connector_resolves_a_user_name_to_its_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})
    connector, _ = _connector_with(users={"second-login": _USER}, groups_by_name={"role-x": _GROUP})
    ctx = _ctx(
        _ADD,
        {"group_name": "role-x", "username": "second-login"},
        realm=_REALM,
        connector=connector,
    )

    block = await _grant_block(ctx)

    assert block == {
        "user_id": _USER,
        "group_id": _GROUP,
        "login_realm": True,
        "platform_admin": False,
    }


@pytest.mark.asyncio
async def test_a_user_name_that_cannot_be_read_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed read is "unknown", not "another login"; the approval then refuses."""
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})
    connector, _ = _connector_with(user_lookup_fails=True, groups_by_name={"role-x": _GROUP})
    params = {"group_name": "role-x", "username": "second-login"}
    ctx = _ctx(_ADD, params, realm=_REALM, connector=connector)

    block = await _grant_block(ctx)

    assert block["user_id"] is None
    assert block["group_id"] == _GROUP


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "id_key", "name_key"),
    [(_ADD, "group_id", "group_name"), (_ATTRS, "id", "name")],
)
async def test_a_group_id_wins_over_a_group_name_as_in_the_handler(
    monkeypatch: pytest.MonkeyPatch, op_id: str, id_key: str, name_key: str
) -> None:
    """The params name an ordinary group but point at the platform admin group by id."""
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})
    _groups_by_id(monkeypatch, {_PLATFORM_GROUP: _PLATFORM_NAME, _GROUP: "role-x"})
    connector, _ = _connector_with(members={_PLATFORM_GROUP: [], _GROUP: []})

    by_id = await _grant_block(
        _ctx(
            op_id,
            {id_key: _PLATFORM_GROUP, name_key: "role-x", "user_id": _USER},
            realm=_REALM,
            connector=connector,
        )
    )
    reverse = await _grant_block(
        _ctx(
            op_id,
            {id_key: _GROUP, name_key: _PLATFORM_NAME, "user_id": _USER},
            realm=_REALM,
            connector=connector,
        )
    )

    assert (by_id["group_id"], by_id["platform_admin"]) == (_PLATFORM_GROUP, True)
    assert (reverse["group_id"], reverse["platform_admin"]) == (_GROUP, False)


@pytest.mark.asyncio
async def test_a_group_name_alone_is_looked_up_on_its_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})
    connector, _ = _connector_with(
        groups_by_name={_PLATFORM_NAME: _PLATFORM_GROUP}, members={_PLATFORM_GROUP: []}
    )

    block = await _grant_block(
        _ctx(
            _ATTRS,
            {"name": _PLATFORM_NAME, "attributes": {"k": ["v"]}},
            realm=_REALM,
            connector=connector,
        )
    )
    gone = {"name": "gone", "attributes": {"k": ["v"]}}
    unknown = await _grant_block(_ctx(_ATTRS, gone, realm=_REALM, connector=connector))

    assert (block["group_id"], block["platform_admin"]) == (_PLATFORM_GROUP, True)
    assert (unknown["group_id"], unknown["platform_admin"], unknown["members"]) == (
        None,
        False,
        None,
    )


@pytest.mark.asyncio
async def test_the_connector_marks_the_platform_admin_group_and_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})

    group = await _grant_block(
        _ctx(_ADD, {"group_name": "meho-platform-admins", "user_id": _USER}, realm=_REALM)
    )
    role = await _grant_block(
        _ctx(_ROLES, {"id": _USER, "roles": ["platform_admin"]}, realm=_REALM)
    )
    plain = await _grant_block(_ctx(_ROLES, {"id": _USER, "roles": ["viewer"]}, realm=_REALM))

    assert group["platform_admin"] is True
    assert role["platform_admin"] is True
    assert plain["platform_admin"] is False
    assert "members" not in role


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
    assert block["group_id"] == _GROUP


@pytest.mark.asyncio
async def test_a_group_attribute_change_lists_the_members_page_by_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})
    _groups_by_id(monkeypatch, {_GROUP: "role-x"})
    rows = [{"id": f"00000000-0000-0000-0000-{n:012d}"} for n in range(150)]
    connector, reads = _connector_with(members={_GROUP: rows})

    block = await _grant_block(
        _ctx(_ATTRS, {"id": _GROUP, "attributes": {"k": ["v"]}}, realm=_REALM, connector=connector)
    )

    assert block["members"] == [row["id"] for row in rows]
    assert [r["first"] for r in reads] == ["0", "100"]


@pytest.mark.asyncio
async def test_members_that_cannot_be_read_or_exceed_the_cap_are_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})
    _groups_by_id(monkeypatch, {_GROUP: "role-x", _PLATFORM_GROUP: "big"})
    too_many = [{"id": f"00000000-0000-0000-0000-{n:012d}"} for n in range(1001)]
    connector, _ = _connector_with(members={_PLATFORM_GROUP: too_many})

    unreadable = await _grant_block(
        _ctx(_ATTRS, {"id": _GROUP, "attributes": {"k": ["v"]}}, realm=_REALM, connector=connector)
    )
    capped = await _grant_block(
        _ctx(
            _ATTRS,
            {"id": _PLATFORM_GROUP, "attributes": {"k": ["v"]}},
            realm=_REALM,
            connector=connector,
        )
    )

    assert unreadable["members"] is None
    assert capped["members"] is None


@pytest.mark.asyncio
async def test_members_are_not_read_outside_the_login_realm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})
    _groups_by_id(monkeypatch, {_GROUP: "role-x"})
    connector, reads = _connector_with(members={_GROUP: [{"id": _USER}]})

    params = {"id": _GROUP, "attributes": {"k": ["v"]}}
    block = await _grant_block(_ctx(_ATTRS, params, realm="customer", connector=connector))

    assert block["login_realm"] is False
    assert block["members"] is None
    assert reads == []

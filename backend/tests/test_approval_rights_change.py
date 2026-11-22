# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The rights-change mark on an approval request.

A request with the mark changes who has rights. Only an admin (a tenant
admin or a platform admin) approves it, and only with a click in the web
console. The admin may be the person who asked; that is recorded as a
self-approval. The command line and REST are refused with "approve this
change of rights in the web console", also for another admin's request.
No other group approves it, at any risk level and with the break-glass
switch on. The same check serves the approve and the "may I approve?" sign.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from meho_backplane.api.v1.approvals import _view
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalRule,
    RightsChangeApprovalError,
    UnauthorizedApprovalError,
    approve_request,
    create_pending_request,
)
from meho_backplane.operations.approval_rights import ApproveRightReason, approve_right
from meho_backplane.operations.grant_approval import (
    _GRANT_OPS,
    RIGHTS_CHANGE_CONSOLE_ONLY,
    RIGHTS_CHANGE_REFUSAL,
    _GrantOp,
    is_rights_change,
)
from meho_backplane.ui.routes.approvals.render import project_request_to_view
from tests.test_api_v1_approvals_fresh_sign_in import (
    _TENANT_A,
    _client,  # noqa: F401 -- fixture: the REST test client and its signing key
    _fake_resume,  # noqa: F401 -- fixture: records the re-dispatch after approval
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)
from tests.test_approval_group_rules import _PARAMS, _REQUESTER, _break_glass_on, _post, _rest_token

_GRANT_OP = "demo.group.add"
_LEVELS = ["safe", "caution", "dangerous", "destructive"]


def _person(
    sub: str,
    *,
    role: TenantRole = TenantRole.OPERATOR,
    sa_operator: bool = False,
    approver: bool = False,
    platform_admin: bool = False,
    kind: PrincipalKind = PrincipalKind.USER,
    web: bool = True,
) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=role,
        principal_kind=kind,
        approver=approver,
        self_approver=sa_operator,
        platform_admin=platform_admin,
        web_session=web,
    )


#: The groups that are not admins, each as the person who asked, in the web console.
_NOT_ADMINS = {
    "reader": _person(_REQUESTER, role=TenantRole.READ_ONLY),
    "operator": _person(_REQUESTER),
    "sa-operator": _person(_REQUESTER, sa_operator=True),
    "approver": _person(_REQUESTER, role=TenantRole.READ_ONLY, approver=True),
}

#: The admins, each as the person who asked, in the web console.
_ADMINS = {
    "admin": _person(_REQUESTER, role=TenantRole.TENANT_ADMIN),
    "platform-admin": _person(_REQUESTER, role=TenantRole.OPERATOR, platform_admin=True),
}


async def _park(
    level: str = "dangerous",
    *,
    rights_change: bool = True,
    op_id: str = "demo.change",
    effect: dict[str, Any] | None = None,
    requester: str = _REQUESTER,
) -> uuid.UUID:
    proposed = effect if effect is not None else {"op_id": op_id, "safety_level": level}
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_person(requester),
            connector_id="demo-1.x",
            op_id=op_id,
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            proposed_effect=proposed,
            preview_hash="preview-hash" if level == "destructive" else None,
            rights_change=rights_change,
        )
        await session.commit()
        return request.id


async def _approve(request_id: uuid.UUID, operator: Operator) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=operator, params=_PARAMS)
        await session.commit()
        return request


async def _row(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row


@pytest.mark.asyncio
@pytest.mark.parametrize("level", _LEVELS)
@pytest.mark.parametrize("group", list(_NOT_ADMINS))
async def test_a_requester_who_is_not_an_admin_cannot_approve_their_own_marked_request(
    group: str, level: str
) -> None:
    request_id = await _park(level)

    with pytest.raises(UnauthorizedApprovalError):
        await _approve(request_id, _NOT_ADMINS[group])

    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["safe", "caution", "dangerous"])
@pytest.mark.parametrize("group", list(_ADMINS))
async def test_an_admin_approves_their_own_marked_request_in_the_console(
    group: str, level: str
) -> None:
    request_id = await _park(level)

    request = await _approve(request_id, _ADMINS[group])

    assert request.status == ApprovalRequestStatus.APPROVED.value
    assert request._approval_rule == ApprovalRule.SA_OPERATOR_SELF_APPROVAL  # type: ignore[attr-defined]
    assert request.reviewed_by == request.principal_sub


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["operator", "sa-operator"])
async def test_a_person_who_is_not_an_admin_gets_the_admin_sentence(group: str) -> None:
    request_id = await _park()

    with pytest.raises(RightsChangeApprovalError) as exc:
        await _approve(request_id, _NOT_ADMINS[group])

    assert exc.value.reason_text == RIGHTS_CHANGE_REFUSAL
    assert exc.value.reason_text == "This is a change of rights. Only an admin may approve it."


@pytest.mark.asyncio
@pytest.mark.parametrize("own", [True, False], ids=["own", "other"])
@pytest.mark.parametrize("group", list(_ADMINS))
async def test_an_admin_with_a_token_gets_the_console_sentence(group: str, own: bool) -> None:
    request_id = await _park(requester=_REQUESTER if own else "someone-else")
    admin = _ADMINS[group].model_copy(update={"web_session": False})

    with pytest.raises(RightsChangeApprovalError) as exc:
        await _approve(request_id, admin)

    assert exc.value.reason_text == RIGHTS_CHANGE_CONSOLE_ONLY
    assert exc.value.reason_text == (
        "This is a change of rights. Approve this change of rights in the web console."
    )
    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["safe", "caution"])
@pytest.mark.parametrize("group", list(_NOT_ADMINS))
async def test_the_break_glass_switch_does_not_clear_the_mark(
    group: str, level: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_glass_on(monkeypatch)
    request_id = await _park(level)

    with pytest.raises(UnauthorizedApprovalError):
        await _approve(request_id, _NOT_ADMINS[group])

    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["safe", "caution"])
async def test_the_break_glass_switch_does_not_clear_the_console_rule(
    level: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_glass_on(monkeypatch)
    request_id = await _park(level)

    with pytest.raises(RightsChangeApprovalError) as exc:
        await _approve(request_id, _person(_REQUESTER, role=TenantRole.TENANT_ADMIN, web=False))

    assert exc.value.reason_text == RIGHTS_CHANGE_CONSOLE_ONLY
    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "approver",
    [
        _person("person-admin", role=TenantRole.TENANT_ADMIN),
        _person("person-platform", platform_admin=True),
    ],
    ids=["tenant-admin", "platform-admin"],
)
async def test_another_admin_approves_a_marked_request(approver: Operator) -> None:
    request_id = await _park("caution")

    request = await _approve(request_id, approver)

    assert request.status == ApprovalRequestStatus.APPROVED.value
    assert request._approval_rule == ApprovalRule.REVIEWER  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "approver",
    [
        _person("person-op"),
        _person("person-sa", sa_operator=True),
        _person("person-approver", role=TenantRole.READ_ONLY, approver=True),
    ],
    ids=["operator", "sa-operator", "approver"],
)
async def test_another_person_who_is_not_an_admin_cannot_approve(approver: Operator) -> None:
    request_id = await _park("caution")

    with pytest.raises(RightsChangeApprovalError):
        await _approve(request_id, approver)

    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [PrincipalKind.AGENT, PrincipalKind.SERVICE])
async def test_a_machine_login_never_approves_a_marked_request(kind: PrincipalKind) -> None:
    request_id = await _park("caution")
    machine = _person("machine", role=TenantRole.TENANT_ADMIN, kind=kind)

    with pytest.raises(UnauthorizedApprovalError):
        await _approve(request_id, machine)

    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_a_request_without_the_mark_keeps_the_usual_rules() -> None:
    request_id = await _park("dangerous", rights_change=False)

    assert (await _row(request_id)).rights_change is None
    request = await _approve(request_id, _person(_REQUESTER, sa_operator=True))
    assert request.status == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
async def test_a_group_or_role_change_in_the_login_realm_is_marked_at_park(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(_GRANT_OPS, _GRANT_OP, _GrantOp(("user_id",), ()))
    effect = {"safety_level": "dangerous", "preview": {"grant": {"login_realm": True}}}

    request_id = await _park(rights_change=False, op_id=_GRANT_OP, effect=effect)

    assert (await _row(request_id)).rights_change is True


@pytest.mark.asyncio
async def test_a_group_or_role_change_outside_the_login_realm_is_not_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(_GRANT_OPS, _GRANT_OP, _GrantOp(("user_id",), ()))
    effect = {"safety_level": "dangerous", "preview": {"grant": {"login_realm": False}}}

    request_id = await _park(rights_change=False, op_id=_GRANT_OP, effect=effect)

    assert (await _row(request_id)).rights_change is None


@pytest.mark.parametrize(
    ("operator", "reason", "message"),
    [
        (_NOT_ADMINS["sa-operator"], ApproveRightReason.RIGHTS_CHANGE, RIGHTS_CHANGE_REFUSAL),
        (
            _person("person-sa", sa_operator=True),
            ApproveRightReason.RIGHTS_CHANGE,
            RIGHTS_CHANGE_REFUSAL,
        ),
        (
            _person(_REQUESTER, role=TenantRole.TENANT_ADMIN, web=False),
            ApproveRightReason.RIGHTS_CHANGE,
            RIGHTS_CHANGE_CONSOLE_ONLY,
        ),
        (
            _person("person-admin", role=TenantRole.TENANT_ADMIN, web=False),
            ApproveRightReason.RIGHTS_CHANGE,
            RIGHTS_CHANGE_CONSOLE_ONLY,
        ),
        (_NOT_ADMINS["reader"], ApproveRightReason.NO_APPROVAL_ACCESS, None),
    ],
    ids=["own-sa", "other-not-admin", "own-admin-token", "other-admin-token", "own-reader"],
)
def test_the_sign_says_who_must_approve(
    operator: Operator, reason: ApproveRightReason, message: str | None
) -> None:
    row = ApprovalRequest(
        id=uuid.uuid4(),
        status=ApprovalRequestStatus.PENDING.value,
        principal_sub=_REQUESTER,
        op_id="demo.change",
        proposed_effect={"safety_level": "caution"},
        rights_change=True,
    )

    right = approve_right(operator, row)

    assert right.may_approve is False
    assert right.reason == reason
    if message is not None:
        assert right.message == message


@pytest.mark.parametrize(
    ("operator", "reason"),
    [
        (_person("person-admin", role=TenantRole.TENANT_ADMIN), ApproveRightReason.OK),
        (_ADMINS["admin"], ApproveRightReason.OWN_CHANGE),
        (_ADMINS["platform-admin"], ApproveRightReason.OWN_CHANGE),
    ],
    ids=["other-admin", "own-admin", "own-platform-admin"],
)
def test_the_sign_allows_an_admin_in_the_console(
    operator: Operator, reason: ApproveRightReason
) -> None:
    row = ApprovalRequest(
        id=uuid.uuid4(),
        status=ApprovalRequestStatus.PENDING.value,
        principal_sub=_REQUESTER,
        op_id="demo.change",
        proposed_effect={"safety_level": "caution"},
        rights_change=True,
    )

    right = approve_right(operator, row)

    assert right.may_approve is True
    assert right.reason == reason


def test_an_unmarked_row_is_not_a_rights_change_and_the_views_say_so() -> None:
    marked = ApprovalRequest(op_id="demo.change", proposed_effect={}, rights_change=True)
    plain = ApprovalRequest(op_id="demo.change", proposed_effect={}, rights_change=None)

    assert is_rights_change(marked) is True
    assert is_rights_change(plain) is False


@pytest.mark.asyncio
async def test_the_rest_and_web_views_show_the_mark() -> None:
    marked = await _row(await _park())
    plain = await _row(await _park(rights_change=False))

    assert _view(marked, operator=_person(_REQUESTER)).rights_change is True
    assert _view(plain, operator=_person(_REQUESTER)).rights_change is False
    assert project_request_to_view(marked)["rights_change"] is True
    assert project_request_to_view(plain)["rights_change"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["approve", "decide"])
@pytest.mark.parametrize("approver", ["op-admin", "second-admin"], ids=["own", "other"])
async def test_rest_an_admin_is_refused_with_the_console_sentence(
    route: str,
    approver: str,
    _client: tuple[httpx.AsyncClient, Any],  # noqa: F811
    _fake_resume: list[uuid.UUID],  # noqa: F811
) -> None:
    ac, key = _client
    request_id = await _park("caution", requester="op-admin")
    token = _rest_token(key, approver, role=TenantRole.TENANT_ADMIN)

    response = await _post(ac, route, request_id, token)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == f"rights_change_required: {RIGHTS_CHANGE_CONSOLE_ONLY}"
    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value
    assert _fake_resume == []

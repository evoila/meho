# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The rights-change mark on an approval request.

A request with the mark changes who has rights. The requester never
approves it, in any group, at any risk level and with the break-glass switch
on. Another admin approves it. The same check serves the approve, the
"may I approve?" sign and run approvals.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

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
    )


#: The five groups and a platform admin, each as the person who asked.
_REQUESTERS = {
    "reader": _person(_REQUESTER, role=TenantRole.READ_ONLY),
    "operator": _person(_REQUESTER),
    "sa-operator": _person(_REQUESTER, sa_operator=True),
    "admin": _person(_REQUESTER, role=TenantRole.TENANT_ADMIN),
    "approver": _person(_REQUESTER, role=TenantRole.READ_ONLY, approver=True),
    "platform-admin": _person(_REQUESTER, role=TenantRole.TENANT_ADMIN, platform_admin=True),
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
@pytest.mark.parametrize("group", list(_REQUESTERS))
async def test_a_requester_in_any_group_cannot_approve_their_own_marked_request(
    group: str, level: str
) -> None:
    request_id = await _park(level)

    with pytest.raises(UnauthorizedApprovalError):
        await _approve(request_id, _REQUESTERS[group])

    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["operator", "sa-operator", "admin", "platform-admin"])
async def test_the_refusal_is_one_sentence(group: str) -> None:
    request_id = await _park()

    with pytest.raises(RightsChangeApprovalError) as exc:
        await _approve(request_id, _REQUESTERS[group])

    assert exc.value.reason_text == RIGHTS_CHANGE_REFUSAL
    assert exc.value.reason_text == "This is a change of rights. Another admin must approve it."


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["safe", "caution"])
@pytest.mark.parametrize("group", list(_REQUESTERS))
async def test_the_break_glass_switch_does_not_clear_the_mark(
    group: str, level: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_glass_on(monkeypatch)
    request_id = await _park(level)

    with pytest.raises(UnauthorizedApprovalError):
        await _approve(request_id, _REQUESTERS[group])

    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
async def test_the_requester_is_compared_without_case() -> None:
    request_id = await _park(requester="ABCD-EF")

    with pytest.raises(RightsChangeApprovalError):
        await _approve(request_id, _person("abcd-ef", role=TenantRole.TENANT_ADMIN))


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
    ("operator", "reason"),
    [
        (_REQUESTERS["sa-operator"], ApproveRightReason.RIGHTS_CHANGE),
        (_REQUESTERS["admin"], ApproveRightReason.RIGHTS_CHANGE),
        (_REQUESTERS["platform-admin"], ApproveRightReason.RIGHTS_CHANGE),
        (_person("person-sa", sa_operator=True), ApproveRightReason.RIGHTS_CHANGE),
        (_REQUESTERS["reader"], ApproveRightReason.NO_APPROVAL_ACCESS),
    ],
    ids=["own-sa", "own-admin", "own-platform-admin", "other-not-admin", "own-reader"],
)
def test_the_sign_says_who_must_approve(operator: Operator, reason: ApproveRightReason) -> None:
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
    if reason == ApproveRightReason.RIGHTS_CHANGE:
        assert right.message == RIGHTS_CHANGE_REFUSAL


def test_the_sign_allows_another_admin() -> None:
    row = ApprovalRequest(
        id=uuid.uuid4(),
        status=ApprovalRequestStatus.PENDING.value,
        principal_sub=_REQUESTER,
        op_id="demo.change",
        proposed_effect={"safety_level": "caution"},
        rights_change=True,
    )

    right = approve_right(_person("person-admin", role=TenantRole.TENANT_ADMIN), row)

    assert right.may_approve is True
    assert right.reason == ApproveRightReason.OK


def test_an_unmarked_row_is_not_a_rights_change_and_the_views_say_so() -> None:
    marked = ApprovalRequest(op_id="demo.change", proposed_effect={}, rights_change=True)
    plain = ApprovalRequest(op_id="demo.change", proposed_effect={}, rights_change=None)

    assert is_rights_change(marked) is True
    assert is_rights_change(plain) is False


@pytest.mark.asyncio
async def test_the_rest_and_web_views_show_the_mark() -> None:
    marked = await _row(await _park())
    plain = await _row(await _park(rights_change=False))

    assert _view(marked).rights_change is True
    assert _view(plain).rights_change is False
    assert project_request_to_view(marked)["rights_change"] is True
    assert project_request_to_view(plain)["rights_change"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["approve", "decide"])
async def test_rest_the_requester_is_refused_with_the_one_sentence(
    route: str,
    _client: tuple[httpx.AsyncClient, Any],  # noqa: F811
    _fake_resume: list[uuid.UUID],  # noqa: F811
) -> None:
    ac, key = _client
    request_id = await _park("caution", requester="op-admin")
    token = _rest_token(key, "op-admin", role=TenantRole.TENANT_ADMIN)

    response = await _post(ac, route, request_id, token)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == f"rights_change_required: {RIGHTS_CHANGE_REFUSAL}"
    assert (await _row(request_id)).status == ApprovalRequestStatus.PENDING.value
    assert _fake_resume == []


@pytest.mark.asyncio
async def test_rest_another_admin_approves(
    _client: tuple[httpx.AsyncClient, Any],  # noqa: F811
    _fake_resume: list[uuid.UUID],  # noqa: F811
) -> None:
    ac, key = _client
    request_id = await _park("caution", requester="op-admin")
    token = _rest_token(key, "second-admin", role=TenantRole.TENANT_ADMIN)

    with patch(
        "meho_backplane.operations.addon_step_events.AddonStepEventService.record_if_owned_committed",
        new_callable=AsyncMock,
    ):
        response = await _post(ac, "approve", request_id, token)

    assert response.status_code == 200, response.text
    assert (await _row(request_id)).status == ApprovalRequestStatus.APPROVED.value

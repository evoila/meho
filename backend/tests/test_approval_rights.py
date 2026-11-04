# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The "may I approve this?" answer (#3329).

``approve_right`` tells a person, for one request, whether they may approve
it and why not. These tests check that:

* each rule of #3328 gives the right answer and reason;
* the answer always matches what ``approve_request`` then does;
* the read logs no break-glass warning;
* REST list and show carry ``may_approve`` and ``may_approve_reason`` for
  the caller;
* ``/decide`` says whether the approval was a self-approval, and which rule
  allowed it.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations.approval_queue import (
    ApprovalError,
    ApprovalGroupForbiddenError,
    ApprovalRule,
    approve_request,
)
from meho_backplane.operations.approval_rights import (
    ApproveRightReason,
    approve_right,
    self_approval_confirmation,
)
from tests._oidc_jwt_helpers import mint_token
from tests.test_api_v1_approvals_fresh_sign_in import (
    _TENANT_A,
    _client,  # noqa: F401 -- fixture: the REST test client and its signing key
    _fake_resume,  # noqa: F401 -- fixture: records the re-dispatch after approval
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)
from tests.test_approval_group_rules import (
    _OTHER,
    _PARAMS,
    _REQUESTER,
    _break_glass_on,
    _park,
    _person,
    _post,
    _rest_token,
)

_ROLE_FLOOR_OP = "targets.register"


def _row(
    level: str | None,
    *,
    requester: str = _REQUESTER,
    status: str = ApprovalRequestStatus.PENDING.value,
    op_id: str = "demo.change",
) -> ApprovalRequest:
    """An in-memory request; ``approve_right`` reads only its fields."""
    effect = {} if level is None else {"op_id": op_id, "safety_level": level}
    return ApprovalRequest(
        id=uuid.uuid4(),
        status=status,
        principal_sub=requester,
        op_id=op_id,
        proposed_effect=effect,
    )


#: (case name, approver, request level, requester, expected may_approve,
#: expected reason, expected rule)
_CASES: list[tuple[str, Operator, str | None, str, bool, str, ApprovalRule | None]] = [
    (
        "operator, another person's caution change",
        _person(_OTHER),
        "caution",
        _REQUESTER,
        True,
        "ok",
        ApprovalRule.REVIEWER,
    ),
    (
        "operator, another person's destructive change",
        _person(_OTHER),
        "destructive",
        _REQUESTER,
        False,
        "wrong_group",
        None,
    ),
    (
        "approver flag, another person's dangerous change",
        _person(_OTHER, role=TenantRole.READ_ONLY, approver=True),
        "dangerous",
        _REQUESTER,
        False,
        "wrong_group",
        None,
    ),
    (
        "admin, another person's destructive change",
        _person(_OTHER, role=TenantRole.TENANT_ADMIN),
        "destructive",
        _REQUESTER,
        True,
        "ok",
        ApprovalRule.SA_OPERATOR_OR_ADMIN,
    ),
    (
        "SA-Operator, another person's dangerous change",
        _person(_OTHER, sa_operator=True),
        "dangerous",
        _REQUESTER,
        True,
        "ok",
        ApprovalRule.SA_OPERATOR_OR_ADMIN,
    ),
    (
        "SA-Operator, own destructive change",
        _person(_REQUESTER, sa_operator=True),
        "destructive",
        _REQUESTER,
        True,
        "own_change",
        ApprovalRule.SA_OPERATOR_SELF_APPROVAL,
    ),
    (
        "operator, own caution change",
        _person(_REQUESTER),
        "caution",
        _REQUESTER,
        False,
        "own_change",
        None,
    ),
    (
        "admin, own dangerous change",
        _person(_REQUESTER, role=TenantRole.TENANT_ADMIN),
        "dangerous",
        _REQUESTER,
        False,
        "own_change",
        None,
    ),
    (
        "read_only without approver flag",
        _person(_OTHER, role=TenantRole.READ_ONLY),
        "safe",
        _REQUESTER,
        False,
        "no_approval_access",
        None,
    ),
    (
        "agent with the SA-Operators flag",
        _person(_OTHER, sa_operator=True, kind=PrincipalKind.AGENT),
        "safe",
        _REQUESTER,
        False,
        "not_a_person",
        None,
    ),
]


@pytest.mark.parametrize(
    ("approver", "level", "requester", "may", "reason", "rule"),
    [case[1:] for case in _CASES],
    ids=[case[0] for case in _CASES],
)
def test_the_answer_follows_the_group_rules(
    approver: Operator,
    level: str | None,
    requester: str,
    may: bool,
    reason: str,
    rule: ApprovalRule | None,
) -> None:
    right = approve_right(approver, _row(level, requester=requester))

    assert right.may_approve is may
    assert right.reason == reason
    assert right.rule == rule
    assert right.self_approval is (rule == ApprovalRule.SA_OPERATOR_SELF_APPROVAL)
    assert right.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approver", "level", "requester", "may"),
    [(c[1], c[2], c[3], c[4]) for c in _CASES],
    ids=[case[0] for case in _CASES],
)
async def test_the_answer_matches_what_the_approve_does(
    approver: Operator, level: str | None, requester: str, may: bool
) -> None:
    """The sign never says yes where the approve refuses, or the other way."""
    request_id = await _park(level, requester=requester)
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        assert approve_right(approver, row).may_approve is may
        try:
            await approve_request(session, request_id, operator=approver, params=_PARAMS)
        except ApprovalError:
            approved = False
        else:
            approved = True
    assert approved is may


def test_a_refusal_shows_the_text_of_the_approve() -> None:
    row = _row("destructive")
    right = approve_right(_person(_OTHER), row)

    expected = ApprovalGroupForbiddenError(
        row.id, operator_sub=_OTHER, role="operator", safety_level="destructive"
    ).reason_text
    assert right.message == expected


def test_an_own_lower_risk_change_names_the_break_glass_switch() -> None:
    low = approve_right(_person(_REQUESTER), _row("caution"))
    high = approve_right(_person(_REQUESTER), _row("destructive"))

    assert "APPROVAL_ALLOW_SELF_APPROVAL" in low.message
    assert "APPROVAL_ALLOW_SELF_APPROVAL" not in high.message
    assert "MEHO-SA-Operator or a MEHO-Admin" in high.message


def test_break_glass_allows_an_own_lower_risk_change_and_logs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _break_glass_on(monkeypatch)

    with capture_logs() as logs:
        right = approve_right(_person(_REQUESTER), _row("caution"))
        high = approve_right(_person(_REQUESTER), _row("dangerous"))

    assert right.may_approve is True
    assert right.reason == ApproveRightReason.OWN_CHANGE
    assert right.rule == ApprovalRule.BREAK_GLASS_SELF_APPROVAL
    assert right.self_approval is True
    assert "APPROVAL_ALLOW_SELF_APPROVAL" in right.message
    assert high.may_approve is False
    assert not [e for e in logs if e["event"] == "approval_self_approval_break_glass"]


@pytest.mark.parametrize(
    "status",
    [s.value for s in ApprovalRequestStatus if s != ApprovalRequestStatus.PENDING],
)
def test_a_request_that_is_not_pending_cannot_be_approved(status: str) -> None:
    right = approve_right(_person(_OTHER, sa_operator=True), _row("safe", status=status))

    assert right.may_approve is False
    assert right.reason == ApproveRightReason.NOT_PENDING
    assert status in right.message


def test_a_role_too_low_for_the_operation_cannot_approve() -> None:
    row = _row("caution", op_id=_ROLE_FLOOR_OP)

    operator = approve_right(_person(_OTHER), row)
    admin = approve_right(_person(_OTHER, role=TenantRole.TENANT_ADMIN), row)

    assert operator.may_approve is False
    assert operator.reason == ApproveRightReason.ROLE_TOO_LOW
    assert "tenant_admin" in operator.message
    assert admin.may_approve is True


def test_the_confirmation_line_names_the_self_approval_rule() -> None:
    sa = self_approval_confirmation(ApprovalRule.SA_OPERATOR_SELF_APPROVAL)
    break_glass = self_approval_confirmation(ApprovalRule.BREAK_GLASS_SELF_APPROVAL)

    assert sa is not None and "MEHO-SA-Operator" in sa and "audit log" in sa
    assert break_glass is not None and "APPROVAL_ALLOW_SELF_APPROVAL" in break_glass
    assert self_approval_confirmation(ApprovalRule.REVIEWER) is None
    assert self_approval_confirmation(ApprovalRule.SA_OPERATOR_OR_ADMIN) is None
    assert self_approval_confirmation(None) is None


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def _read_token(key: Any, sub: str, *, groups: list[str] | None = None) -> str:
    """A plain login token: enough to read the approvals list and show."""
    extra = {"groups": groups} if groups is not None else None
    return mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(_TENANT_A),
        extra_claims=extra,
    )


@pytest.mark.asyncio
async def test_rest_list_and_show_carry_the_answer_for_the_caller(
    _client: tuple[httpx.AsyncClient, Any],  # noqa: F811
) -> None:
    ac, key = _client
    own = await _park("caution", requester="op-reader")
    others = await _park("destructive", requester="someone-else")
    headers = {"Authorization": f"Bearer {_read_token(key, 'op-reader')}"}

    listed = await ac.get("/api/v1/approvals", headers=headers)
    shown = await ac.get(f"/api/v1/approvals/{others}", headers=headers)

    assert listed.status_code == 200, listed.text
    by_id = {row["id"]: row for row in listed.json()}
    assert by_id[str(own)]["may_approve"] is False
    assert by_id[str(own)]["may_approve_reason"] == "own_change"
    assert by_id[str(others)]["may_approve"] is False
    assert by_id[str(others)]["may_approve_reason"] == "wrong_group"
    assert shown.status_code == 200, shown.text
    assert shown.json()["may_approve"] is False
    assert shown.json()["may_approve_reason"] == "wrong_group"


@pytest.mark.asyncio
async def test_rest_an_sa_operator_sees_yes_for_their_own_change(
    _client: tuple[httpx.AsyncClient, Any],  # noqa: F811
) -> None:
    ac, key = _client
    own = await _park("destructive", requester="op-sa")
    token = _read_token(key, "op-sa", groups=["MEHO-SA-Operators"])

    shown = await ac.get(f"/api/v1/approvals/{own}", headers={"Authorization": f"Bearer {token}"})

    assert shown.status_code == 200, shown.text
    assert shown.json()["may_approve"] is True
    assert shown.json()["may_approve_reason"] == "own_change"


@pytest.mark.asyncio
async def test_rest_a_decided_request_says_not_pending(
    _client: tuple[httpx.AsyncClient, Any],  # noqa: F811
    _fake_resume: list[uuid.UUID],  # noqa: F811
) -> None:
    ac, key = _client
    request_id = await _park("caution", requester="someone-else")
    approved = await _post(ac, "decide", request_id, _rest_token(key, "op-other"))
    assert approved.status_code == 200, approved.text

    shown = await ac.get(
        f"/api/v1/approvals/{request_id}",
        headers={"Authorization": f"Bearer {_read_token(key, 'op-reader')}"},
    )

    assert shown.json()["may_approve"] is False
    assert shown.json()["may_approve_reason"] == "not_pending"


@pytest.mark.asyncio
async def test_rest_decide_says_it_was_a_self_approval(
    _client: tuple[httpx.AsyncClient, Any],  # noqa: F811
    _fake_resume: list[uuid.UUID],  # noqa: F811
) -> None:
    ac, key = _client
    own = await _park("destructive", requester="op-sa")
    others = await _park("caution", requester="someone-else")

    self_approved = await _post(
        ac, "decide", own, _rest_token(key, "op-sa", groups=["MEHO-SA-Operators"])
    )
    other_approved = await _post(ac, "decide", others, _rest_token(key, "op-other"))

    assert self_approved.status_code == 200, self_approved.text
    assert self_approved.json()["self_approved"] is True
    assert self_approved.json()["approval_rule"] == "sa_operator_self_approval"
    assert other_approved.status_code == 200, other_approved.text
    assert other_approved.json()["self_approved"] is False
    assert other_approved.json()["approval_rule"] == "reviewer"


@pytest.mark.asyncio
async def test_rest_a_rejection_carries_no_self_approval_fields(
    _client: tuple[httpx.AsyncClient, Any],  # noqa: F811
) -> None:
    ac, key = _client
    request_id = await _park("caution", requester="someone-else")

    response = await ac.post(
        f"/api/v1/approvals/{request_id}/decide",
        json={"decision": "rejected", "reason": ""},
        headers={"Authorization": f"Bearer {_read_token(key, 'op-other')}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["self_approved"] is None
    assert response.json()["approval_rule"] is None

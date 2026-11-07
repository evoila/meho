# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who may approve a waiting change (#3328).

The rules are in ``docs/decisions/approval-groups-and-self-approval.md``.
These tests check the code against them on the service layer and over REST:

* a MEHO-SA-Operator may approve their own change, at every risk level;
* another person's ``dangerous`` or ``destructive`` change may be approved
  only by the groups the decision record names; everyone else gets 403;
* a lower-risk change keeps the reviewer rule (#3243);
* a machine principal never approves, also with the SA-Operators flag;
* the emergency switch still covers only ``safe`` and ``caution`` changes;
* the audit row and the step event say whether it was a self-approval and
  which rule allowed it.
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditLog
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalGroupForbiddenError,
    ApprovalRule,
    NonHumanApprovalError,
    SelfApprovalForbiddenError,
    UnauthorizedApprovalError,
    approve_request,
    create_pending_request,
    record_approval_step_event,
)
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import APPROVAL_CLIENT_ID, mint_token
from tests.test_api_v1_approvals_fresh_sign_in import (
    _TENANT_A,
    _client,  # noqa: F401 -- fixture: the REST test client and its signing key
    _fake_resume,  # noqa: F401 -- fixture: records the re-dispatch after approval
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)

_PARAMS: dict[str, Any] = {"name": "demo"}
_REQUESTER = "person-requester"
_OTHER = "person-other"
_HIGH_RISK = ["dangerous", "destructive"]
_LOW_RISK = ["safe", "caution"]
_STEP_EVENT = (
    "meho_backplane.operations.addon_step_events.AddonStepEventService.record_if_owned_committed"
)


def _person(
    sub: str,
    *,
    role: TenantRole = TenantRole.OPERATOR,
    sa_operator: bool = False,
    approver: bool = False,
    kind: PrincipalKind = PrincipalKind.USER,
    name: str | None = None,
) -> Operator:
    return Operator(
        sub=sub,
        name=name,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=role,
        principal_kind=kind,
        approver=approver,
        self_approver=sa_operator,
    )


async def _park(level: str | None, *, requester: str = _REQUESTER) -> uuid.UUID:
    """Park one change by *requester* at risk *level* and return its id."""
    effect = None if level is None else {"op_id": "demo.change", "safety_level": level}
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_person(requester),
            connector_id="demo-1.x",
            op_id="demo.change",
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            proposed_effect=effect,
            preview_hash="preview-hash" if level == "destructive" else None,
        )
        await session.commit()
        return request.id


async def _approve(request_id: uuid.UUID, operator: Operator) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=operator, params=_PARAMS)
        await session.commit()
        return request


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


def _break_glass_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true")
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Own change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [*_LOW_RISK, *_HIGH_RISK])
async def test_an_sa_operator_approves_their_own_change_at_every_level(level: str) -> None:
    request_id = await _park(level)

    await _approve(request_id, _person(_REQUESTER, sa_operator=True))

    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    payload = await _decision_payload(request_id)
    assert payload["self_approved"] is True
    assert payload["approval_rule"] == ApprovalRule.SA_OPERATOR_SELF_APPROVAL.value


@pytest.mark.asyncio
async def test_the_step_event_of_a_self_approval_carries_the_same_fields() -> None:
    request_id = await _park("destructive")
    request = await _approve(request_id, _person(_REQUESTER, sa_operator=True))

    with patch(_STEP_EVENT, new_callable=AsyncMock) as record:
        await record_approval_step_event(
            tenant_id=_TENANT_A, request=request, decision="approved", audit_id=uuid.uuid4()
        )

    payload = record.await_args.kwargs["payload"]
    assert record.await_args.kwargs["event_kind"] == "approval.approved"
    audit_payload = await _decision_payload(request_id)
    assert payload["reviewed_by"] == _REQUESTER
    assert payload["reviewed_by"] == audit_payload["reviewed_by"]
    assert payload["self_approved"] is True
    assert payload["approval_rule"] == "sa_operator_self_approval"


@pytest.mark.asyncio
@pytest.mark.parametrize("level", _HIGH_RISK)
@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.TENANT_ADMIN])
async def test_others_cannot_approve_their_own_high_risk_change(
    level: str, role: TenantRole, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Also an admin, and also with the emergency switch on."""
    _break_glass_on(monkeypatch)
    request_id = await _park(level)

    with pytest.raises(SelfApprovalForbiddenError) as exc:
        await _approve(request_id, _person(_REQUESTER, role=role))

    assert exc.value.high_risk is True
    message = str(exc.value)
    assert "you cannot approve your own change" in message.lower()
    assert "MEHO-SA-Operators" in message
    assert "APPROVAL_ALLOW_SELF_APPROVAL" not in message
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [*_LOW_RISK, None])
async def test_others_cannot_approve_their_own_lower_risk_change(level: str | None) -> None:
    request_id = await _park(level)

    with pytest.raises(SelfApprovalForbiddenError) as exc:
        await _approve(request_id, _person(_REQUESTER))

    assert exc.value.high_risk is False
    assert "APPROVAL_ALLOW_SELF_APPROVAL" in str(exc.value)
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("level", _LOW_RISK)
async def test_the_emergency_switch_still_covers_safe_and_caution(
    level: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_glass_on(monkeypatch)
    request_id = await _park(level)

    await _approve(request_id, _person(_REQUESTER))

    payload = await _decision_payload(request_id)
    assert payload["self_approved"] is True
    assert payload["approval_rule"] == ApprovalRule.BREAK_GLASS_SELF_APPROVAL.value


@pytest.mark.asyncio
async def test_a_display_name_never_makes_a_self_approval() -> None:
    """Only the ``sub`` counts: the same display name is another person."""
    request_id = await _park("caution")

    await _approve(request_id, _person(_OTHER, name=_REQUESTER))

    payload = await _decision_payload(request_id)
    assert payload["self_approved"] is False
    assert payload["approval_rule"] == ApprovalRule.REVIEWER.value


# ---------------------------------------------------------------------------
# Another person's change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("level", _HIGH_RISK)
@pytest.mark.parametrize(
    "approver",
    [
        _person(_OTHER, sa_operator=True),
        _person(_OTHER, role=TenantRole.TENANT_ADMIN),
    ],
    ids=["sa-operator", "admin"],
)
async def test_the_named_groups_approve_another_persons_high_risk_change(
    level: str, approver: Operator
) -> None:
    request_id = await _park(level)

    await _approve(request_id, approver)

    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    payload = await _decision_payload(request_id)
    assert payload["self_approved"] is False
    assert payload["approval_rule"] == ApprovalRule.SA_OPERATOR_OR_ADMIN.value


@pytest.mark.asyncio
@pytest.mark.parametrize("level", _HIGH_RISK)
@pytest.mark.parametrize(
    "approver",
    [
        _person(_OTHER),
        _person(_OTHER, role=TenantRole.READ_ONLY, approver=True),
    ],
    ids=["operator", "approver"],
)
async def test_other_groups_cannot_approve_another_persons_high_risk_change(
    level: str, approver: Operator
) -> None:
    request_id = await _park(level)

    with pytest.raises(ApprovalGroupForbiddenError) as exc:
        await _approve(request_id, approver)

    assert isinstance(exc.value, UnauthorizedApprovalError)
    assert exc.value.safety_level == level
    assert "MEHO-SA-Operator or a MEHO-Admin" in str(exc.value)
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [*_LOW_RISK, None])
@pytest.mark.parametrize(
    "approver",
    [
        _person(_OTHER),
        _person(_OTHER, role=TenantRole.READ_ONLY, approver=True),
    ],
    ids=["operator", "approver"],
)
async def test_a_lower_risk_change_keeps_the_reviewer_rule(
    level: str | None, approver: Operator
) -> None:
    """A ``requires_approval`` change, or one without a stamped level (#3243)."""
    request_id = await _park(level)

    await _approve(request_id, approver)

    payload = await _decision_payload(request_id)
    assert payload["self_approved"] is False
    assert payload["approval_rule"] == ApprovalRule.REVIEWER.value


@pytest.mark.asyncio
async def test_read_only_without_the_approver_flag_is_still_refused() -> None:
    """The #3243 split is unchanged: this is the role refusal, not the group one."""
    request_id = await _park("caution")

    with pytest.raises(UnauthorizedApprovalError) as exc:
        await _approve(request_id, _person(_OTHER, role=TenantRole.READ_ONLY))

    assert not isinstance(exc.value, ApprovalGroupForbiddenError)


# ---------------------------------------------------------------------------
# Machines never approve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [PrincipalKind.AGENT, PrincipalKind.SERVICE])
@pytest.mark.parametrize("own", [True, False], ids=["own-change", "other-change"])
async def test_a_machine_with_the_sa_operators_flag_is_refused(
    kind: PrincipalKind, own: bool
) -> None:
    machine = _person("machine-sub", kind=kind, sa_operator=True, role=TenantRole.TENANT_ADMIN)
    request_id = await _park("destructive", requester=machine.sub if own else _REQUESTER)

    with pytest.raises(NonHumanApprovalError):
        await _approve(request_id, machine)

    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def _rest_token(key: Any, sub: str, *, groups: list[str] | None = None) -> str:
    """A person's fresh approval sign-in; *groups* is the ``groups`` claim."""
    claims: dict[str, Any] = {
        "azp": APPROVAL_CLIENT_ID,
        "acr": "1",
        "auth_time": int(time.time()),
        "sid": f"sid-{uuid.uuid4()}",
    }
    if groups is not None:
        claims["groups"] = groups
    return mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(_TENANT_A),
        extra_claims=claims,
    )


async def _post(ac: httpx.AsyncClient, route: str, request_id: uuid.UUID, token: str) -> Any:
    body: dict[str, Any] = (
        {"params": _PARAMS} if route == "approve" else {"decision": "approved", "reason": ""}
    )
    return await ac.post(
        f"/api/v1/approvals/{request_id}/{route}",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["approve", "decide"])
async def test_rest_an_sa_operator_approves_their_own_destructive_change(
    route: str,
    _client: tuple[httpx.AsyncClient, Any],  # noqa: F811
    _fake_resume: list[uuid.UUID],  # noqa: F811
) -> None:
    ac, key = _client
    request_id = await _park("destructive", requester="op-sa")
    token = _rest_token(key, "op-sa", groups=["MEHO-SA-Operators"])

    response = await _post(ac, route, request_id, token)

    assert response.status_code == 200, response.text
    assert _fake_resume == [request_id]
    payload = await _decision_payload(request_id)
    assert payload["self_approved"] is True
    assert payload["approval_rule"] == "sa_operator_self_approval"
    assert payload["approval_auth"]["method"] == "fresh_sign_in"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["approve", "decide"])
async def test_rest_an_operator_cannot_approve_their_own_dangerous_change(
    route: str,
    _client: tuple[httpx.AsyncClient, Any],  # noqa: F811
    _fake_resume: list[uuid.UUID],  # noqa: F811
) -> None:
    ac, key = _client
    request_id = await _park("dangerous", requester="op-plain")

    response = await _post(ac, route, request_id, _rest_token(key, "op-plain"))

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail.startswith("self_approval_forbidden:")
    assert "Only members of the MEHO-SA-Operators group" in detail
    assert "MEHO-SA-Operator or a MEHO-Admin" in detail
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value
    assert _fake_resume == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["approve", "decide"])
async def test_rest_an_operator_cannot_approve_another_persons_destructive_change(
    route: str,
    _client: tuple[httpx.AsyncClient, Any],  # noqa: F811
    _fake_resume: list[uuid.UUID],  # noqa: F811
) -> None:
    ac, key = _client
    request_id = await _park("destructive")

    response = await _post(ac, route, request_id, _rest_token(key, "op-plain"))

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail.startswith("approval_group_required:")
    assert "destructive change" in detail
    assert "MEHO-SA-Operator or a MEHO-Admin" in detail
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value
    assert _fake_resume == []

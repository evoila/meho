# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Whole-path tests of the fixed rules on who asks and who approves.

* With the switch ``CUSTOM_POLICY`` on, only a person may ask for a change
  of the policy or of rights. The rule holds for every such operation from
  the call gate, also for a stand-in operation that brings no check of its
  own: an agent login, a service login and a hosted agent run are refused
  before anything waits; a person's token waits.
* In sandbox mode the gate itself holds a change of rights: a standing
  grant alone and a run approval alone do not run a service login's change
  of rights, also for an operation with no per-call approval mark.
* The approve path names the rule ``floor:agent_never_approves`` when an
  agent, a service login or a runner tries to approve.

The calls go over ``POST /api/v1/operations/call`` and
``POST /api/v1/approvals/{id}/approve`` with signed tokens, and through the
dispatcher for the hosted agent run, as the hosted agent does. The
connector, the operations and the target are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
    ServicePrincipalGrant,
    Target,
)
from meho_backplane.op_classes import _PER_CALL_APPROVAL, needs_per_call_approval
from meho_backplane.operations import dispatch
from meho_backplane.operations.approval_queue import NonHumanApprovalError, _check_reviewer_role
from meho_backplane.operations.grant_approval import _RIGHTS_CHANGE_OPS, is_rights_change_op
from meho_backplane.operations.role_floors import has_requester_check
from tests._fixed_rules_helpers import (
    CONNECTOR,
    PRODUCT,
    RUNS,
    TARGET,
    call_as,
    call_rows,
    register,
)
from tests._hard_to_undo_helpers import (
    SERVICE,
    TENANT,
    add_connector,
    custom_policy,
    finish,
    request_op_ids,
    run_approval_uses,
    seed_run_approval,
    seed_target,
    service_client,
    start,
)
from tests._oidc_jwt_helpers import mint_token
from tests._sandbox_mode_helpers import (
    grant_use_rows,
    seed_grant_row,
    turn_sandbox_mode_off,
    turn_sandbox_mode_on,
)

#: A change of rights with no check of who may ask and no per-call mark.
_STAND_IN = "rulebox.role.give"
_PLAIN = "rulebox.box.tidy"
_RULE = "floor:agent_never_approves"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    start(monkeypatch)
    add_connector(PRODUCT["product"], PRODUCT["version"], PRODUCT["impl_id"])
    _RIGHTS_CHANGE_OPS.add(_STAND_IN)
    RUNS.clear()
    yield
    _RIGHTS_CHANGE_OPS.discard(_STAND_IN)
    _PER_CALL_APPROVAL.discard(_STAND_IN)
    turn_sandbox_mode_off(monkeypatch)
    RUNS.clear()
    finish()


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    async with service_client() as c:
        yield c


async def _seed(op_id: str = _STAND_IN) -> Target:
    await register(op_id, requires_approval=False)
    target_id = await seed_target(TARGET, product="rulebox", version="1.x")
    async with get_sessionmaker()() as session:
        target = await session.get(Target, target_id)
    assert target is not None
    return target


async def _hosted_run(target: Target) -> Any:
    """A hosted agent run that a person started calls the operation."""
    person = Operator(
        sub="person-starter",
        raw_jwt="header.payload.signature",
        tenant_id=TENANT,
        tenant_role=TenantRole.OPERATOR,
    )
    with actor_delegation("hosted-agent:rule-helper"):
        return await dispatch(
            operator=person, connector_id=CONNECTOR, op_id=_STAND_IN, target=target, params={}
        )


def test_the_stand_in_brings_no_check_of_its_own() -> None:
    assert is_rights_change_op(_STAND_IN)
    assert not has_requester_check(_STAND_IN)
    assert not needs_per_call_approval(_STAND_IN)


@pytest.mark.asyncio
async def test_with_the_switch_on_only_a_person_may_ask_for_a_change_of_rights(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    custom_policy(monkeypatch, on=True)
    target = await _seed()

    agent = await call_as(client, _STAND_IN, kind="agent", sub="agent:rule-bot")
    service = await call_as(client, _STAND_IN, kind="service", sub=SERVICE)
    hosted = await _hosted_run(target)
    person = await call_as(client, _STAND_IN, kind="user", sub="person-token")

    for refused in (agent, service):
        assert refused["status"] == "denied", refused
        assert "only a person may ask for a change of the policy or of rights" in str(refused)
    assert hosted.status == "denied", hosted
    assert "only a person may ask" in str(hosted.error)
    assert person["status"] == "awaiting_approval", person
    assert RUNS == []
    assert await request_op_ids() == [_STAND_IN]
    assert sorted(row.status_code for row in await call_rows(_STAND_IN)) == [403, 403, 403]


@pytest.mark.asyncio
async def test_with_the_switch_off_an_agent_still_asks_and_waits(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    custom_policy(monkeypatch, on=False)
    await _seed()

    agent = await call_as(client, _STAND_IN, kind="agent", sub="agent:rule-bot")

    assert agent["status"] == "awaiting_approval", agent
    assert await request_op_ids() == [_STAND_IN]
    assert await call_rows(_STAND_IN) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [True, False], ids=["switch_on", "switch_off"])
async def test_in_sandbox_mode_no_grant_and_no_run_approval_runs_a_change_of_rights(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], on: bool
) -> None:
    custom_policy(monkeypatch, on=on)
    turn_sandbox_mode_on(monkeypatch)
    target = await _seed()
    grant_id = await seed_grant_row(
        model=ServicePrincipalGrant,
        tenant_id=TENANT,
        principal_sub=SERVICE,
        op_id=_STAND_IN,
        connector_id=CONNECTOR,
        target_product="rulebox",
    )
    with_grant = await call_as(client, _STAND_IN, kind="service", sub=SERVICE)
    other = "svc:rule-other"
    approval_id = await _run_approval_for(other, target.id)
    with_run_approval = await call_as(
        client, _STAND_IN, kind="service", sub=other, work_ref="run-rights"
    )

    # With the switch on a service login may not even ask; with it off the call waits.
    expected = "denied" if on else "awaiting_approval"
    assert with_grant["status"] == expected, with_grant
    assert with_run_approval["status"] == expected, with_run_approval
    assert RUNS == []
    assert await request_op_ids() == ([] if on else [_STAND_IN, _STAND_IN])
    assert await grant_use_rows(grant_id) == []
    assert await run_approval_uses(approval_id) == 0


async def _run_approval_for(sub: str, target_id: uuid.UUID) -> uuid.UUID:
    approval_id = await seed_run_approval(
        target_id, work_ref="run-rights", connector_id=CONNECTOR, op_ids=(_STAND_IN,)
    )
    async with get_sessionmaker()() as session, session.begin():
        from meho_backplane.db.models import RunApproval

        row = await session.get(RunApproval, approval_id)
        assert row is not None
        row.principal_sub = sub
    return approval_id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "service"])
async def test_the_approve_path_names_the_rule_for_a_machine_login(
    client: tuple[httpx.AsyncClient, Any], kind: str
) -> None:
    await _seed(_PLAIN)
    waiting = await call_as(client, _PLAIN, kind="service", sub=SERVICE)
    assert waiting["status"] == "awaiting_approval", waiting
    request_id = waiting["extras"]["approval_request_id"]

    ac, key = client
    token = mint_token(
        key,
        sub=f"{kind}:approver",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(TENANT),
        principal_kind=kind,
        extra_claims={"client_id": "rule-client"} if kind == "service" else None,
    )
    response = await ac.post(
        f"/api/v1/approvals/{request_id}/approve",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403, response.text
    assert _RULE in response.json()["detail"]
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, uuid.UUID(request_id))
    assert row is not None and row.status == ApprovalRequestStatus.PENDING.value
    assert RUNS == []


@pytest.mark.parametrize("kind", [PrincipalKind.AGENT, PrincipalKind.SERVICE, PrincipalKind.RUNNER])
def test_the_service_layer_check_names_the_rule(kind: PrincipalKind) -> None:
    """The check behind every approve path; a runner token never reaches the REST route."""
    operator = Operator(
        sub="machine",
        raw_jwt="header.payload.signature",
        tenant_id=TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=kind,
    )
    with pytest.raises(NonHumanApprovalError) as refused:
        _check_reviewer_role(operator)
    assert refused.value.rule == _RULE
    assert _RULE in str(refused.value)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A request of an agent, service or runner login counts as a request of the people behind it.

A person behind the login may approve the request only where they may
approve their own change. A change of rights always needs another person.
When MEHO knows nobody behind the login, only an Admin may approve.

Every surface gives the same answer: the approve itself
(:func:`~meho_backplane.operations.approval_queue.approve_request`), the
"may I approve?" answer behind the button, the lists, the waiting count and
web push (:mod:`~meho_backplane.operations.approval_rights`), and the policy
evaluator (:mod:`~meho_backplane.policy.approve`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.auth.machine_logins import note_machine_login
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentDefinition,
    AgentPrincipal,
    ApprovalRequest,
    AuditLog,
    RunbookRun,
    RunnerPrincipal,
    ScheduledTrigger,
    ServicePrincipalGrant,
)
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    ApprovalRule,
    RightsChangeApprovalError,
    SelfApprovalForbiddenError,
    UnknownRequesterApprovalError,
    approve_request,
    create_pending_request,
    record_approval_step_event,
    requester_of,
)
from meho_backplane.operations.approval_rights import (
    ApproveRightReason,
    approve_right,
    approve_right_any_status,
)
from meho_backplane.policy.approve import may_approve
from meho_backplane.policy.shadow import approval_ask_of, asker_of
from meho_backplane.settings import get_settings
from meho_backplane.web_push.send import _snapshot as push_snapshot

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000b0101")
_AGENT_SUB = "agent-sa-sub"
_AGENT_CLIENT = "agent:nightly"
_SETTER_UP = "person-who-registered"
_OTHER = "person-not-behind"
_PARAMS = {"name": "x"}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("APPROVAL_ALLOW_SELF_APPROVAL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _login(
    sub: str,
    *,
    kind: PrincipalKind = PrincipalKind.USER,
    role: TenantRole = TenantRole.OPERATOR,
    client_id: str | None = None,
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=kind,
        client_id=client_id,
    )


def _agent() -> Operator:
    return _login(_AGENT_SUB, kind=PrincipalKind.AGENT, client_id=_AGENT_CLIENT)


async def _add(*rows: object) -> None:
    async with get_sessionmaker()() as session:
        session.add_all(rows)
        await session.commit()


async def _register_agent(by: str = _SETTER_UP) -> None:
    await _add(
        AgentPrincipal(
            tenant_id=_TENANT,
            name="nightly",
            keycloak_client_id=_AGENT_CLIENT,
            keycloak_internal_id="kc-nightly",
            owner_sub=by,
            created_by_sub=by,
        )
    )


async def _park(requester: Operator, *, level: str = "caution", rights: bool = False) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=requester,
            connector_id="x-1.0",
            op_id="x.write",
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            proposed_effect={"op_id": "x.write", "safety_level": level},
            rights_change=rights,
        )
        await session.commit()
        return request.id


async def _load(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        session.expunge(row)
        return row


async def _approve(request_id: uuid.UUID, approver: Operator) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        decided = await approve_request(session, request_id, operator=approver, params=_PARAMS)
        await session.commit()
        return decided


async def _surfaces_agree(request_id: uuid.UUID, approver: Operator) -> bool:
    """The "may I approve?" answers, web push and the policy evaluator; they must agree."""
    row = await _load(request_id)
    right = approve_right(approver, row)
    assert approve_right_any_status(approver, row).may_approve == right.may_approve
    push_copy = push_snapshot(_TENANT, row)
    assert approve_right_any_status(approver, push_copy).may_approve == right.may_approve
    allowed, _ = may_approve(asker_of(approver), approval_ask_of(approver, row))
    assert allowed == right.may_approve
    return right.may_approve


# --- Agent with its own login -------------------------------------------------------


async def test_the_person_who_registered_the_agent_cannot_approve_as_an_operator() -> None:
    await _register_agent()
    request_id = await _park(_agent())
    setter_up = _login(_SETTER_UP)

    assert not await _surfaces_agree(request_id, setter_up)
    assert approve_right(setter_up, await _load(request_id)).reason is (
        ApproveRightReason.OWN_CHANGE
    )
    with pytest.raises(SelfApprovalForbiddenError) as excinfo:
        await _approve(request_id, setter_up)
    assert excinfo.value.behind is True
    assert "counts as your own change" in excinfo.value.reason_text
    assert "APPROVAL_ALLOW_SELF_APPROVAL" not in excinfo.value.reason_text


async def test_an_admin_behind_the_agent_approves_it_as_an_own_change() -> None:
    await _register_agent()
    request_id = await _park(_agent())
    admin = _login(_SETTER_UP, role=TenantRole.TENANT_ADMIN)

    assert await _surfaces_agree(request_id, admin)
    decided = await _approve(request_id, admin)

    assert decided.status == "approved"
    assert decided._approval_rule == ApprovalRule.SA_OPERATOR_SELF_APPROVAL  # type: ignore[attr-defined]
    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(select(AuditLog).where(AuditLog.path == "approval.decision"))
        ).all()
    (row,) = rows
    assert row.payload["self_approved"] is True
    assert row.payload["approver_behind_requester"] is True
    assert row.payload["approver_behind_why"] == ["registered", "owns"]

    target = (
        "meho_backplane.operations.addon_step_events."
        "AddonStepEventService.record_if_owned_committed"
    )
    with patch(target, new_callable=AsyncMock) as record:
        await record_approval_step_event(
            tenant_id=_TENANT, request=decided, decision="approved", audit_id=uuid.uuid4()
        )
    step = record.await_args.kwargs["payload"]
    assert step["self_approved"] is True
    assert "approver_behind_requester" not in step
    assert "approver_behind_why" not in step


async def test_a_person_not_behind_the_agent_approves_as_today() -> None:
    await _register_agent()
    request_id = await _park(_agent())
    other = _login(_OTHER)

    assert await _surfaces_agree(request_id, other)
    decided = await _approve(request_id, other)

    assert decided.status == "approved"
    async with get_sessionmaker()() as session:
        (row,) = (
            await session.scalars(select(AuditLog).where(AuditLog.path == "approval.decision"))
        ).all()
    assert row.payload["self_approved"] is False
    assert "approver_behind_requester" not in row.payload


async def test_a_rights_change_of_an_agent_needs_an_admin_who_is_not_behind_it() -> None:
    await _register_agent()
    request_id = await _park(_agent(), rights=True)
    admin_behind = _login(_SETTER_UP, role=TenantRole.TENANT_ADMIN)
    other_admin = _login(_OTHER, role=TenantRole.TENANT_ADMIN)

    assert not await _surfaces_agree(request_id, admin_behind)
    with pytest.raises(RightsChangeApprovalError):
        await _approve(request_id, admin_behind)
    assert await _surfaces_agree(request_id, other_admin)
    assert (await _approve(request_id, other_admin)).status == "approved"


# --- Nobody known behind the login ----------------------------------------------------


async def test_only_an_admin_approves_when_nobody_is_known_behind_the_agent() -> None:
    request_id = await _park(_agent())
    row = await _load(request_id)
    assert row.people_behind == []
    operator = _login(_OTHER)
    admin = _login("some-admin", role=TenantRole.TENANT_ADMIN)

    assert not await _surfaces_agree(request_id, operator)
    with pytest.raises(UnknownRequesterApprovalError):
        await _approve(request_id, operator)
    assert await _surfaces_agree(request_id, admin)
    assert (await _approve(request_id, admin)).status == "approved"


@pytest.mark.parametrize("known_as", ["signed_in", "agent_login"])
async def test_only_an_admin_approves_when_only_a_machine_login_set_up_the_agent(
    known_as: str,
) -> None:
    machine = _login("machine-admin-sa", kind=PrincipalKind.AGENT, role=TenantRole.TENANT_ADMIN)
    definition_id = uuid.uuid4()
    rows: list[object] = [
        AgentDefinition(
            id=definition_id,
            tenant_id=_TENANT,
            name="made-by-a-machine",
            identity_ref=_AGENT_CLIENT,
            model_tier="standard",
            system_prompt="p",
            toolset={},
            turn_budget=5,
            created_by_sub=machine.sub,
            updated_by_sub=machine.sub,
        ),
        ScheduledTrigger(
            tenant_id=_TENANT,
            agent_definition_id=definition_id,
            kind="cron",
            cron_expr="*/5 * * * *",
            status="active",
            identity_sub=_AGENT_SUB,
            created_by_sub=machine.sub,
        ),
    ]
    if known_as == "agent_login":
        rows.append(
            AgentDefinition(
                tenant_id=_TENANT,
                name="the-machine-admin",
                identity_ref=machine.sub,
                model_tier="standard",
                system_prompt="p",
                toolset={},
                turn_budget=5,
                created_by_sub="someone",
            )
        )
    await _add(*rows)
    if known_as == "signed_in":
        async with get_sessionmaker()() as session:
            await note_machine_login(session, machine)
            await session.commit()

    request_id = await _park(_agent())
    assert (await _load(request_id)).people_behind == []
    operator = _login(_OTHER)
    admin = _login("some-admin", role=TenantRole.TENANT_ADMIN)

    assert not await _surfaces_agree(request_id, operator)
    with pytest.raises(UnknownRequesterApprovalError):
        await _approve(request_id, operator)
    assert await _surfaces_agree(request_id, admin)
    assert (await _approve(request_id, admin)).status == "approved"


async def test_only_an_admin_approves_when_a_hosted_scheduled_run_handed_over_the_runbook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hosted_sub = "hosted-agent-sa"
    hosted_client = "agent:hosted"
    await _add(
        AgentDefinition(
            tenant_id=_TENANT,
            name="hosted",
            identity_ref=hosted_client,
            model_tier="standard",
            system_prompt="p",
            toolset={},
            turn_budget=5,
            created_by_sub="someone",
        )
    )
    hosted = _login(hosted_sub, kind=PrincipalKind.AGENT, client_id=hosted_client)
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.get_client_credentials_token",
        AsyncMock(return_value="agent-token"),
    )
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.verify_jwt_for_audience",
        AsyncMock(return_value=hosted),
    )
    signed_in, _ = await AgentInvoker()._authenticate_scheduled_agent(
        "hosted",
        agent_client_id=hosted_client,
        agent_client_secret=SecretStr("test-secret"),
        settings=get_settings(),
    )
    assert signed_in.sub == hosted_sub
    await _add(
        RunbookRun(
            tenant_id=_TENANT,
            template_slug="patch",
            template_version=1,
            assigned_to=_AGENT_SUB,
            target="host-1",
            params={},
            started_by=hosted_sub,
            reassigned_by=hosted_sub,
        )
    )

    request_id = await _park(_agent())
    assert (await _load(request_id)).people_behind == []
    operator = _login(_OTHER)
    admin = _login("some-admin", role=TenantRole.TENANT_ADMIN)

    assert not await _surfaces_agree(request_id, operator)
    with pytest.raises(UnknownRequesterApprovalError):
        await _approve(request_id, operator)
    assert await _surfaces_agree(request_id, admin)
    assert (await _approve(request_id, admin)).status == "approved"


# --- Other ways an agent gets people behind it -----------------------------------------


async def test_a_scheduled_agent_counts_the_schedule_and_definition_people() -> None:
    definition_id = uuid.uuid4()
    now = datetime.now(UTC)
    await _add(
        AgentDefinition(
            id=definition_id,
            tenant_id=_TENANT,
            name="nightly-check",
            identity_ref=_AGENT_CLIENT,
            model_tier="standard",
            system_prompt="p",
            toolset={},
            turn_budget=5,
            created_by_sub="definition-author",
            updated_by_sub="definition-editor",
        )
    )
    await _add(
        ScheduledTrigger(
            tenant_id=_TENANT,
            agent_definition_id=definition_id,
            kind="cron",
            cron_expr="0 * * * *",
            status="active",
            identity_sub=_AGENT_SUB,
            created_by_sub="schedule-maker",
            created_at=now,
            updated_at=now,
        )
    )
    request_id = await _park(_agent())

    for person in ("definition-author", "definition-editor", "schedule-maker"):
        assert not await _surfaces_agree(request_id, _login(person)), person
    assert await _surfaces_agree(request_id, _login(_OTHER))


async def test_a_runbook_step_counts_who_started_and_handed_over_the_run() -> None:
    await _add(
        RunbookRun(
            tenant_id=_TENANT,
            template_slug="patch",
            template_version=1,
            assigned_to=_AGENT_SUB,
            target="host-1",
            params={},
            started_by="run-starter",
            reassigned_by="run-handover",
        )
    )
    request_id = await _park(_agent())

    for person in ("run-starter", "run-handover"):
        assert not await _surfaces_agree(request_id, _login(person)), person


async def test_a_service_login_counts_the_giver_of_its_standing_grant() -> None:
    await _add(
        ServicePrincipalGrant(
            tenant_id=_TENANT,
            principal_sub="svc-sub",
            op_id="x.create",
            connector_id="x-1.0",
            reason="build",
            created_by_sub="grant-giver",
        )
    )
    request_id = await _park(_login("svc-sub", kind=PrincipalKind.SERVICE))

    assert not await _surfaces_agree(request_id, _login("grant-giver"))
    assert await _surfaces_agree(request_id, _login(_OTHER))


async def test_a_runner_login_counts_its_owner() -> None:
    await _add(
        RunnerPrincipal(
            tenant_id=_TENANT,
            name="edge",
            keycloak_client_id="runner:edge",
            keycloak_internal_id="kc-edge",
            owner_sub="runner-owner",
            created_by_sub="runner-owner",
        )
    )
    runner = _login("runner-sa", kind=PrincipalKind.RUNNER, client_id="runner:edge")
    request_id = await _park(runner)

    assert not await _surfaces_agree(request_id, _login("runner-owner"))


# --- A person's own request ------------------------------------------------------------


async def test_a_person_or_delegated_request_keeps_todays_rule() -> None:
    await _register_agent()
    request_id = await _park(_login(_SETTER_UP))
    row = await _load(request_id)

    assert row.people_behind is None
    with pytest.raises(SelfApprovalForbiddenError) as excinfo:
        await _approve(request_id, _login(_SETTER_UP))
    assert excinfo.value.behind is False
    assert await _surfaces_agree(request_id, _login(_OTHER))


# --- A step that waits again ------------------------------------------------------------


async def test_a_step_that_waits_again_keeps_the_people_of_the_first_request() -> None:
    await _register_agent()
    first = await _load(await _park(_agent()))
    reviewer = _login(_OTHER)
    async with get_sessionmaker()() as session:
        second = await create_pending_request(
            session,
            operator=reviewer,
            connector_id="x-1.0",
            op_id="x.step",
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            proposed_effect={"op_id": "x.step", "safety_level": "caution"},
            requester=requester_of(first),
        )
        await session.commit()
        second_id = second.id

    assert (await _load(second_id)).people_behind == first.people_behind
    assert not await _surfaces_agree(second_id, _login(_SETTER_UP))

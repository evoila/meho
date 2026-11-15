# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A runbook ``operation_call`` check runs with the identity of the caller who advances the run.

The caller passed to :meth:`RunbookRunService.next_step` is the identity
the check call runs as: its principal kind, client id, role and token.
So:

* an agent's check is checked against the agent's grants (no grant with
  ``AGENT_GRANT_REQUIRED=on`` and a ``deny`` grant both refuse it; a
  matching grant lets it run);
* a service login's check is checked against its read scope;
* a person's check runs as before;
* the audit row of the check names the real kind and the agent's name;
* a check call that does not return ``ok`` fails the step with the
  call's text, whatever the step's ``expect`` says;
* without a caller, or with a caller who is not the assignee, the step
  is refused and its state does not change.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    AgentPrincipal,
    AuditLog,
    PermissionVerdict,
    RunbookRunStepState,
    Target,
    Tenant,
)
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.service_read_scope_schemas import (
    ReadScopeEntry,
    ServiceReadScopeSet,
)
from meho_backplane.operations.service_read_scopes import ServiceReadScopeService
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.runbooks.run_service import (
    NotRunAssigneeError,
    PreviousStepFailedError,
    RunbookRunService,
    StepCallerRequiredError,
)
from meho_backplane.runbooks.runs_schemas import (
    CurrentStepResponse,
    NextStepRequest,
    RunCompletedResponse,
    StartRunRequest,
)
from meho_backplane.runbooks.schemas import RunbookTemplateBody
from meho_backplane.settings import get_settings
from tests.test_runbooks_run_service import (
    _op_call_step,
    _seed_published_template,
    _seed_stub_op,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000012a3")
_OP = "stub.op_call"
_AGENT_SUB = "aaaaaaaa-0000-0000-0000-000000000001"
_AGENT_CLIENT = "agent:runbook-bot"
_AGENT_NAME = "runbook-bot"
_SERVICE_SUB = "svc:runbook-reader"
_PERSON_SUB = "person-alpha"
_TARGET = "node-1"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("AUDIT_CLIENT_IDENTITY", "true")
    monkeypatch.delenv("AGENT_GRANT_REQUIRED", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_dispatcher_state() -> Iterator[None]:
    clear_registry()
    reset_dispatcher_caches()
    yield
    clear_registry()
    reset_dispatcher_caches()


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


def _switch(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("AGENT_GRANT_REQUIRED", mode)
    get_settings.cache_clear()


def _agent() -> Operator:
    return Operator(
        sub=_AGENT_SUB,
        name=None,
        email=None,
        raw_jwt="<test-agent-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
        client_id=_AGENT_CLIENT,
        azp=_AGENT_CLIENT,
    )


def _service() -> Operator:
    return Operator(
        sub=_SERVICE_SUB,
        name="svc",
        email=None,
        raw_jwt="<test-service-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
        client_id="runbook-reader",
    )


def _person(sub: str = _PERSON_SUB) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


def _template(expect: dict[str, Any] | None = None) -> RunbookTemplateBody:
    return RunbookTemplateBody(
        title="Check procedure",
        description="one operation_call check",
        target_kind="k8s-node",
        steps=[_op_call_step("call-it", op_id=_OP, expect=expect)],
    )


async def _seed_tenant_and_target() -> uuid.UUID:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT, slug="t-runbook-caller", name="Runbook caller"))
        target = Target(
            id=uuid.uuid4(),
            tenant_id=_TENANT,
            name=_TARGET,
            product="stub",
            host="stub-host.example",
            auth_model="shared_service_account",
        )
        session.add(target)
        session.add(
            AgentPrincipal(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                name=_AGENT_NAME,
                keycloak_client_id=_AGENT_CLIENT,
                keycloak_internal_id="kc-runbook-bot",
                owner_sub="op-admin",
                revoked=False,
                created_by_sub="op-admin",
            )
        )
        await session.commit()
        return target.id


async def _grant(verdict: str, target_scope: str | None = None) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub=_AGENT_CLIENT,
                op_pattern=_OP,
                verdict=verdict,
                target_scope=target_scope,
                created_by_sub="op-admin",
            )
        )
        await session.commit()


async def _start(
    caller: Operator,
    stub_embedding_service: AsyncMock,
    *,
    expect: dict[str, Any] | None = None,
) -> tuple[RunbookRunService, uuid.UUID, uuid.UUID]:
    """Seed the op, tenant, target and template; start a run as *caller*."""
    await _seed_stub_op(stub_embedding_service, op_id=_OP)
    target_id = await _seed_tenant_and_target()
    await _seed_published_template(_TENANT, "check", body=_template(expect))
    service = RunbookRunService()
    start = await service.start_run(
        _TENANT,
        caller.sub,
        StartRunRequest(template_slug="check", target=_TARGET, params={}),
    )
    assert isinstance(start, CurrentStepResponse)
    return service, start.run_id, target_id


async def _advance(service: RunbookRunService, caller: Operator, run_id: uuid.UUID) -> Any:
    return await service.next_step(
        _TENANT,
        caller.sub,
        run_id,
        NextStepRequest(last_verified=True, verify_response=None),
        caller=caller,
    )


async def _step_state(session: AsyncSession, run_id: uuid.UUID) -> RunbookRunStepState:
    row = (
        await session.scalars(
            select(RunbookRunStepState).where(RunbookRunStepState.run_id == run_id)
        )
    ).one()
    await session.refresh(row)
    return row


async def _step_audit_rows(session: AsyncSession) -> list[AuditLog]:
    return list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.method == "DISPATCH", AuditLog.path == _OP)
            )
        ).all()
    )


@pytest.mark.asyncio
async def test_agent_without_grant_is_refused_and_step_fails(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switch on, no grant: the check is refused like a direct call, with a clear text."""
    _switch(monkeypatch, "on")
    agent = _agent()
    service, run_id, _ = await _start(agent, stub_embedding_service)

    with pytest.raises(PreviousStepFailedError, match="check call did not run: denied"):
        await _advance(service, agent, run_id)

    row = await _step_state(db_session, run_id)
    assert row.state == "failed"
    assert row.verify_response is not None
    assert row.verify_response["matched"] is False
    assert row.verify_response["actual"]["status"] == "denied"
    rows = await _step_audit_rows(db_session)
    assert [r.payload["result_status"] for r in rows] == ["denied"]


@pytest.mark.asyncio
async def test_refused_check_fails_even_with_empty_expect(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused call never passes the step, also when ``expect`` is empty."""
    _switch(monkeypatch, "on")
    agent = _agent()
    service, run_id, _ = await _start(agent, stub_embedding_service, expect={})

    with pytest.raises(PreviousStepFailedError, match="check call did not run"):
        await _advance(service, agent, run_id)

    assert (await _step_state(db_session, run_id)).state == "failed"


@pytest.mark.asyncio
async def test_agent_with_deny_grant_is_refused(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """Switch off, an explicit deny grant: the check is refused."""
    agent = _agent()
    service, run_id, _ = await _start(agent, stub_embedding_service)
    await _grant(PermissionVerdict.DENY.value)

    with pytest.raises(PreviousStepFailedError, match="check call did not run: denied"):
        await _advance(service, agent, run_id)

    assert (await _step_state(db_session, run_id)).state == "failed"


@pytest.mark.asyncio
async def test_agent_with_grant_for_op_and_target_runs(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switch on, a grant for the operation and the target: the check runs."""
    _switch(monkeypatch, "on")
    agent = _agent()
    service, run_id, target_id = await _start(agent, stub_embedding_service)
    await _grant(PermissionVerdict.AUTO_EXECUTE.value, target_scope=str(target_id))

    resp = await _advance(service, agent, run_id)

    assert isinstance(resp, RunCompletedResponse)
    assert (await _step_state(db_session, run_id)).state == "verified"


@pytest.mark.asyncio
async def test_agent_step_audit_row_names_agent(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit row of an agent's check says kind ``agent`` and the agent's name."""
    _switch(monkeypatch, "on")
    agent = _agent()
    service, run_id, target_id = await _start(agent, stub_embedding_service)
    await _grant(PermissionVerdict.AUTO_EXECUTE.value, target_scope=str(target_id))

    await _advance(service, agent, run_id)

    rows = await _step_audit_rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.operator_sub == _AGENT_SUB
    assert row.payload["principal_name"] == _AGENT_NAME
    assert row.payload["client"]["principal_kind"] == "agent"
    assert row.payload["client"]["azp"] == _AGENT_CLIENT
    assert row.payload["step_id"] == "call-it" or row.step_id == "call-it"


@pytest.mark.asyncio
async def test_service_login_outside_read_scope_is_refused(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """A service login whose read scope does not cover the target: refused."""
    svc = _service()
    service, run_id, _ = await _start(svc, stub_embedding_service)
    await ServiceReadScopeService().set(
        _TENANT,
        _SERVICE_SUB,
        "op-admin",
        ServiceReadScopeSet(entries=[ReadScopeEntry(target_id=uuid.uuid4())]),
    )

    with pytest.raises(PreviousStepFailedError, match="read_scope_denied"):
        await _advance(service, svc, run_id)

    assert (await _step_state(db_session, run_id)).state == "failed"


@pytest.mark.asyncio
async def test_person_step_runs_as_before(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """A person's check runs at once, and its audit row says kind ``user``."""
    person = _person()
    service, run_id, _ = await _start(person, stub_embedding_service)

    resp = await _advance(service, person, run_id)

    assert isinstance(resp, RunCompletedResponse)
    rows = await _step_audit_rows(db_session)
    assert len(rows) == 1
    assert rows[0].operator_sub == _PERSON_SUB
    assert rows[0].payload["client"]["principal_kind"] == "user"


@pytest.mark.asyncio
async def test_step_without_caller_is_refused_and_state_kept(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """No caller identity: the check does not run and the step stays in progress."""
    person = _person()
    service, run_id, _ = await _start(person, stub_embedding_service)

    with pytest.raises(StepCallerRequiredError):
        await service.next_step(_TENANT, person.sub, run_id, NextStepRequest(last_verified=True))

    assert (await _step_state(db_session, run_id)).state == "in_progress"
    assert await _step_audit_rows(db_session) == []


@pytest.mark.asyncio
async def test_caller_who_is_not_the_assignee_is_refused(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """A caller identity that differs from the advancing caller is refused before any call."""
    person = _person()
    service, run_id, _ = await _start(person, stub_embedding_service)

    with pytest.raises(NotRunAssigneeError):
        await service.next_step(
            _TENANT,
            person.sub,
            run_id,
            NextStepRequest(last_verified=True),
            caller=_person("person-beta"),
        )

    assert (await _step_state(db_session, run_id)).state == "in_progress"
    assert await _step_audit_rows(db_session) == []

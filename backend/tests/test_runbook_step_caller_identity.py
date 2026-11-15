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
* the check follows the token rule on every path, also from the web
  console: for a person only a ``safe`` check runs at once, a ``caution``
  or higher check waits for approval;
* a ``credential_read`` check never runs at once;
* the audit row and the approval request of the check name the real
  caller;
* the audit row of the check names the real kind and the agent's name;
* a check call that does not return ``ok`` fails the step with the
  call's text, whatever the step's ``expect`` says;
* without a caller, or with a caller who is not the assignee (another
  sub or another tenant), the step is refused and its state does not
  change.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    AgentPrincipal,
    ApprovalRequest,
    AuditLog,
    PermissionVerdict,
    RunbookRunStepState,
    Target,
    Tenant,
)
from meho_backplane.op_classes import (
    _reset_declared_op_classes_for_testing,
    declare_op_classes,
)
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.service_read_scope_schemas import (
    ReadScopeEntry,
    ServiceReadScopeSet,
)
from meho_backplane.operations.service_read_scopes import ServiceReadScopeService
from meho_backplane.operations.typed_register import register_typed_operation
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
    _StubConnector,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000012a3")
_OP = "stub.op_call"
_AGENT_SUB = "aaaaaaaa-0000-0000-0000-000000000001"
_AGENT_CLIENT = "agent:runbook-bot"
_AGENT_NAME = "runbook-bot"
_SERVICE_SUB = "svc:runbook-reader"
_PERSON_SUB = "person-alpha"
_TARGET = "node-1"
_SECRET_OP = "stub.secret.read"

_Level = Literal["safe", "caution", "dangerous", "destructive"]

#: Each call of :func:`_counting_handler` appends here, so a test can tell
#: whether the handler ran.
_HANDLER_CALLS: list[str] = []


async def _counting_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Typed handler: note the call and return ``{"ok": True}``."""
    _HANDLER_CALLS.append(operator.sub)
    return {"ok": True}


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
    _HANDLER_CALLS.clear()
    yield
    clear_registry()
    reset_dispatcher_caches()
    _HANDLER_CALLS.clear()


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


def _person(
    sub: str = _PERSON_SUB,
    *,
    web_session: bool = False,
    tenant_id: uuid.UUID = _TENANT,
) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="" if web_session else "<test-person-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        web_session=web_session,
    )


def _template(expect: dict[str, Any] | None = None, op_id: str = _OP) -> RunbookTemplateBody:
    return RunbookTemplateBody(
        title="Check procedure",
        description="one operation_call check",
        target_kind="k8s-node",
        steps=[_op_call_step("call-it", op_id=op_id, expect=expect)],
    )


async def _seed_op(
    stub_embedding_service: AsyncMock,
    *,
    op_id: str = _OP,
    safety_level: _Level = "safe",
) -> None:
    """Register the stub connector and one typed op with *safety_level*."""
    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    await register_typed_operation(
        product="stub",
        version="1.x",
        impl_id="stub",
        op_id=op_id,
        handler=_counting_handler,
        summary="Stub op for runbook caller identity tests.",
        description="Echo OK and note the call.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level=safety_level,
        embedding_service=stub_embedding_service,
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


async def _grant(verdict: str, target_scope: str | None = None, op_pattern: str = _OP) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub=_AGENT_CLIENT,
                op_pattern=op_pattern,
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
    op_id: str = _OP,
    safety_level: _Level = "safe",
) -> tuple[RunbookRunService, uuid.UUID, uuid.UUID]:
    """Seed the op, tenant, target and template; start a run as *caller*."""
    await _seed_op(stub_embedding_service, op_id=op_id, safety_level=safety_level)
    target_id = await _seed_tenant_and_target()
    await _seed_published_template(_TENANT, "check", body=_template(expect, op_id))
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


async def _set_read_scope(target_id: uuid.UUID) -> None:
    await ServiceReadScopeService().set(
        _TENANT,
        _SERVICE_SUB,
        "op-admin",
        ServiceReadScopeSet(entries=[ReadScopeEntry(target_id=target_id)]),
    )


@pytest.fixture
def _secret_op_class() -> Iterator[None]:
    """Declare :data:`_SECRET_OP` as a ``credential_read`` operation."""
    declare_op_classes(credential_read=[_SECRET_OP])
    yield
    _reset_declared_op_classes_for_testing()


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
    assert row.step_id == "call-it"


@pytest.mark.asyncio
async def test_service_login_outside_read_scope_is_refused(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """A service login whose read scope does not cover the target: refused."""
    svc = _service()
    service, run_id, _ = await _start(svc, stub_embedding_service)
    await _set_read_scope(uuid.uuid4())

    with pytest.raises(PreviousStepFailedError, match="read_scope_denied"):
        await _advance(service, svc, run_id)

    assert (await _step_state(db_session, run_id)).state == "failed"


@pytest.mark.asyncio
async def test_person_safe_check_runs_at_once(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """Person: a ``safe`` check runs at once, and its audit row says kind ``user``."""
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


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["caution", "dangerous"])
async def test_web_console_person_check_above_safe_waits(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
    level: _Level,
) -> None:
    """Person in the web console: a ``caution`` or ``dangerous`` check waits.

    The handler does not run.
    """
    person = _person(web_session=True)
    service, run_id, _ = await _start(person, stub_embedding_service, safety_level=level)

    with pytest.raises(PreviousStepFailedError, match="check call did not run: awaiting_approval"):
        await _advance(service, person, run_id)

    assert _HANDLER_CALLS == []
    row = await _step_state(db_session, run_id)
    assert row.state == "failed"
    assert row.verify_response is not None
    assert row.verify_response["actual"]["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_web_console_person_safe_check_runs(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """Person in the web console: a ``safe`` check runs at once."""
    person = _person(web_session=True)
    service, run_id, _ = await _start(person, stub_embedding_service)

    resp = await _advance(service, person, run_id)

    assert isinstance(resp, RunCompletedResponse)
    assert _HANDLER_CALLS == [_PERSON_SUB]
    assert (await _step_state(db_session, run_id)).state == "verified"


@pytest.mark.asyncio
async def test_token_person_caution_check_waits(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """Person with a token: a ``caution`` check waits; the handler does not run."""
    person = _person()
    service, run_id, _ = await _start(person, stub_embedding_service, safety_level="caution")

    with pytest.raises(PreviousStepFailedError, match="check call did not run: awaiting_approval"):
        await _advance(service, person, run_id)

    assert _HANDLER_CALLS == []
    assert (await _step_state(db_session, run_id)).state == "failed"


@pytest.mark.asyncio
async def test_agent_with_grant_for_other_target_only_is_refused(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switch on, a grant for another target only: the check is refused."""
    _switch(monkeypatch, "on")
    agent = _agent()
    service, run_id, _ = await _start(agent, stub_embedding_service)
    await _grant(PermissionVerdict.AUTO_EXECUTE.value, target_scope=str(uuid.uuid4()))

    with pytest.raises(PreviousStepFailedError, match="check call did not run"):
        await _advance(service, agent, run_id)

    assert _HANDLER_CALLS == []
    assert (await _step_state(db_session, run_id)).state == "failed"


@pytest.mark.asyncio
async def test_service_login_inside_read_scope_runs(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """A service login whose read scope covers the target: the check runs."""
    svc = _service()
    service, run_id, target_id = await _start(svc, stub_embedding_service)
    await _set_read_scope(target_id)

    resp = await _advance(service, svc, run_id)

    assert isinstance(resp, RunCompletedResponse)
    assert _HANDLER_CALLS == [_SERVICE_SUB]
    assert (await _step_state(db_session, run_id)).state == "verified"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_secret_op_class")
@pytest.mark.parametrize("kind", ["person", "agent", "service"])
async def test_credential_read_check_never_runs_at_once(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """A ``credential_read`` check waits for a person, an agent with a grant and a service login."""
    callers = {"person": _person(), "agent": _agent(), "service": _service()}
    caller = callers[kind]
    if kind == "agent":
        _switch(monkeypatch, "on")
    service, run_id, target_id = await _start(caller, stub_embedding_service, op_id=_SECRET_OP)
    if kind == "agent":
        await _grant(
            PermissionVerdict.AUTO_EXECUTE.value,
            target_scope=str(target_id),
            op_pattern=_SECRET_OP,
        )
    if kind == "service":
        await _set_read_scope(target_id)

    with pytest.raises(PreviousStepFailedError, match="check call did not run: awaiting_approval"):
        await _advance(service, caller, run_id)

    assert _HANDLER_CALLS == []
    assert (await _step_state(db_session, run_id)).state == "failed"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_secret_op_class")
async def test_waiting_check_approval_request_names_real_caller(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The approval request of a waiting agent check says kind ``agent`` and the agent's name."""
    _switch(monkeypatch, "on")
    agent = _agent()
    service, run_id, target_id = await _start(agent, stub_embedding_service, op_id=_SECRET_OP)
    await _grant(
        PermissionVerdict.AUTO_EXECUTE.value, target_scope=str(target_id), op_pattern=_SECRET_OP
    )

    with pytest.raises(PreviousStepFailedError, match="awaiting_approval"):
        await _advance(service, agent, run_id)

    requests = list(
        (
            await db_session.scalars(
                select(ApprovalRequest).where(ApprovalRequest.op_id == _SECRET_OP)
            )
        ).all()
    )
    assert len(requests) == 1
    request = requests[0]
    assert request.principal_sub == _AGENT_SUB
    assert request.principal_kind == "agent"
    assert request.principal_name == _AGENT_NAME


@pytest.mark.asyncio
async def test_caller_from_another_tenant_is_refused(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """A caller with the assignee's sub but another tenant is refused before any call."""
    person = _person()
    service, run_id, _ = await _start(person, stub_embedding_service)

    with pytest.raises(NotRunAssigneeError):
        await service.next_step(
            _TENANT,
            person.sub,
            run_id,
            NextStepRequest(last_verified=True),
            caller=_person(tenant_id=uuid.uuid4()),
        )

    assert _HANDLER_CALLS == []
    assert (await _step_state(db_session, run_id)).state == "in_progress"
    assert await _step_audit_rows(db_session) == []


@pytest.mark.asyncio
async def test_failed_step_message_keeps_the_reason(
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stored text of a failed step is the text that the advance raised."""
    _switch(monkeypatch, "on")
    agent = _agent()
    service, run_id, _ = await _start(agent, stub_embedding_service)
    assert await service.get_failed_step_message(_TENANT, run_id) is None

    with pytest.raises(PreviousStepFailedError) as raised:
        await _advance(service, agent, run_id)

    message = await service.get_failed_step_message(_TENANT, run_id)
    assert message == str(raised.value)
    assert "check call did not run: denied" in message

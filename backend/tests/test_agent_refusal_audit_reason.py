# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit log says why a call in an agent run was refused, and who made it (#5773).

* A refused call's audit row keeps a reason code and a short text under
  ``payload["error"]``. A call refused by the agent's grant and a call
  refused by the person's role get different codes. So does a call the
  agent's connector list refuses before the dispatcher runs.
* The audit answers (query, show by id, session replay) show the agent
  that acted (``actor_sub``) and its name (``actor_name``).
* Inside an agent run, the status answer shows no tenant role higher than
  the run's own role.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.agent.tool_audit import ToolRefusedRetry, write_agent_tool_row
from meho_backplane.api.v1.health import _operator_identity
from meho_backplane.audit_query import AuditQueryFilters, query_audit
from meho_backplane.audit_query.replay import replay_session
from meho_backplane.auth.delegation import actor_delegation, delegated_operator
from meho_backplane.auth.operator import Operator, RoleGroup, TenantMembership, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, AuditEventClass, AuditLog
from meho_backplane.operations import reset_dispatcher_caches, role_floors
from meho_backplane.operations.deny_reasons import (
    AGENT_GRANT_DENIED,
    CONNECTOR_NOT_ALLOWED,
    POLICY_DENIED,
    READ_SCOPE_DENIED,
    ROLE_INSUFFICIENT,
    deny_reason_code,
)
from meho_backplane.settings import get_settings
from tests.test_agent_invocation import (
    _TENANT_A,
    _make_operator,
    _seed_definition,
    _seed_echo_op,
)

_OP_ID = "vault.kv.read"
_AGENT = "agent:reader"
_CALL = ToolCallPart(
    "call_operation",
    {"connector_id": "vault-1.x", "op_id": _OP_ID, "params": {"path": "secret/foo"}},
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    clear_registry()
    reset_dispatcher_caches()
    yield
    clear_registry()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION

    service = AsyncMock()
    service.encode_one.return_value = [0.1] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.1] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


@pytest.fixture
def admin_floor_on_op() -> Iterator[None]:
    """The test op needs the ``tenant_admin`` role while the test runs."""
    role_floors.register_role_floor(_OP_ID, TenantRole.TENANT_ADMIN)
    yield
    role_floors._FLOORS.pop(_OP_ID, None)


def _scripted(*steps: ToolCallPart) -> FunctionModel:
    remaining = list(steps)

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if remaining:
            return ModelResponse(parts=[remaining.pop(0)])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(fn)


async def _run_call() -> list[AuditLog]:
    """Run the agent ``reader`` for a person; it calls the test op once."""
    invoker = AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: _scripted(_CALL)))
    outcome = await invoker.run(_make_operator(), "reader", "go")
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(AuditLog)
            .where(AuditLog.agent_session_id == outcome.run_id)
            .order_by(AuditLog.occurred_at)
        )
        return list(result.scalars())


def _refusal(rows: list[AuditLog]) -> AuditLog:
    refused = [r for r in rows if r.status_code == 403]
    assert len(refused) == 1, [(r.method, r.path, r.status_code) for r in rows]
    return refused[0]


async def _seed_deny_grant() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AgentPermission(
                tenant_id=_TENANT_A,
                principal_sub=_AGENT,
                op_pattern="*",
                verdict="deny",
                created_by_sub="seed-admin",
            )
        )
        await session.commit()


# --- Part 1: the reason code on the refusal row -------------------------------


@pytest.mark.asyncio
async def test_agent_grant_and_role_refusals_get_different_codes(
    stub_embedding_service: AsyncMock, admin_floor_on_op: None
) -> None:
    """Done when: one call refused by an agent grant, one by a role, two codes."""
    await _seed_definition()
    await _seed_echo_op(stub_embedding_service)

    # The person (operator role) is below the op's tenant_admin floor.
    role_row = _refusal(await _run_call())

    # Now the agent's own grant refuses the call first.
    await _seed_deny_grant()
    grant_row = _refusal(await _run_call())

    for row in (role_row, grant_row):
        assert row.method == "DISPATCH"
        assert row.policy_decision == "deny"
        assert row.actor_sub == _AGENT
    assert role_row.payload["error"]["reason_code"] == ROLE_INSUFFICIENT
    assert "needs the tenant_admin role" in role_row.payload["error"]["reason"]
    assert grant_row.payload["error"]["reason_code"] == AGENT_GRANT_DENIED
    assert "pattern='*'" in grant_row.payload["error"]["reason"]
    assert "agent agent:reader acting for" in grant_row.payload["error"]["reason"]


@pytest.mark.asyncio
async def test_connector_refusal_row_has_code_and_no_arguments() -> None:
    """A call the agent's connector list refuses keeps a code, never the arguments."""
    await _seed_definition(toolset={"connectors": []})

    row = _refusal(await _run_call())

    assert row.method == "AGENT"
    assert row.payload["error"] == {
        "error_code": "denied",
        "reason_code": CONNECTOR_NOT_ALLOWED,
        "reason": "call_operation: the connector is not in this agent's allowed connectors",
    }
    assert "vault-1.x" not in str(row.payload)


@pytest.mark.asyncio
async def test_role_refusal_of_a_tool_keeps_the_code() -> None:
    """The toolset's role re-check stores its code and the roles on the row."""
    await _seed_definition()
    error = ToolRefusedRetry(
        "forbidden",
        reason_code=ROLE_INSUFFICIENT,
        reason="meho_status needs the tenant_admin role; the role in this run is operator",
    )
    audit_id = await write_agent_tool_row(
        operator=_make_operator(),
        tool_name="meho_status",
        args={},
        status_code=403,
        duration_ms=1.0,
        error=error,
    )
    async with get_sessionmaker()() as session:
        row = await session.get(AuditLog, audit_id)
    assert row is not None
    assert row.payload["error"]["reason_code"] == ROLE_INSUFFICIENT
    assert row.payload["error_type"] == "ToolRefusedRetry"


def test_deny_reason_code_reads_each_rule() -> None:
    """Each rule's text gives its own code; other texts give ``policy_denied``."""
    role_floors.register_role_floor("x.write", TenantRole.TENANT_ADMIN)
    try:
        role_text = role_floors.role_floor_denial("x.write", TenantRole.OPERATOR)
    finally:
        role_floors._FLOORS.pop("x.write", None)
    assert role_text is not None
    assert deny_reason_code(role_text) == ROLE_INSUFFICIENT
    assert deny_reason_code("verdict=deny; source=permission row (pattern='*')") == (
        AGENT_GRANT_DENIED
    )
    assert deny_reason_code("agent agent:a acting for alice: verdict=deny; source=x") == (
        AGENT_GRANT_DENIED
    )
    assert deny_reason_code("read_scope_denied: no entry covers it") == READ_SCOPE_DENIED
    assert deny_reason_code("policy denied") == POLICY_DENIED
    assert deny_reason_code("") == POLICY_DENIED


# --- Part 2: the audit answers show the actor ---------------------------------


@pytest.mark.asyncio
async def test_audit_answers_show_the_actor() -> None:
    """Done when: query, show (one row by id) and replay show the actor."""
    session_id = uuid.uuid4()
    rows = [
        AuditLog(
            id=uuid.uuid4(),
            event_class=AuditEventClass.POLICY_DECISION,
            occurred_at=datetime.now(UTC),
            operator_sub="person-5773",
            actor_sub=actor,
            tenant_id=_TENANT_A,
            agent_session_id=session_id,
            method="DISPATCH",
            path=_OP_ID,
            status_code=403,
            payload={"op_id": _OP_ID},
        )
        for actor in (_AGENT, None)
    ]
    await _seed_definition()  # makes the tenant row
    async with get_sessionmaker()() as session:
        session.add_all(rows)
        await session.commit()

    expected = {_AGENT: "reader", None: None}
    async with get_sessionmaker()() as session:
        queried = await query_audit(
            AuditQueryFilters(principal_sub="person-5773"), tenant_id=_TENANT_A, session=session
        )
        shown = await query_audit(
            AuditQueryFilters(audit_id=rows[0].id, limit=1), tenant_id=_TENANT_A, session=session
        )
        replayed = await replay_session(session_id, tenant_id=_TENANT_A, session=session)

    assert {e.actor_sub: e.actor_name for e in queried.rows} == expected
    assert [(e.actor_sub, e.actor_name) for e in shown.rows] == [(_AGENT, "reader")]
    assert {n.actor_sub: n.actor_name for n in replayed} == expected


# --- Part 3: the status answer in an agent run --------------------------------


def _person_with_admin_group() -> Operator:
    return _make_operator(role=TenantRole.TENANT_ADMIN).model_copy(
        update={
            "tenants": (
                TenantMembership(tenant_id=_TENANT_A, name="tenant-a", group=RoleGroup.ADMINS),
                TenantMembership(tenant_id=uuid.uuid4(), name="tenant-c", group=RoleGroup.READERS),
            )
        }
    )


def test_status_in_agent_run_shows_no_role_above_the_run() -> None:
    """Done when: inside an agent run, no tenant shows a higher role than the run's."""
    run_operator = delegated_operator(_person_with_admin_group())
    with actor_delegation(_AGENT):
        identity = _operator_identity(run_operator)

    assert identity.tenant_role is TenantRole.OPERATOR
    assert [t.tenant_role for t in identity.tenants] == [
        TenantRole.OPERATOR,
        TenantRole.READ_ONLY,
    ]


def test_status_outside_an_agent_run_keeps_the_person_roles() -> None:
    identity = _operator_identity(_person_with_admin_group())

    assert identity.tenant_role is TenantRole.TENANT_ADMIN
    assert [t.tenant_role for t in identity.tenants] == [
        TenantRole.TENANT_ADMIN,
        TenantRole.READ_ONLY,
    ]

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every tool call a hosted agent makes leaves an audit row (#4234).

The tests drive real runs through :class:`AgentInvoker` with a scripted
:class:`FunctionModel` (no real LLM) and read ``audit_log`` by
``agent_session_id`` = the run id.

* A succeeding run: the discovery call has its own row; ``call_operation``
  keeps only the dispatcher's row (no second row for one call).
* A failing run that never reaches the dispatcher: the refused call and the
  call that crashed both have rows. A call with bad arguments has one too.
* With ``AUDIT_WRAPPER_ROWS_RETIRED`` on, the rows carry a decision or
  execution class, not ``legacy_request``.
* Audit is fail-closed: if the row cannot be written, the run fails.
* The run row records ``ended_at`` for succeeded, failed and cancelled runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from meho_backplane.agent import tool_audit, toolset
from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentRun as AgentRunRow
from meho_backplane.db.models import AgentRunStatus, AuditEventClass, AuditLog
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.settings import get_settings
from tests.test_agent_invocation import (
    _make_operator,
    _seed_definition,
    _seed_echo_op,
)

pytestmark = pytest.mark.asyncio


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


_GROUPS = ToolCallPart("list_operation_groups", {"connector_id": "vault-1.x"})
_CALL = ToolCallPart(
    "call_operation",
    {"connector_id": "vault-1.x", "op_id": "vault.kv.read", "params": {"path": "secret/foo"}},
)


def _scripted(*steps: ToolCallPart, final: str = "done") -> FunctionModel:
    """A model that makes one tool call per turn from *steps*, then answers *final*."""
    remaining = list(steps)

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if remaining:
            return ModelResponse(parts=[remaining.pop(0)])
        return ModelResponse(parts=[TextPart(final)])

    return FunctionModel(fn)


def _invoker(model: FunctionModel) -> AgentInvoker:
    return AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: model))


async def _rows(run_id: UUID) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(AuditLog)
            .where(AuditLog.agent_session_id == run_id)
            .order_by(AuditLog.occurred_at)
        )
        return list(result.scalars())


async def _run_row(run_id: UUID) -> AgentRunRow:
    async with get_sessionmaker()() as session:
        row = await session.get(AgentRunRow, run_id)
        assert row is not None
        return row


def _agent_rows(rows: list[AuditLog]) -> list[AuditLog]:
    return [r for r in rows if r.method == tool_audit.AGENT_TOOL_AUDIT_METHOD]


async def test_succeeding_run_audits_discovery_call_once_per_call(
    stub_embedding_service: AsyncMock,
) -> None:
    await _seed_definition()
    await _seed_echo_op(stub_embedding_service)
    operator = _make_operator()

    outcome = await _invoker(_scripted(_GROUPS, _CALL)).run(operator, "reader", "go")

    assert outcome.status is AgentRunStatus.SUCCEEDED
    rows = await _rows(outcome.run_id)
    agent_rows = _agent_rows(rows)
    # The discovery call has its own row, keyed by the run id.
    assert [r.path for r in agent_rows] == ["/agent/tools/call/list_operation_groups"]
    groups_row = agent_rows[0]
    assert groups_row.status_code == 200
    assert groups_row.operator_sub == operator.sub
    assert groups_row.event_class == AuditEventClass.LEGACY_REQUEST
    assert groups_row.payload["op_id"] == "list_operation_groups"
    assert groups_row.payload["op_class"] == "read"
    assert len(str(groups_row.payload["params_hash"])) == 64
    # Arguments are hashed, never stored.
    assert "vault-1.x" not in str(groups_row.payload)
    # call_operation keeps only the dispatcher's own row.
    other = [r for r in rows if r not in agent_rows]
    assert len(other) == 1
    assert other[0].event_class == AuditEventClass.OPERATION_EXECUTION

    run = await _run_row(outcome.run_id)
    assert run.ended_at is not None


async def test_failing_run_before_dispatch_still_audits_every_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The agent may reach no connector, so call_operation is refused before
    # the dispatcher runs. The run then fails on a crashing tool.
    await _seed_definition(toolset={"connectors": []})

    async def _boom(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("backend down")

    spec = next(s for s in toolset._META_TOOL_CATALOG if s.name == "list_operation_groups")
    monkeypatch.setattr(spec, "handler", _boom)

    outcome = await _invoker(_scripted(_CALL, _GROUPS)).run(_make_operator(), "reader", "go")

    assert outcome.status is AgentRunStatus.FAILED
    rows = await _rows(outcome.run_id)
    assert all(r.method == tool_audit.AGENT_TOOL_AUDIT_METHOD for r in rows)
    seen = [(r.payload["op_id"], r.status_code, r.payload.get("error_type")) for r in rows]
    assert seen == [
        ("call_operation", 403, "ToolRefusedRetry"),
        ("list_operation_groups", 500, "RuntimeError"),
    ]
    # No error text lands on the row.
    assert all("backend down" not in str(r.payload) for r in rows)

    run = await _run_row(outcome.run_id)
    assert run.status == AgentRunStatus.FAILED.value
    assert run.ended_at is not None


async def test_arguments_that_fail_validation_are_audited() -> None:
    await _seed_definition()
    bad_json = ToolCallPart("list_operation_groups", "{not json")

    outcome = await _invoker(_scripted(bad_json)).run(_make_operator(), "reader", "go")

    rows = _agent_rows(await _rows(outcome.run_id))
    assert [(r.payload["op_id"], r.status_code) for r in rows] == [("list_operation_groups", 400)]


async def test_rows_carry_decision_or_execution_class_after_cutover(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
) -> None:
    monkeypatch.setenv("REQUEST_USAGE_RETENTION_DAYS", "30")
    monkeypatch.setenv("AUDIT_WRAPPER_ROWS_RETIRED", "true")
    get_settings.cache_clear()
    await _seed_definition(toolset={"connectors": []})
    await _seed_echo_op(stub_embedding_service)

    outcome = await _invoker(_scripted(_CALL, _GROUPS)).run(_make_operator(), "reader", "go")

    assert outcome.status is AgentRunStatus.SUCCEEDED
    classes = {
        (r.payload["op_id"], r.event_class) for r in _agent_rows(await _rows(outcome.run_id))
    }
    assert classes == {
        ("call_operation", AuditEventClass.POLICY_DECISION),
        ("list_operation_groups", AuditEventClass.OPERATION_EXECUTION),
    }


async def test_audit_write_failure_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed_definition()

    async def _fail(**_: Any) -> UUID:
        raise RuntimeError("audit store down")

    monkeypatch.setattr(tool_audit, "write_agent_tool_row", _fail)

    outcome = await _invoker(_scripted(_GROUPS)).run(_make_operator(), "reader", "go")

    assert outcome.status is AgentRunStatus.FAILED


async def test_cancelled_run_records_ended_at() -> None:
    await _seed_definition()
    release = asyncio.Event()

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await release.wait()
        return ModelResponse(parts=[TextPart("late")])

    invoker = _invoker(FunctionModel(fn))
    operator = _make_operator()
    outcome = await invoker.run(operator, "reader", "go", async_mode=True)

    summary = await invoker.cancel(operator, outcome.run_id)
    release.set()

    assert summary.status is AgentRunStatus.CANCELLED
    assert summary.ended_at is not None
    for _ in range(200):
        if outcome.run_id not in invoker._store:
            break
        await asyncio.sleep(0.01)
    run = await _run_row(outcome.run_id)
    assert run.status == AgentRunStatus.CANCELLED.value
    assert run.ended_at is not None

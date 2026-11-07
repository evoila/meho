# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A cancelled run stops and is charged; a start counts runs in progress.

* ``test_cancel_stops_the_loop_and_charges_the_finished_call`` -- cancel
  ends the loop at once: the model is asked no more questions, and the
  call that finished before the cancel is charged.
* ``test_cancel_on_another_worker_stops_the_loop_when_the_lease_is_lost``
  -- the heartbeat finds the lease gone and stops the loop.
* ``test_second_run_is_refused_while_the_first_one_is_in_progress`` -- with
  a cap of one request, a second start before the first one ends is
  refused, and a start is allowed again once the first one ended.
* ``test_runs_of_another_person_do_not_count`` -- the count is per person.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.agent.run import BudgetExceededError, PydanticAgentRun
from meho_backplane.agents.schemas import AgentDefinitionCreate, AgentModelTier
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPrincipal,
    AgentRunStatus,
    IdentityBudget,
    Tenant,
)
from meho_backplane.db.models import AgentRun as AgentRunRow
from meho_backplane.operations import agent_run as run_lifecycle
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.identity_budget import BudgetWindowKind, set_limits
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

pytestmark = pytest.mark.asyncio

_TENANT = UUID("44444444-4444-4444-4444-444444444444")
_AGENT = "spend-cap-agent"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("AGENT_DEFAULT_MODEL", "anthropic:claude-sonnet-5-5")
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
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.1] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


def _operator(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name="Agent Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed() -> None:
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, _TENANT) is None:
            session.add(Tenant(id=_TENANT, slug="tenant-cap", name="Tenant Cap"))
            await session.commit()
        session.add(
            AgentPrincipal(
                id=uuid4(),
                tenant_id=_TENANT,
                name=_AGENT,
                keycloak_client_id=f"agent:{_AGENT}",
                keycloak_internal_id=f"kc-internal-{_TENANT}-{_AGENT}",
                owner_sub="seed-admin",
                revoked=False,
                created_by_sub="seed-admin",
            )
        )
        await session.commit()
    await AgentDefinitionService().create(
        tenant_id=_TENANT,
        created_by_sub="seed-admin",
        payload=AgentDefinitionCreate(
            name=_AGENT,
            identity_ref=f"agent:{_AGENT}",
            model_tier=AgentModelTier.STANDARD,
            system_prompt="You are a spend cap integration test.",
            toolset={},
            turn_budget=5,
        ),
    )


class _SlowSecondCall:
    """A model that finishes one call, then waits on the second one."""

    def __init__(self) -> None:
        self.calls = 0
        self.second_call_started = asyncio.Event()

    async def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("list_operation_groups", {"connector_id": "guess-1.x"})],
                usage=RequestUsage(input_tokens=100, output_tokens=10),
            )
        self.second_call_started.set()
        await asyncio.sleep(60)
        raise AssertionError("the loop was not stopped")  # pragma: no cover


def _invoker(model: _SlowSecondCall) -> AgentInvoker:
    return AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: FunctionModel(model)))


async def _wait_for_end(task: asyncio.Task[None], timeout: float = 10) -> None:
    await asyncio.wait({task}, timeout=timeout)
    assert task.done()


async def _row(run_id: UUID) -> AgentRunRow:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(AgentRunRow).where(AgentRunRow.id == run_id))
        ).scalar_one()


async def _budget_rows(sub: str) -> list[IdentityBudget]:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(IdentityBudget).where(IdentityBudget.principal_sub == sub)
        )
        return list(result.scalars().all())


async def test_cancel_stops_the_loop_and_charges_the_finished_call(
    stub_embedding_service: AsyncMock,
) -> None:
    await _seed()
    model = _SlowSecondCall()
    invoker = _invoker(model)
    op = _operator("cap-cancel")
    outcome = await invoker.run(op, _AGENT, "go", async_mode=True)
    task = invoker._store[outcome.run_id].task
    await asyncio.wait_for(model.second_call_started.wait(), timeout=10)

    await invoker.cancel(op, outcome.run_id)
    await _wait_for_end(task)

    assert model.calls == 2
    row = await _row(outcome.run_id)
    assert row.status == AgentRunStatus.CANCELLED.value
    assert row.cost is not None
    assert row.cost > 0
    rows = await _budget_rows(op.sub)
    assert {r.window_kind for r in rows} == {"daily", "weekly", "monthly"}
    for r in rows:
        assert r.requests_consumed == 1
        assert r.tokens_consumed == 110
        assert r.cost_consumed == row.cost


async def test_cancel_on_another_worker_stops_the_loop_when_the_lease_is_lost(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed()
    # The smallest lease the settings allow: the heartbeat checks every 5 s.
    monkeypatch.setenv("AGENT_RUN_LEASE_TTL_SECONDS", "10")
    get_settings.cache_clear()
    model = _SlowSecondCall()
    invoker = _invoker(model)
    op = _operator("cap-cancel-remote")
    outcome = await invoker.run(op, _AGENT, "go", async_mode=True)
    task = invoker._store[outcome.run_id].task
    await asyncio.wait_for(model.second_call_started.wait(), timeout=10)

    # Another worker cancels the row; this worker's task is not touched.
    async with get_sessionmaker()() as session:
        await run_lifecycle.cancel_run(session, outcome.run_id, operator=op)
        await session.commit()
    await _wait_for_end(task, timeout=15)

    assert model.calls == 2
    rows = await _budget_rows(op.sub)
    assert [r.requests_consumed for r in rows] == [1, 1, 1]


async def test_second_run_is_refused_while_the_first_one_is_in_progress(
    stub_embedding_service: AsyncMock,
) -> None:
    await _seed()
    op = _operator("cap-parallel")
    async with get_sessionmaker()() as session:
        await set_limits(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            window_kind=BudgetWindowKind.DAILY,
            request_limit=1,
        )
        await session.commit()
    model = _SlowSecondCall()
    invoker = _invoker(model)
    first = await invoker.run(op, _AGENT, "go", async_mode=True)
    await asyncio.wait_for(model.second_call_started.wait(), timeout=10)

    with pytest.raises(BudgetExceededError):
        await invoker.run(op, _AGENT, "go", async_mode=True)
    async with get_sessionmaker()() as session:
        runs = (
            (await session.execute(select(AgentRunRow).where(AgentRunRow.identity_sub == op.sub)))
            .scalars()
            .all()
        )
    assert [r.id for r in runs] == [first.run_id]

    # Once the first run ended and was charged, the cap itself refuses.
    task = invoker._store[first.run_id].task
    await invoker.cancel(op, first.run_id)
    await _wait_for_end(task)
    with pytest.raises(BudgetExceededError):
        await invoker.run(op, _AGENT, "go", async_mode=True)


async def test_runs_of_another_person_do_not_count(
    stub_embedding_service: AsyncMock,
) -> None:
    await _seed()
    first_op = _operator("cap-person-a")
    second_op = _operator("cap-person-b")
    async with get_sessionmaker()() as session:
        for sub in (first_op.sub, second_op.sub):
            await set_limits(
                session,
                tenant_id=_TENANT,
                principal_sub=sub,
                window_kind=BudgetWindowKind.DAILY,
                request_limit=1,
            )
        await session.commit()
    model = _SlowSecondCall()
    invoker = _invoker(model)
    first = await invoker.run(first_op, _AGENT, "go", async_mode=True)
    await asyncio.wait_for(model.second_call_started.wait(), timeout=10)

    second = await invoker.run(second_op, _AGENT, "go", async_mode=True)
    assert second.run_id != first.run_id

    for outcome, op in ((first, first_op), (second, second_op)):
        task = invoker._store[outcome.run_id].task
        await invoker.cancel(op, outcome.run_id)
        await _wait_for_end(task)

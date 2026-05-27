# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration: a finished agent run stamps cost + applies budget consumption.

Initiative #806 (G11.5 Portability + cost), Task #1079 (G11.5-T5).

The task's acceptance criterion *"Consumption increments from a real
run's usage; a query returns remaining budget per identity/window"*
is what this module pins. It drives the
:class:`~meho_backplane.agent.invocation.AgentInvoker` through a
deterministic :class:`~pydantic_ai.models.function.FunctionModel`
that stamps a known :class:`~pydantic_ai.usage.RequestUsage` on its
:class:`~pydantic_ai.messages.ModelResponse` and asserts:

* The durable :class:`~meho_backplane.db.models.AgentRun` row's
  ``cost`` column is set (no longer NULL) to the
  pricing-table-computed value.
* Three :class:`~meho_backplane.db.models.IdentityBudget` rows
  (daily / weekly / monthly) materialise for the run's principal.
* Their ``tokens_consumed`` / ``cost_consumed`` / ``requests_consumed``
  match the run's reported usage + computed cost.
* :func:`~meho_backplane.operations.identity_budget.get_remaining`
  returns a :class:`BudgetReading` whose ``*_remaining`` fields equal
  ``limit - consumed`` for the configured (daily) bucket and ``None``
  for dimensions left unset.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.agent.run import PydanticAgentRun
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
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.identity_budget import (
    MODEL_PRICING,
    get_remaining,
    set_limits,
)
from meho_backplane.operations.identity_budget import (
    BudgetWindowKind as ServiceWindowKind,
)
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

pytestmark = pytest.mark.asyncio

_TENANT = UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    # Pin the default model so we hit a known pricing-table entry.
    monkeypatch.setenv("AGENT_DEFAULT_MODEL", "anthropic:claude-sonnet-4-6")
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


def _make_operator(sub: str = "op-agent") -> Operator:
    return Operator(
        sub=sub,
        name="Agent Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_tenant_and_definition(*, name: str = "consumption-reader") -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        existing = await session.get(Tenant, _TENANT)
        if existing is None:
            session.add(Tenant(id=_TENANT, slug="tenant-c", name="Tenant C"))
            await session.commit()
        existing_p = await session.execute(
            select(AgentPrincipal).where(
                AgentPrincipal.tenant_id == _TENANT,
                AgentPrincipal.keycloak_client_id == f"agent:{name}",
            )
        )
        if existing_p.scalar_one_or_none() is None:
            session.add(
                AgentPrincipal(
                    id=uuid4(),
                    tenant_id=_TENANT,
                    name=name,
                    keycloak_client_id=f"agent:{name}",
                    keycloak_internal_id=f"kc-internal-{_TENANT}-{name}",
                    owner_sub="seed-admin",
                    revoked=False,
                    created_by_sub="seed-admin",
                )
            )
            await session.commit()
    service = AgentDefinitionService()
    await service.create(
        tenant_id=_TENANT,
        created_by_sub="seed-admin",
        payload=AgentDefinitionCreate(
            name=name,
            identity_ref=f"agent:{name}",
            model_tier=AgentModelTier.STANDARD,
            system_prompt="You are a budget integration test.",
            toolset={},
            turn_budget=5,
        ),
    )


def _final_text_with_usage(
    text: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> FunctionModel:
    """A model that answers immediately, stamping a known usage payload."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[TextPart(text)],
            usage=RequestUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            ),
        )

    return FunctionModel(fn)


def _invoker_with(model: FunctionModel) -> AgentInvoker:
    return AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: model))


async def test_finished_run_stamps_cost_and_applies_consumption(
    stub_embedding_service: AsyncMock,
) -> None:
    """A real run stamps :attr:`AgentRun.cost` *and* increments budget buckets."""
    await _seed_tenant_and_definition()
    input_tokens = 1_200
    output_tokens = 500
    invoker = _invoker_with(
        _final_text_with_usage(
            "done",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    )

    op = _make_operator(sub="agent-consumption-1")
    outcome = await invoker.run(op, "consumption-reader", "go")
    assert outcome.status is AgentRunStatus.SUCCEEDED

    # --- 1. AgentRun.cost stamped --------------------------------------
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(select(AgentRunRow).where(AgentRunRow.id == outcome.run_id))
        ).scalar_one()
    pricing = MODEL_PRICING["anthropic:claude-sonnet-4-6"]
    expected_cost = (
        pricing.input * Decimal(input_tokens) + pricing.output * Decimal(output_tokens)
    ) / Decimal(1_000_000)
    assert row.cost is not None
    assert row.cost == expected_cost

    # --- 2. Three IdentityBudget rows materialise ----------------------
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(IdentityBudget)
                    .where(IdentityBudget.principal_sub == op.sub)
                    .where(IdentityBudget.tenant_id == _TENANT)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 3
    kinds = {r.window_kind for r in rows}
    assert kinds == {"daily", "weekly", "monthly"}
    expected_tokens = Decimal(input_tokens + output_tokens)
    for r in rows:
        assert r.tokens_consumed == expected_tokens
        assert r.cost_consumed == expected_cost
        assert r.requests_consumed == 1


async def test_get_remaining_after_run_reports_correct_gap(
    stub_embedding_service: AsyncMock,
) -> None:
    """After a configured-limit bucket sees a run, ``*_remaining`` is exact."""
    await _seed_tenant_and_definition()

    sessionmaker = get_sessionmaker()
    op = _make_operator(sub="agent-consumption-2")

    # Pre-seed a daily token + cost limit for this principal.
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            window_kind=ServiceWindowKind.DAILY,
            token_limit=1_000_000,
            cost_limit=Decimal("1.00"),
        )
        await session.commit()

    input_tokens = 800
    output_tokens = 200
    invoker = _invoker_with(
        _final_text_with_usage(
            "ack",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    )
    outcome = await invoker.run(op, "consumption-reader", "go")
    assert outcome.status is AgentRunStatus.SUCCEEDED

    pricing = MODEL_PRICING["anthropic:claude-sonnet-4-6"]
    expected_cost = (
        pricing.input * Decimal(input_tokens) + pricing.output * Decimal(output_tokens)
    ) / Decimal(1_000_000)
    expected_tokens = Decimal(input_tokens + output_tokens)

    async with sessionmaker() as session:
        reading = await get_remaining(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            window_kind=ServiceWindowKind.DAILY,
        )
    assert reading.tokens_consumed == expected_tokens
    assert reading.cost_consumed == expected_cost
    assert reading.token_limit == Decimal(1_000_000)
    assert reading.tokens_remaining == Decimal(1_000_000) - expected_tokens
    assert reading.cost_limit == Decimal("1.00")
    assert reading.cost_remaining == Decimal("1.00") - expected_cost


async def test_failed_run_skips_consumption_application(
    stub_embedding_service: AsyncMock,
) -> None:
    """A run that fails (turn-budget exhausted) charges no budget bucket.

    The contract pinned here: consumption is **success-only**. A run
    that ends in :attr:`AgentRunStatus.FAILED` neither stamps
    :attr:`AgentRun.cost` nor materialises any :class:`IdentityBudget`
    row. Future enforcement (C3-b, #1080) may want to record best-
    effort partial usage on failed runs, but that is its decision to
    make explicitly — the v0.2 contract is "failed runs cost nothing".
    """
    from pydantic_ai.messages import ToolCallPart

    await _seed_tenant_and_definition(name="loop-forever")
    runtime = PydanticAgentRun(
        # Model that only calls a missing tool — pydantic_ai burns the
        # turn budget and surfaces UsageLimitExceeded, which the seam
        # translates into AgentRunError, which finalize_run records as
        # ``failed`` (no usage applied).
        model_factory=lambda: FunctionModel(
            lambda messages, info: ModelResponse(parts=[ToolCallPart("nonexistent", {})])
        )
    )
    invoker = AgentInvoker(runtime=runtime)
    op = _make_operator(sub="agent-consumption-failed")
    outcome = await invoker.run(op, "loop-forever", "go")
    assert outcome.status is AgentRunStatus.FAILED

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # No budget rows for this principal.
        rows = (
            (
                await session.execute(
                    select(IdentityBudget).where(IdentityBudget.principal_sub == op.sub)
                )
            )
            .scalars()
            .all()
        )
        # And the agent_run row's cost is NULL.
        row = (
            await session.execute(select(AgentRunRow).where(AgentRunRow.id == outcome.run_id))
        ).scalar_one()
    assert rows == []
    assert row.cost is None

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end burn-down soak: N runs against a budget, observe the policy fire.

Initiative #806 (G11.5 Portability + cost), Task #1080 (G11.5-T6 /
C3-b), the acceptance criterion *"A soak test: total cost stays under
the configured budget; at threshold the resolved tier downgrades; at
cap runs are refused"*.

The test drives the
:class:`~meho_backplane.agent.invocation.AgentInvoker` through a
deterministic :class:`~pydantic_ai.models.function.FunctionModel` that
stamps a known per-call cost, then walks the principal's consumption
from 0% → 70% → 80% → 95% → 100% of the configured cost cap and
asserts:

* Up to 80% the runs ALLOW unchanged (no degradation, no refusal).
* From 80% to 100% the runs ALLOW but with the tier downgraded
  (INVESTIGATE → SUMMARIZE).
* At 100% the next run REFUSES with
  :class:`~meho_backplane.agent.run.BudgetExceededError`.
* The kill switch (global + per-tenant) refuses *before* any
  consumption is touched.

The model is wired through the direct programmatic path the M1
follow-up will eventually drive from the persisted ``model_tier``
column (the deferred enum unification per the task brief). Until that
lands, the burn-down asserts the gate's behaviour by inspecting the
:class:`~meho_backplane.operations.budget_enforcement.BudgetDecision`
directly + the live ``AgentRun`` row sequence the invoker writes.
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

from meho_backplane.agent.invocation import (
    AgentInvoker,
    BudgetExceededError,
)
from meho_backplane.agent.models import AgentTier
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.agents.schemas import AgentDefinitionCreate, AgentModelTier
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPrincipal,
    AgentRunStatus,
    BudgetWindowKind,
    IdentityBudget,
    Tenant,
)
from meho_backplane.db.models import AgentRun as AgentRunRow
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.budget_enforcement import (
    BudgetDecisionKind,
    EnforcementContext,
    evaluate_pre_run_budget,
)
from meho_backplane.operations.identity_budget import (
    MODEL_PRICING,
    apply_consumption,
    get_remaining,
    set_limits,
)
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

pytestmark = pytest.mark.asyncio

_TENANT = UUID("44444444-4444-4444-4444-444444444444")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
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


def _make_operator(sub: str = "agent-burn") -> Operator:
    return Operator(
        sub=sub,
        name="Burn-down agent",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_tenant_and_definition(*, name: str = "burner") -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        existing = await session.get(Tenant, _TENANT)
        if existing is None:
            session.add(Tenant(id=_TENANT, slug="tenant-burndown", name="Tenant Burndown"))
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
            system_prompt="You are a budget burn-down test agent.",
            toolset={},
            turn_budget=5,
        ),
    )


def _per_run_cost() -> Decimal:
    """A single run's cost under the pricing table with our usage shape."""
    pricing = MODEL_PRICING["anthropic:claude-sonnet-4-6"]
    return (pricing.input * Decimal(1_000) + pricing.output * Decimal(500)) / Decimal(1_000_000)


def _final_with_known_usage() -> FunctionModel:
    """Model that answers immediately with a stable, priced token usage."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[TextPart("ok")],
            usage=RequestUsage(
                input_tokens=1_000,
                output_tokens=500,
            ),
        )

    return FunctionModel(fn)


def _invoker_with(model: FunctionModel) -> AgentInvoker:
    return AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: model))


async def test_burndown_70_80_95_100_percent_traversal(
    stub_embedding_service: AsyncMock,
) -> None:
    """A 4-stage walk through the cost budget shows ALLOW / DOWNGRADE / REFUSE.

    The acceptance criterion for #1080: the gate's behaviour at each
    stage of the burn-down. The test runs the consumption service
    directly to ratchet the recorded state to 70 / 80 / 95 / 100 % of
    the configured cost cap, calls
    :func:`evaluate_pre_run_budget` at each stage, and asserts the
    decision matches the policy contract:

    * 70% — ALLOW unchanged (below threshold).
    * 80% — ALLOW with tier degraded (at threshold).
    * 95% — ALLOW with tier degraded (still below cap).
    * 100% — REFUSE.

    The pre-execution gate is what the public surface invokes; the
    consumption side (the *after* path) is the sibling #1079 task's
    responsibility — already covered in
    :mod:`tests.test_agent_run_consumption`.
    """
    await _seed_tenant_and_definition()
    op = _make_operator(sub="agent-burndown-walk")

    cost_cap = Decimal("10.00")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            window_kind=BudgetWindowKind.DAILY,
            cost_limit=cost_cap,
        )
        await session.commit()

    ctx = EnforcementContext(
        degrade_threshold=0.8,
        global_kill_switch=False,
        disabled_tenants=frozenset(),
    )

    # ---- 70% --------------------------------------------------------
    async with sessionmaker() as session:
        await apply_consumption(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            tokens=0,
            cost=cost_cap * Decimal("0.70"),
        )
        await session.commit()
    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            requested_tier=AgentTier.INVESTIGATE,
            context=ctx,
        )
    assert decision.kind is BudgetDecisionKind.ALLOW
    assert decision.tier is AgentTier.INVESTIGATE
    assert decision.downgraded is False

    # ---- 80% --------------------------------------------------------
    # 70 + 10 = 80% — bring the running total to exactly the threshold.
    async with sessionmaker() as session:
        await apply_consumption(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            tokens=0,
            cost=cost_cap * Decimal("0.10"),
        )
        await session.commit()
    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            requested_tier=AgentTier.INVESTIGATE,
            context=ctx,
        )
    assert decision.kind is BudgetDecisionKind.ALLOW
    assert decision.downgraded is True
    assert decision.tier is AgentTier.SUMMARIZE

    # ---- 95% --------------------------------------------------------
    # 80 + 15 = 95% — still under the cap, still over the threshold.
    async with sessionmaker() as session:
        await apply_consumption(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            tokens=0,
            cost=cost_cap * Decimal("0.15"),
        )
        await session.commit()
    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            requested_tier=AgentTier.INVESTIGATE,
            context=ctx,
        )
    assert decision.kind is BudgetDecisionKind.ALLOW
    assert decision.downgraded is True
    assert decision.tier is AgentTier.SUMMARIZE

    # ---- 100% -------------------------------------------------------
    # 95 + 5 = 100% — at cap; the gate now refuses.
    async with sessionmaker() as session:
        await apply_consumption(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            tokens=0,
            cost=cost_cap * Decimal("0.05"),
        )
        await session.commit()
    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            requested_tier=AgentTier.INVESTIGATE,
            context=ctx,
        )
    assert decision.kind is BudgetDecisionKind.REFUSE
    # Reason carries the dimension that fired so the audit row has
    # operator-actionable evidence.
    assert "cost_consumed" in decision.reason

    # Sanity check the budget state landed where we wrote it.
    async with sessionmaker() as session:
        reading = await get_remaining(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            window_kind=BudgetWindowKind.DAILY,
        )
    assert reading.cost_consumed == cost_cap
    assert reading.cost_remaining == Decimal(0)


async def test_invoker_raises_budget_exceeded_at_cap(
    stub_embedding_service: AsyncMock,
) -> None:
    """End-to-end: a capped principal whose next run would be over budget gets
    :class:`BudgetExceededError` from :meth:`AgentInvoker.run` --
    and no new ``agent_run`` row is created.

    Pins the *visible* refusal contract: the surface raises a typed
    exception the REST / MCP boundary maps to 429 / invalid-params,
    and the runs table stays clean.
    """
    await _seed_tenant_and_definition(name="cap-burner")
    op = _make_operator(sub="agent-burndown-cap")

    # Pre-seed the principal already at the cap.
    cost_cap = Decimal("0.50")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            window_kind=BudgetWindowKind.DAILY,
            cost_limit=cost_cap,
        )
        await apply_consumption(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            tokens=0,
            cost=cost_cap,
        )
        await session.commit()

    invoker = _invoker_with(_final_with_known_usage())
    with pytest.raises(BudgetExceededError) as exc:
        await invoker.run(op, "cap-burner", "go")
    assert "cost_consumed" in exc.value.reason

    # No agent_run row for this attempt — the gate fires *before* the
    # row is created so a stuck client doesn't fill the runs table
    # with FAILED rows.
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(AgentRunRow)
                    .where(AgentRunRow.tenant_id == _TENANT)
                    .where(AgentRunRow.identity_sub == op.sub)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


async def test_invoker_with_global_kill_switch_refuses_immediately(
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The global kill switch refuses runs regardless of budget state."""
    monkeypatch.setenv("AGENT_RUNS_DISABLED_GLOBAL", "true")
    get_settings.cache_clear()

    await _seed_tenant_and_definition(name="kill-switch-target")
    op = _make_operator(sub="agent-killed-globally")
    invoker = _invoker_with(_final_with_known_usage())
    with pytest.raises(BudgetExceededError) as exc:
        await invoker.run(op, "kill-switch-target", "go")
    assert "global kill switch" in exc.value.reason


async def test_invoker_with_per_tenant_kill_switch_refuses_listed_tenant(
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-tenant kill switch refuses only the listed tenant."""
    monkeypatch.setenv("AGENT_RUNS_DISABLED_TENANTS", str(_TENANT))
    get_settings.cache_clear()

    await _seed_tenant_and_definition(name="tenant-kill-target")
    op = _make_operator(sub="agent-killed-by-tenant")
    invoker = _invoker_with(_final_with_known_usage())
    with pytest.raises(BudgetExceededError) as exc:
        await invoker.run(op, "tenant-kill-target", "go")
    assert "kill-switched" in exc.value.reason


async def test_no_budget_consumption_recorded_on_refused_run(
    stub_embedding_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget-refused run never reaches the consumption increment.

    Tightens the contract: the budget table is not perturbed by a
    refused run -- the only mutation comes from successful runs.
    """
    monkeypatch.setenv("AGENT_RUNS_DISABLED_GLOBAL", "true")
    get_settings.cache_clear()

    await _seed_tenant_and_definition(name="not-consumed")
    op = _make_operator(sub="agent-not-charged")
    invoker = _invoker_with(_final_with_known_usage())
    with pytest.raises(BudgetExceededError):
        await invoker.run(op, "not-consumed", "go")

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(IdentityBudget).where(IdentityBudget.principal_sub == op.sub)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


async def test_burndown_total_cost_stays_under_budget(
    stub_embedding_service: AsyncMock,
) -> None:
    """Soak: N successful runs at known cost stay strictly under the cap.

    The Initiative #806 DoD line *"total cost stays under the
    configured budget in a soak test"*. The test does N successful
    runs against a generous cap (so the gate never fires), and
    asserts the recorded ``cost_consumed`` after each run equals
    ``per_run_cost * iteration`` and never exceeds the cap.
    """
    await _seed_tenant_and_definition(name="soak-runner")
    op = _make_operator(sub="agent-soak")
    per_run = _per_run_cost()
    # Big enough cap to fit 20 runs comfortably; the soak is on the
    # invariant "consumption matches the model's stamp", not on the
    # cap firing.
    cap = per_run * Decimal(50)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=_TENANT,
            principal_sub=op.sub,
            window_kind=BudgetWindowKind.DAILY,
            cost_limit=cap,
        )
        await session.commit()

    invoker = _invoker_with(_final_with_known_usage())
    iterations = 5
    for i in range(1, iterations + 1):
        outcome = await invoker.run(op, "soak-runner", f"run {i}")
        assert outcome.status is AgentRunStatus.SUCCEEDED
        async with sessionmaker() as session:
            reading = await get_remaining(
                session,
                tenant_id=_TENANT,
                principal_sub=op.sub,
                window_kind=BudgetWindowKind.DAILY,
            )
        assert reading.cost_consumed == per_run * Decimal(i)
        assert reading.cost_consumed <= cap

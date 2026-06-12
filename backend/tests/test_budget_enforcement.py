# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the pre-execution budget enforcement gate.

Initiative #806 (G11.5 Portability + cost), Task #1080 (G11.5-T6 /
C3-b). The module under test is
:mod:`meho_backplane.operations.budget_enforcement` — the pure-logic
gate the
:class:`~meho_backplane.agent.invocation.AgentInvoker` calls before
creating a durable ``agent_run`` row.

Coverage matrix
---------------

* **Kill switches.** Global flag refuses every run; per-tenant list
  refuses only the listed tenant; per-identity ``request_limit=0`` on
  a budget row refuses that identity (path through the cap branch).
* **Hard refusal at the cap.** Token / cost / request consumption
  ``>= limit`` returns REFUSE; the reason names the dimension.
* **Graceful degradation at the threshold.** A run that crosses
  configured threshold but not the cap downgrades the tier one rung
  along :data:`TIER_DOWNGRADE_LADDER`.
* **TRIAGE doesn't degrade further.** A TRIAGE request crossing the
  threshold is allowed unchanged (no cheaper tier exists).
* **No-tier definitions.** A run with ``requested_tier=None`` still
  goes through the gate; it can only ALLOW unchanged or REFUSE — no
  degradation possible.
* **Order of precedence.** Cap-breach wins over threshold-breach
  (REFUSE beats degradation when both fire).
* **Per-tenant kill list parsing.** Whitespace + case tolerance;
  empty string is the no-tenants-disabled sentinel; malformed UUID
  raises ``ValueError`` at parse time.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from meho_backplane.agent.models import AgentTier
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import BudgetWindowKind, Tenant
from meho_backplane.operations.budget_enforcement import (
    TIER_DOWNGRADE_LADDER,
    BudgetDecisionKind,
    EnforcementContext,
    evaluate_pre_run_budget,
    parse_disabled_tenants,
)
from meho_backplane.operations.identity_budget import (
    apply_consumption,
    set_limits,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin :class:`Settings`'s required env vars (same shape as the sibling tests)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant(slug: str = "budget-enforcement-test") -> uuid.UUID:
    """Insert a :class:`Tenant` row and return its id."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name="Tenant"))
        await session.commit()
    return tenant_id


def _ctx(
    *,
    degrade_threshold: float = 0.8,
    global_kill_switch: bool = False,
    disabled_tenants: frozenset[uuid.UUID] | None = None,
) -> EnforcementContext:
    """Build an :class:`EnforcementContext` without going through Settings."""
    return EnforcementContext(
        degrade_threshold=degrade_threshold,
        global_kill_switch=global_kill_switch,
        disabled_tenants=disabled_tenants or frozenset(),
    )


# ---------------------------------------------------------------------------
# parse_disabled_tenants
# ---------------------------------------------------------------------------


def test_parse_disabled_tenants_empty_string_returns_empty_set() -> None:
    """The documented "no tenants disabled" sentinel returns frozenset()."""
    assert parse_disabled_tenants("") == frozenset()
    assert parse_disabled_tenants("   ") == frozenset()


def test_parse_disabled_tenants_tolerates_whitespace_and_case() -> None:
    """Operator copy-paste from docs shouldn't fail on whitespace."""
    a = "11111111-1111-1111-1111-111111111111"
    b = "22222222-2222-2222-2222-222222222222"
    parsed = parse_disabled_tenants(f"  {a.upper()},{b},  ")
    assert parsed == frozenset({uuid.UUID(a), uuid.UUID(b)})


def test_parse_disabled_tenants_malformed_raises() -> None:
    """A typo is a configuration error and surfaces at parse time."""
    with pytest.raises(ValueError):
        parse_disabled_tenants("not-a-uuid")


# ---------------------------------------------------------------------------
# Kill switches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_kill_switch_refuses_before_db_read() -> None:
    """``global_kill_switch=True`` refuses every run without touching the DB."""
    tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-x",
            requested_tier=AgentTier.INVESTIGATE,
            context=_ctx(global_kill_switch=True),
        )
    assert decision.kind is BudgetDecisionKind.REFUSE
    assert "global kill switch" in decision.reason
    # Requested tier preserved on the decision so the raised error carries it.
    assert decision.tier is AgentTier.INVESTIGATE
    # No DB read happened — snapshots is empty.
    assert decision.snapshots == ()


@pytest.mark.asyncio
async def test_per_tenant_kill_list_refuses_listed_tenant_only() -> None:
    """Only the listed tenant is refused; others pass through cleanly."""
    blocked = uuid.uuid4()
    allowed = uuid.uuid4()
    ctx = _ctx(disabled_tenants=frozenset({blocked}))
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        refused = await evaluate_pre_run_budget(
            session,
            tenant_id=blocked,
            principal_sub="agent-x",
            requested_tier=AgentTier.TRIAGE,
            context=ctx,
        )
        allowed_decision = await evaluate_pre_run_budget(
            session,
            tenant_id=allowed,
            principal_sub="agent-x",
            requested_tier=AgentTier.TRIAGE,
            context=ctx,
        )
    assert refused.kind is BudgetDecisionKind.REFUSE
    assert "kill-switched" in refused.reason
    assert allowed_decision.kind is BudgetDecisionKind.ALLOW
    assert allowed_decision.tier is AgentTier.TRIAGE


@pytest.mark.asyncio
async def test_per_identity_kill_switch_via_zero_request_limit() -> None:
    """``request_limit=0`` on the bucket means "this principal cannot run".

    The cap-breach branch fires because ``requests_consumed (0) >=
    request_limit (0)``; the reason string distinguishes the kill
    switch case from "budget filled by use".
    """
    tenant_id = await _seed_tenant()
    when = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-disabled",
            window_kind=BudgetWindowKind.DAILY,
            request_limit=0,
            when=when,
        )
        await session.commit()

    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-disabled",
            requested_tier=AgentTier.INVESTIGATE,
            context=_ctx(),
        )
    assert decision.kind is BudgetDecisionKind.REFUSE
    assert "per-identity kill switch" in decision.reason


# ---------------------------------------------------------------------------
# Cap refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cap_refuses_when_cost_consumed_meets_cost_limit() -> None:
    """Once ``cost_consumed >= cost_limit`` on any window the gate refuses."""
    tenant_id = await _seed_tenant()
    when = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-spender",
            window_kind=BudgetWindowKind.DAILY,
            cost_limit=Decimal("3.00"),
            when=when,
        )
        await apply_consumption(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-spender",
            tokens=0,
            cost=Decimal("3.00"),
            when=when,
        )
        await session.commit()

    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-spender",
            requested_tier=AgentTier.INVESTIGATE,
            context=_ctx(),
        )
    assert decision.kind is BudgetDecisionKind.REFUSE
    assert "cost_consumed" in decision.reason
    assert "daily" in decision.reason


@pytest.mark.asyncio
async def test_cap_refuses_when_tokens_consumed_meets_token_limit() -> None:
    """The token dimension is enforced on the same all-or-nothing basis."""
    tenant_id = await _seed_tenant()
    when = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-token-burner",
            window_kind=BudgetWindowKind.WEEKLY,
            token_limit=10_000,
            when=when,
        )
        await apply_consumption(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-token-burner",
            tokens=10_000,
            cost=Decimal(0),
            when=when,
        )
        await session.commit()

    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-token-burner",
            requested_tier=AgentTier.TRIAGE,
            context=_ctx(),
        )
    assert decision.kind is BudgetDecisionKind.REFUSE
    assert "tokens_consumed" in decision.reason
    assert "weekly" in decision.reason


# ---------------------------------------------------------------------------
# Threshold degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_threshold_downgrades_investigate_to_summarize() -> None:
    """At threshold the resolver tier walks one rung down the ladder."""
    tenant_id = await _seed_tenant()
    when = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    # 80% of 1.00 USD = 0.80; spend 0.80 to hit the threshold exactly.
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-cheaper-please",
            window_kind=BudgetWindowKind.DAILY,
            cost_limit=Decimal("1.00"),
            when=when,
        )
        await apply_consumption(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-cheaper-please",
            tokens=0,
            cost=Decimal("0.80"),
            when=when,
        )
        await session.commit()

    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-cheaper-please",
            requested_tier=AgentTier.INVESTIGATE,
            context=_ctx(degrade_threshold=0.8),
        )
    assert decision.kind is BudgetDecisionKind.ALLOW
    assert decision.downgraded
    assert decision.tier is AgentTier.SUMMARIZE
    assert TIER_DOWNGRADE_LADDER[AgentTier.INVESTIGATE] is AgentTier.SUMMARIZE


@pytest.mark.asyncio
async def test_threshold_downgrades_summarize_to_triage() -> None:
    """The ladder's second rung lands on TRIAGE."""
    tenant_id = await _seed_tenant()
    when = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-summarize-er",
            window_kind=BudgetWindowKind.MONTHLY,
            cost_limit=Decimal("100.00"),
            when=when,
        )
        await apply_consumption(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-summarize-er",
            tokens=0,
            cost=Decimal("90.00"),  # 90% > 80% threshold
            when=when,
        )
        await session.commit()

    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-summarize-er",
            requested_tier=AgentTier.SUMMARIZE,
            context=_ctx(degrade_threshold=0.8),
        )
    assert decision.kind is BudgetDecisionKind.ALLOW
    assert decision.tier is AgentTier.TRIAGE
    assert decision.downgraded


@pytest.mark.asyncio
async def test_triage_at_threshold_runs_unchanged() -> None:
    """TRIAGE has no cheaper rung; the run is allowed unchanged."""
    tenant_id = await _seed_tenant()
    when = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-already-cheap",
            window_kind=BudgetWindowKind.DAILY,
            cost_limit=Decimal("1.00"),
            when=when,
        )
        await apply_consumption(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-already-cheap",
            tokens=0,
            cost=Decimal("0.85"),
            when=when,
        )
        await session.commit()

    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-already-cheap",
            requested_tier=AgentTier.TRIAGE,
            context=_ctx(degrade_threshold=0.8),
        )
    assert decision.kind is BudgetDecisionKind.ALLOW
    assert decision.downgraded is False
    assert decision.tier is AgentTier.TRIAGE


@pytest.mark.asyncio
async def test_threshold_does_not_fire_on_request_dimension() -> None:
    """Requests count toward cap but not toward degradation.

    Halfway through a request budget doesn't mean a cheaper tier
    saves anything (each run is still one request). The threshold
    branch is tokens + cost only.
    """
    tenant_id = await _seed_tenant()
    when = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-rate-limited",
            window_kind=BudgetWindowKind.DAILY,
            request_limit=10,
            when=when,
        )
        # 9 of 10 requests used: 90% > 80% threshold on the requests
        # dimension. But the policy doesn't trigger degradation on
        # requests, so the tier comes through unchanged.
        for _ in range(9):
            await apply_consumption(
                session,
                tenant_id=tenant_id,
                principal_sub="agent-rate-limited",
                tokens=0,
                cost=Decimal(0),
                when=when,
            )
        await session.commit()

    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-rate-limited",
            requested_tier=AgentTier.INVESTIGATE,
            context=_ctx(),
        )
    assert decision.kind is BudgetDecisionKind.ALLOW
    assert decision.tier is AgentTier.INVESTIGATE
    assert decision.downgraded is False


# ---------------------------------------------------------------------------
# Cap beats threshold (precedence)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cap_breach_takes_precedence_over_threshold() -> None:
    """When a window is over the cap, the gate refuses (doesn't degrade)."""
    tenant_id = await _seed_tenant()
    when = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-over-cap",
            window_kind=BudgetWindowKind.DAILY,
            cost_limit=Decimal("1.00"),
            when=when,
        )
        await apply_consumption(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-over-cap",
            tokens=0,
            cost=Decimal("1.00"),  # exactly at cap; both threshold + cap fire
            when=when,
        )
        await session.commit()

    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-over-cap",
            requested_tier=AgentTier.INVESTIGATE,
            context=_ctx(),
        )
    assert decision.kind is BudgetDecisionKind.REFUSE


# ---------------------------------------------------------------------------
# No-tier definitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tier_definition_still_refuses_at_cap() -> None:
    """A definition with no tier still runs through the gate; can REFUSE."""
    tenant_id = await _seed_tenant()
    when = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=tenant_id,
            principal_sub="legacy-agent",
            window_kind=BudgetWindowKind.DAILY,
            cost_limit=Decimal("1.00"),
            when=when,
        )
        await apply_consumption(
            session,
            tenant_id=tenant_id,
            principal_sub="legacy-agent",
            tokens=0,
            cost=Decimal("1.00"),
            when=when,
        )
        await session.commit()

    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=tenant_id,
            principal_sub="legacy-agent",
            requested_tier=None,
            context=_ctx(),
        )
    assert decision.kind is BudgetDecisionKind.REFUSE


@pytest.mark.asyncio
async def test_no_tier_definition_at_threshold_runs_unchanged() -> None:
    """No tier = no degradation path; threshold-crossed run still ALLOWs."""
    tenant_id = await _seed_tenant()
    when = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=tenant_id,
            principal_sub="legacy-agent-2",
            window_kind=BudgetWindowKind.DAILY,
            cost_limit=Decimal("1.00"),
            when=when,
        )
        await apply_consumption(
            session,
            tenant_id=tenant_id,
            principal_sub="legacy-agent-2",
            tokens=0,
            cost=Decimal("0.85"),
            when=when,
        )
        await session.commit()

    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=tenant_id,
            principal_sub="legacy-agent-2",
            requested_tier=None,
            context=_ctx(),
        )
    assert decision.kind is BudgetDecisionKind.ALLOW
    assert decision.tier is None
    assert decision.downgraded is False


# ---------------------------------------------------------------------------
# No budget configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_budget_configured_allows_unchanged() -> None:
    """A principal with no budget row is allowed unchanged ("no cap")."""
    tenant_id = await _seed_tenant()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        decision = await evaluate_pre_run_budget(
            session,
            tenant_id=tenant_id,
            principal_sub="unbudgeted-agent",
            requested_tier=AgentTier.INVESTIGATE,
            context=_ctx(),
        )
    assert decision.kind is BudgetDecisionKind.ALLOW
    assert decision.tier is AgentTier.INVESTIGATE
    assert decision.downgraded is False

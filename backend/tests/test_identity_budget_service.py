# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the per-identity budget service.

Initiative #806 (G11.5 Portability + cost), Task #1079 (G11.5-T5).
The module under test is
:mod:`meho_backplane.operations.identity_budget` -- the per-op cost
source + per-principal consumption bucketing service.

Coverage matrix
---------------

* **Cost computation.** :func:`compute_cost` applies the
  :data:`MODEL_PRICING` table to a :class:`TokenUsage` and returns a
  :class:`Decimal` USD cost; unknown model ids return
  :class:`Decimal(0)` (the known-unknown contract).
* **Window truncation.** :func:`window_start_for` truncates a
  :class:`datetime` to the bucket boundary for each
  :class:`BudgetWindowKind` (daily / weekly / monthly).
* **Apply consumption inserts on miss.** First
  :func:`apply_consumption` call for a (tenant, principal) creates
  three rows (one per window-kind) with the increment.
* **Apply consumption updates on hit.** A second
  :func:`apply_consumption` call for the same (tenant, principal,
  window) bumps the same three rows.
* **Window rotation.** Calling :func:`apply_consumption` on a date
  in the next day / week / month inserts new buckets rather than
  charging the old ones.
* **get_remaining returns gap = limit - consumed.** After setting
  limits and applying consumption, the reading exposes
  ``*_remaining`` correctly; unset limits surface as ``None``.
* **get_remaining synthesises an empty reading when no row exists.**
  The "no budget configured" answer is distinct from "budget
  exists, unspent" -- both honest, both returned.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import BudgetWindowKind, IdentityBudget, Tenant
from meho_backplane.operations.identity_budget import (
    MODEL_PRICING,
    BudgetReading,
    TokenUsage,
    apply_consumption,
    compute_cost,
    get_remaining,
    set_limits,
    window_start_for,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin :class:`Settings`'s required env vars."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant(slug: str = "identity-budget-service-test") -> uuid.UUID:
    """Insert a :class:`Tenant` row and return its id."""
    sessionmaker = get_sessionmaker()
    tenant_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name="Tenant"))
        await session.commit()
    return tenant_id


# ---------------------------------------------------------------------------
# compute_cost
# ---------------------------------------------------------------------------


def test_compute_cost_for_known_model_matches_published_rates() -> None:
    """Cost = sum(rate * tokens) / 1M, in :class:`Decimal`."""
    model_id = "anthropic:claude-sonnet-4-6"
    pricing = MODEL_PRICING[model_id]
    usage = TokenUsage(
        input_tokens=1_000_000,
        output_tokens=500_000,
        cache_read_tokens=100_000,
        cache_write_tokens=10_000,
    )
    cost = compute_cost(usage, model_id)
    # Hand-computed: 1M*input + 0.5M*output + 0.1M*cache_read + 0.01M*cache_write
    # all divided by 1M.
    expected = (
        pricing.input * Decimal(1_000_000)
        + pricing.output * Decimal(500_000)
        + pricing.cache_read * Decimal(100_000)
        + pricing.cache_write * Decimal(10_000)
    ) / Decimal(1_000_000)
    assert cost == expected
    assert isinstance(cost, Decimal)


def test_compute_cost_unknown_model_returns_zero() -> None:
    """An un-priced model id resolves to ``Decimal(0)`` (known-unknown)."""
    usage = TokenUsage(input_tokens=10, output_tokens=20)
    assert compute_cost(usage, "fictional:made-up-model-v7") == Decimal(0)


def test_compute_cost_none_model_returns_zero() -> None:
    """``model_id is None`` (run never resolved a provider) returns 0."""
    usage = TokenUsage(input_tokens=10, output_tokens=20)
    assert compute_cost(usage, None) == Decimal(0)


# ---------------------------------------------------------------------------
# window_start_for
# ---------------------------------------------------------------------------


def test_window_start_daily_truncates_to_midnight_utc() -> None:
    when = datetime(2026, 5, 27, 14, 32, 11, tzinfo=UTC)
    start = window_start_for(BudgetWindowKind.DAILY, when)
    assert start == datetime(2026, 5, 27, 0, 0, 0, tzinfo=UTC)


def test_window_start_weekly_anchors_to_iso_monday_utc() -> None:
    # 2026-05-27 is a Wednesday; the Monday of that week is 2026-05-25.
    when = datetime(2026, 5, 27, 14, 32, 11, tzinfo=UTC)
    start = window_start_for(BudgetWindowKind.WEEKLY, when)
    assert start == datetime(2026, 5, 25, 0, 0, 0, tzinfo=UTC)
    assert start.isoweekday() == 1


def test_window_start_monthly_anchors_to_first_of_month_utc() -> None:
    when = datetime(2026, 5, 27, 14, 32, 11, tzinfo=UTC)
    start = window_start_for(BudgetWindowKind.MONTHLY, when)
    assert start == datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)


def test_window_start_rejects_naive_datetime() -> None:
    """A naive datetime is a programming error (silent UTC assumption would be a bug)."""
    naive = datetime(2026, 5, 27, 14, 32, 11)
    with pytest.raises(ValueError, match="aware"):
        window_start_for(BudgetWindowKind.DAILY, naive)


# ---------------------------------------------------------------------------
# apply_consumption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_consumption_creates_three_buckets_on_miss() -> None:
    """First charge for a (tenant, principal) inserts daily + weekly + monthly."""
    tenant_id = await _seed_tenant()
    when = datetime(2026, 5, 27, 14, 0, 0, tzinfo=UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        buckets = await apply_consumption(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-1",
            tokens=12_000,
            cost=Decimal("0.075"),
            when=when,
        )
        await session.commit()

    assert len(buckets) == 3
    kinds = {b.window_kind for b in buckets}
    assert kinds == {"daily", "weekly", "monthly"}
    for bucket in buckets:
        assert bucket.tokens_consumed == Decimal(12_000)
        assert bucket.cost_consumed == Decimal("0.075")
        assert bucket.requests_consumed == 1


@pytest.mark.asyncio
async def test_apply_consumption_increments_existing_buckets_on_hit() -> None:
    """A second charge in the same window updates rather than inserts."""
    tenant_id = await _seed_tenant()
    when = datetime(2026, 5, 27, 14, 0, 0, tzinfo=UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await apply_consumption(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-2",
            tokens=10_000,
            cost=Decimal("0.05"),
            when=when,
        )
        await session.commit()

    async with sessionmaker() as session:
        await apply_consumption(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-2",
            tokens=5_000,
            cost=Decimal("0.025"),
            when=when + timedelta(minutes=5),  # same windows
        )
        await session.commit()

    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(IdentityBudget)
                    .where(IdentityBudget.principal_sub == "agent-2")
                    .where(IdentityBudget.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 3  # still three buckets, not six
    for row in rows:
        assert row.tokens_consumed == Decimal(15_000)
        assert row.cost_consumed == Decimal("0.075")
        assert row.requests_consumed == 2


@pytest.mark.asyncio
async def test_apply_consumption_rotates_daily_bucket_on_new_day() -> None:
    """A charge on day N+1 creates a new daily bucket, not a hit on day N."""
    tenant_id = await _seed_tenant()
    day_one = datetime(2026, 5, 27, 14, 0, 0, tzinfo=UTC)
    day_two = datetime(2026, 5, 28, 14, 0, 0, tzinfo=UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await apply_consumption(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-3",
            tokens=1000,
            cost=Decimal("0.01"),
            when=day_one,
        )
        await apply_consumption(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-3",
            tokens=2000,
            cost=Decimal("0.02"),
            when=day_two,
        )
        await session.commit()

    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(IdentityBudget)
                    .where(IdentityBudget.principal_sub == "agent-3")
                    .where(IdentityBudget.window_kind == "daily")
                )
            )
            .scalars()
            .all()
        )
    # Two daily buckets, distinct window_start.
    assert len(rows) == 2
    starts = sorted(r.window_start for r in rows)
    assert starts[0].day == 27
    assert starts[1].day == 28


# ---------------------------------------------------------------------------
# set_limits + get_remaining
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_limits_creates_bucket_with_caps() -> None:
    """Seed limits on a brand-new bucket; consumption stays zero."""
    tenant_id = await _seed_tenant()
    when = datetime(2026, 5, 27, 14, 0, 0, tzinfo=UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        bucket = await set_limits(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-4",
            window_kind=BudgetWindowKind.DAILY,
            token_limit=1_000_000,
            cost_limit=Decimal("5.00"),
            request_limit=100,
            when=when,
        )
        await session.commit()
    assert bucket.token_limit == Decimal(1_000_000)
    assert bucket.cost_limit == Decimal("5.00")
    assert bucket.request_limit == 100
    assert bucket.tokens_consumed == Decimal(0)


@pytest.mark.asyncio
async def test_get_remaining_returns_gap_after_consumption() -> None:
    """remaining = limit - consumed; unset limit dimensions surface as None."""
    tenant_id = await _seed_tenant()
    when = datetime(2026, 5, 27, 14, 0, 0, tzinfo=UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-5",
            window_kind=BudgetWindowKind.DAILY,
            token_limit=100_000,
            cost_limit=Decimal("1.00"),
            # request_limit intentionally not set -- stays NULL.
            when=when,
        )
        await apply_consumption(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-5",
            tokens=12_000,
            cost=Decimal("0.30"),
            when=when,
        )
        await session.commit()

    async with sessionmaker() as session:
        reading = await get_remaining(
            session,
            tenant_id=tenant_id,
            principal_sub="agent-5",
            window_kind=BudgetWindowKind.DAILY,
            when=when,
        )
    assert isinstance(reading, BudgetReading)
    assert reading.token_limit == Decimal(100_000)
    assert reading.tokens_consumed == Decimal(12_000)
    assert reading.tokens_remaining == Decimal(88_000)
    assert reading.cost_limit == Decimal("1.00")
    assert reading.cost_consumed == Decimal("0.30")
    assert reading.cost_remaining == Decimal("0.70")
    assert reading.request_limit is None
    assert reading.requests_remaining is None


@pytest.mark.asyncio
async def test_get_remaining_returns_empty_reading_when_no_bucket() -> None:
    """A principal with no budget configured returns zero / None remaining."""
    tenant_id = await _seed_tenant()
    when = datetime(2026, 5, 27, 14, 0, 0, tzinfo=UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        reading = await get_remaining(
            session,
            tenant_id=tenant_id,
            principal_sub="unknown-principal",
            window_kind=BudgetWindowKind.DAILY,
            when=when,
        )
    assert reading.tokens_consumed == Decimal(0)
    assert reading.cost_consumed == Decimal(0)
    assert reading.requests_consumed == 0
    assert reading.token_limit is None
    assert reading.cost_limit is None
    assert reading.request_limit is None
    assert reading.tokens_remaining is None
    assert reading.cost_remaining is None
    assert reading.requests_remaining is None

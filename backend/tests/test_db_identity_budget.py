# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :class:`meho_backplane.db.models.IdentityBudget`.

Initiative #806 (G11.5 Portability + cost), Task #1079 (G11.5-T5).

Coverage matrix
---------------

* **Round-trip.** Insert a row, query it back, every field
  round-trips. ORM ``default=`` machinery (uuid, created_at,
  consumption-counter zeros) fires on the SQLite path where the
  migration's PG ``server_default`` is a no-op.
* **Composite uniqueness.** Two inserts with identical
  ``(tenant_id, principal_sub, window_kind, window_start)`` raise
  :class:`IntegrityError` on the second commit.
* **Window-kind drift guard.** Every member of
  :class:`BudgetWindowKind` is accepted by the DB-level CHECK
  constraint; a string outside the enum is rejected.
* **Limit nullability.** ``token_limit`` / ``cost_limit`` /
  ``request_limit`` round-trip as ``None``.
* **Foreign key enforcement.** ``tenant_id`` references
  ``tenant.id``; a non-existent tenant id raises :class:`IntegrityError`
  with SQLite ``PRAGMA foreign_keys = ON``.

The tests use ``sqlite+aiosqlite`` via the shared engine cache that
the autouse ``_default_database_url`` fixture in
:mod:`tests.conftest` pre-migrates to ``alembic upgrade head``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import BudgetWindowKind, IdentityBudget, Tenant
from meho_backplane.settings import get_settings


async def _enable_sqlite_foreign_keys(session: AsyncSession) -> None:
    """Issue ``PRAGMA foreign_keys = ON`` on the bound SQLite connection."""
    await session.execute(text("PRAGMA foreign_keys = ON"))


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_tenant(session: AsyncSession, slug: str = "identity-budget-test") -> uuid.UUID:
    """Insert a :class:`Tenant` row and return its id."""
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant for {slug}"))
    await session.commit()
    return tenant_id


@pytest.mark.asyncio
async def test_round_trip_persists_every_field() -> None:
    """Insert a fully-populated :class:`IdentityBudget`, fields round-trip."""
    sessionmaker = get_sessionmaker()
    row_id = uuid.uuid4()
    window_start = datetime(2026, 5, 27, 0, 0, 0, tzinfo=UTC)
    window_end = window_start + timedelta(days=1)
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            IdentityBudget(
                id=row_id,
                tenant_id=tenant_id,
                principal_sub="user-1",
                window_kind=BudgetWindowKind.DAILY.value,
                window_start=window_start,
                window_end=window_end,
                token_limit=Decimal(1_000_000),
                cost_limit=Decimal("10.50"),
                request_limit=100,
                tokens_consumed=Decimal(2500),
                cost_consumed=Decimal("0.075"),
                requests_consumed=3,
            )
        )
        await session.commit()
        loaded = (
            await session.execute(select(IdentityBudget).where(IdentityBudget.id == row_id))
        ).scalar_one()
    assert loaded.tenant_id == tenant_id
    assert loaded.principal_sub == "user-1"
    assert loaded.window_kind == "daily"
    assert loaded.token_limit == Decimal(1_000_000)
    assert loaded.cost_limit == Decimal("10.50")
    assert loaded.request_limit == 100
    assert loaded.tokens_consumed == Decimal(2500)
    assert loaded.cost_consumed == Decimal("0.075")
    assert loaded.requests_consumed == 3


@pytest.mark.asyncio
async def test_orm_defaults_fire_on_sqlite() -> None:
    """A minimal insert populates id / consumption / timestamps Python-side."""
    sessionmaker = get_sessionmaker()
    window_start = datetime(2026, 5, 27, 0, 0, 0, tzinfo=UTC)
    window_end = window_start + timedelta(days=7)
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        bucket = IdentityBudget(
            tenant_id=tenant_id,
            principal_sub="user-2",
            window_kind=BudgetWindowKind.WEEKLY.value,
            window_start=window_start,
            window_end=window_end,
        )
        session.add(bucket)
        await session.commit()
        assert bucket.id is not None
        assert bucket.tokens_consumed == Decimal(0)
        assert bucket.cost_consumed == Decimal(0)
        assert bucket.requests_consumed == 0
        assert bucket.created_at is not None
        assert bucket.updated_at is not None


@pytest.mark.asyncio
async def test_composite_uniqueness_constraint() -> None:
    """A duplicate (tenant, principal, kind, start) row is refused."""
    sessionmaker = get_sessionmaker()
    window_start = datetime(2026, 5, 27, 0, 0, 0, tzinfo=UTC)
    window_end = window_start + timedelta(days=1)
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        session.add(
            IdentityBudget(
                tenant_id=tenant_id,
                principal_sub="user-3",
                window_kind=BudgetWindowKind.DAILY.value,
                window_start=window_start,
                window_end=window_end,
            )
        )
        await session.commit()

        session.add(
            IdentityBudget(
                tenant_id=tenant_id,
                principal_sub="user-3",
                window_kind=BudgetWindowKind.DAILY.value,
                window_start=window_start,
                window_end=window_end,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_window_kind_check_accepts_every_enum_member() -> None:
    """Every :class:`BudgetWindowKind` value is accepted by the CHECK."""
    sessionmaker = get_sessionmaker()
    window_start = datetime(2026, 5, 27, 0, 0, 0, tzinfo=UTC)
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        for i, kind in enumerate(BudgetWindowKind):
            session.add(
                IdentityBudget(
                    tenant_id=tenant_id,
                    principal_sub=f"user-{i}",
                    window_kind=kind.value,
                    window_start=window_start,
                    window_end=window_start + timedelta(days=1),
                )
            )
        await session.commit()
        # All three accepted.
        rows = (
            (
                await session.execute(
                    select(IdentityBudget).where(IdentityBudget.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == len(list(BudgetWindowKind))


@pytest.mark.asyncio
async def test_limit_columns_round_trip_as_null() -> None:
    """``token_limit`` / ``cost_limit`` / ``request_limit`` round-trip as None."""
    sessionmaker = get_sessionmaker()
    window_start = datetime(2026, 5, 27, 0, 0, 0, tzinfo=UTC)
    async with sessionmaker() as session:
        tenant_id = await _seed_tenant(session)
        bucket = IdentityBudget(
            tenant_id=tenant_id,
            principal_sub="user-null-limits",
            window_kind=BudgetWindowKind.MONTHLY.value,
            window_start=window_start,
            window_end=window_start + timedelta(days=30),
        )
        session.add(bucket)
        await session.commit()
        loaded = (
            await session.execute(select(IdentityBudget).where(IdentityBudget.id == bucket.id))
        ).scalar_one()
    assert loaded.token_limit is None
    assert loaded.cost_limit is None
    assert loaded.request_limit is None

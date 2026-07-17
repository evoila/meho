# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Durable-row data access for the Dashboard entity (#2506).

The narrow data-access layer the
:class:`~meho_backplane.checks.dashboard_service.CheckDashboardAdminService`
calls. Every function takes an :class:`AsyncSession` it did not open -- the
caller's transaction owns the commit / rollback -- and flushes so row state
is visible within the transaction without committing (the flush-not-commit
discipline :mod:`meho_backplane.checks.repository` follows).

Membership is modelled as an explicit association table
(:class:`~meho_backplane.db.models.CheckDashboardSensor`) queried with plain
joins rather than a SQLAlchemy ``relationship(secondary=...)`` -- the rollup
reads the members' latest-state projection columns off the ``sensor`` row, so
an explicit join is both clearer and avoids dragging lazy-load machinery into
the async read path.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import CheckDashboard, CheckDashboardSensor, Sensor

__all__ = [
    "create_dashboard",
    "delete_dashboard",
    "existing_sensor_ids",
    "get_dashboard",
    "list_dashboards",
    "members_by_dashboard",
]


async def existing_sensor_ids(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    sensor_ids: Sequence[uuid.UUID],
) -> set[uuid.UUID]:
    """Return which of *sensor_ids* exist under *tenant_id*.

    The membership-validation query: the service compares the returned set
    against the requested ids and refuses the create when any is missing (a
    foreign or absent sensor id). Tenant-scoped, so another tenant's sensor
    id is reported absent rather than leaked.
    """
    if not sensor_ids:
        return set()
    stmt = select(Sensor.id).where(
        Sensor.tenant_id == tenant_id,
        Sensor.id.in_(list(sensor_ids)),
    )
    result = await session.execute(stmt)
    return set(result.scalars().all())


async def create_dashboard(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    description: str | None,
    sensor_ids: Sequence[uuid.UUID],
    created_by_sub: str,
) -> CheckDashboard:
    """Insert a Dashboard row plus one membership row per *sensor_ids*.

    *sensor_ids* is trusted to be validated (every id exists under
    *tenant_id*) and de-duplicated by the caller -- this function does not
    re-check, so a duplicate would trip the composite PK. Flushes so the row
    ids are populated within the caller's transaction.
    """
    row = CheckDashboard(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        description=description,
        created_by_sub=created_by_sub,
    )
    session.add(row)
    for sensor_id in sensor_ids:
        session.add(CheckDashboardSensor(dashboard_id=row.id, sensor_id=sensor_id))
    await session.flush()
    return row


async def list_dashboards(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    limit: int,
    offset: int = 0,
) -> Sequence[CheckDashboard]:
    """List Dashboards under *tenant_id*, newest-first."""
    stmt = (
        select(CheckDashboard)
        .where(CheckDashboard.tenant_id == tenant_id)
        .order_by(CheckDashboard.created_at.desc(), CheckDashboard.id)
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_dashboard(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    dashboard_id: uuid.UUID,
) -> CheckDashboard | None:
    """Return one Dashboard by id; ``None`` on absence / cross-tenant.

    The tenant filter is the first WHERE clause so a probe for another
    tenant's dashboard id surfaces as ``None`` (the 404 the boundary
    renders), never as the other tenant's row.
    """
    stmt = select(CheckDashboard).where(
        CheckDashboard.tenant_id == tenant_id,
        CheckDashboard.id == dashboard_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def members_by_dashboard(
    session: AsyncSession,
    *,
    dashboard_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, list[Sensor]]:
    """Load member ``sensor`` rows for *dashboard_ids*, grouped by dashboard.

    One join query for the whole set (no per-dashboard N+1); members are
    ordered by Sensor name for a stable render. A dashboard with no members
    is absent from the returned map -- the caller defaults it to ``[]``.
    """
    if not dashboard_ids:
        return {}
    stmt = (
        select(CheckDashboardSensor.dashboard_id, Sensor)
        .join(Sensor, Sensor.id == CheckDashboardSensor.sensor_id)
        .where(CheckDashboardSensor.dashboard_id.in_(list(dashboard_ids)))
        .order_by(Sensor.name, Sensor.id)
    )
    result = await session.execute(stmt)
    grouped: dict[uuid.UUID, list[Sensor]] = {}
    for dashboard_id, sensor in result.all():
        grouped.setdefault(dashboard_id, []).append(sensor)
    return grouped


async def delete_dashboard(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    dashboard_id: uuid.UUID,
) -> bool:
    """Delete a Dashboard by id; return ``True`` when a row was removed.

    Tenant-scoped: a cross-tenant / absent id removes nothing and returns
    ``False`` (the 404 the boundary renders). Memberships are deleted
    explicitly before the Dashboard row -- SQLite FK enforcement is opt-in,
    so the ``ondelete="CASCADE"`` FK cannot be relied on in every runtime;
    the explicit delete leaves no orphan membership rows on any dialect.
    """
    owned = await session.execute(
        select(CheckDashboard.id).where(
            CheckDashboard.tenant_id == tenant_id,
            CheckDashboard.id == dashboard_id,
        )
    )
    if owned.scalar_one_or_none() is None:
        return False
    await session.execute(
        delete(CheckDashboardSensor).where(CheckDashboardSensor.dashboard_id == dashboard_id)
    )
    await session.execute(
        delete(CheckDashboard).where(
            CheckDashboard.tenant_id == tenant_id,
            CheckDashboard.id == dashboard_id,
        )
    )
    return True

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Durable-row operations for the ``sensor`` table (#2503).

The narrow data-access layer the Sensor admin service and #2505's runner
call. Two concerns:

1. **Insert** a new sensor with a materialised ``next_fire_at`` (the admin
   service calls this; exposed here so tests can construct sensors without
   going through HTTP).
2. **Record a result** onto the latest-state projection -- the one named
   write path (Decision D). #2505's runner calls it locally after an
   evaluation, #2507 hooks the same persist path, and #2415-T3's gateway
   batch-post calls it for remote results.

All functions take an :class:`AsyncSession` they did not open -- the
caller's transaction owns the commit / rollback. They flush so the row
state is visible within the transaction but do not commit (the
flush-not-commit discipline
:mod:`meho_backplane.scheduler.repository` follows).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.checks.assertions import CheckState
from meho_backplane.db.models import Sensor, SensorCadenceKind, SensorStatus
from meho_backplane.scheduler.cron import next_fire_after

__all__ = [
    "create_sensor",
    "record_sensor_result",
]


async def create_sensor(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    connector_id: str,
    op_id: str,
    target: dict[str, object] | None,
    params: dict[str, object],
    assertion: dict[str, object],
    cadence_kind: SensorCadenceKind,
    interval_seconds: int | None,
    cron_expr: str | None,
    timezone: str,
    severity: str,
    for_seconds: int,
    identity_sub: str,
    created_by_sub: str,
    base: datetime | None = None,
) -> Sensor:
    """Insert a sensor with a materialised first ``next_fire_at``.

    *base* is the instant the first fire is computed from; the default
    ``datetime.now(UTC)`` is what production wants. Tests pass an explicit
    base to make the first-fire instant deterministic. For an interval
    cadence ``next_fire_at`` is ``base + interval_seconds``; for a cron
    cadence it is :func:`~meho_backplane.scheduler.cron.next_fire_after`
    (the same materialisation the scheduler uses), so #2505's claim query
    (``status='active' AND next_fire_at <= now``) is uniform across kinds.

    *cadence_kind* determines which of *interval_seconds* / *cron_expr* is
    non-``None`` -- the wire schema's model validator already proved the
    cadence union, so this function trusts the shape and asserts it for
    the type-checker.

    Raises:
        InvalidCronExpressionError: *cron_expr* is not a valid 5-field
            cron expression (cron cadence only).
    """
    if base is None:
        base = datetime.now(UTC)
    if cadence_kind == SensorCadenceKind.CRON:
        assert cron_expr is not None
        next_fire = next_fire_after(cron_expr, base, timezone)
    else:
        assert interval_seconds is not None
        next_fire = base + timedelta(seconds=interval_seconds)
    row = Sensor(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        connector_id=connector_id,
        op_id=op_id,
        target=target,
        params=params,
        assertion=assertion,
        status=SensorStatus.ACTIVE.value,
        cadence_kind=cadence_kind.value,
        interval_seconds=interval_seconds,
        cron_expr=cron_expr,
        timezone=timezone,
        next_fire_at=next_fire,
        severity=severity,
        for_seconds=for_seconds,
        identity_sub=identity_sub,
        created_by_sub=created_by_sub,
    )
    session.add(row)
    await session.flush()
    return row


async def record_sensor_result(
    session: AsyncSession,
    *,
    sensor_id: uuid.UUID,
    state: CheckState,
    value: object,
    evidence: dict[str, object],
    evaluated_at: datetime,
) -> bool:
    """Update the latest-state projection for *sensor_id*; return state-changed.

    Updates ``last_state`` / ``last_value`` / ``last_evidence`` /
    ``last_evaluated_at`` on every call, and bumps ``state_since`` **only**
    when ``state`` differs from the row's current ``last_state`` -- so
    #2506's ``for:`` hold-time hysteresis can read how long the current
    state has held. Returns ``True`` iff the state changed.

    A ``sensor_id`` that names no row (deleted between an evaluation and
    the persist) returns ``False`` without raising -- the runner treats
    the missing row as "nothing to record".
    """
    row = await session.get(Sensor, sensor_id)
    if row is None:
        return False
    changed = row.last_state != state
    row.last_state = state
    row.last_value = value
    row.last_evidence = evidence
    row.last_evaluated_at = evaluated_at
    if changed:
        row.state_since = evaluated_at
    await session.flush()
    return changed

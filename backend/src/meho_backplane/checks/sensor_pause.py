# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pause and resume a Sensor (``PATCH /api/v1/sensors/{id}`` with ``status``).

A paused Sensor is not claimed by the runner, so it does not call its
target. The row, its result history and its Dashboard memberships stay.
A resume makes the Sensor due at once and sets the count of refused logins
back to 0.

Pause and resume are safe to send twice: a Sensor that is already in the
asked status is not written again.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from meho_backplane.checks.schemas import SensorRead
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Sensor, SensorStatus
from meho_backplane.settings import get_settings

__all__ = ["PAUSED_BY_OPERATOR", "SensorStatusResult", "set_sensor_status"]

#: ``status_reason`` of a Sensor an operator paused.
PAUSED_BY_OPERATOR = "paused_by_operator"


@dataclass(frozen=True)
class SensorStatusResult:
    """The Sensor after the change, and the status before and after."""

    sensor: SensorRead
    before: dict[str, Any]
    after: dict[str, Any]


async def set_sensor_status(
    tenant_id: uuid.UUID,
    sensor_id: uuid.UUID,
    status: SensorStatus,
    *,
    now: datetime | None = None,
) -> SensorStatusResult | None:
    """Pause or resume one Sensor of *tenant_id*.

    Returns ``None`` when the Sensor does not exist in the tenant (the
    boundary answers 404). When the Sensor already has *status*, nothing is
    written and ``before`` and ``after`` are empty. When the ``unknown``
    reading after a resume fails, the failure is logged with the Sensor id
    and the saved resume is still returned.
    """
    now = now or datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.scalar(
            select(Sensor)
            .where(Sensor.tenant_id == tenant_id, Sensor.id == sensor_id)
            .with_for_update()
        )
        if row is None:
            return None
        if row.status == status.value:
            return SensorStatusResult(SensorRead.model_validate(row, from_attributes=True), {}, {})
        before = {"status": row.status, "status_reason": row.status_reason}
        if status is SensorStatus.PAUSED:
            row.status = SensorStatus.PAUSED.value
            row.status_reason = PAUSED_BY_OPERATOR
        else:
            row.status = SensorStatus.ACTIVE.value
            row.status_reason = None
            row.auth_failure_count = 0
            row.next_fire_at = now
        after = {"status": row.status, "status_reason": row.status_reason}
        await session.commit()
        await session.refresh(row)
        has_runner = row.runner_name is not None
        result = SensorStatusResult(
            SensorRead.model_validate(row, from_attributes=True), before, after
        )
    if status is SensorStatus.ACTIVE and has_runner and get_settings().runner_checks_enabled:
        try:
            await _mark_if_runner_stale(tenant_id, sensor_id, now)
        except Exception:
            # The resume is saved: answer with it. The check keeps its state
            # until the runner's next result. The stale sweep does not mark a
            # runner again that already has the stale mark (gateway/deadman.py).
            structlog.get_logger(__name__).warning(
                "sensor_resume_stale_mark_failed", sensor_id=str(sensor_id), exc_info=True
            )
    return result


async def _mark_if_runner_stale(tenant_id: uuid.UUID, sensor_id: uuid.UUID, now: datetime) -> None:
    """Record ``unknown`` on a resumed Sensor whose runner is marked stale.

    A runner that is away does not run the check, so the resumed Sensor
    must not keep its old state. Runs in its own transaction after the
    resume: it takes the runner's assignment lock first and the Sensor
    second, the same order as a result batch.
    """
    from meho_backplane.db.models import RunnerAssignmentRow
    from meho_backplane.gateway.repository import lock_assignment_row
    from meho_backplane.gateway.runner_checks import after_commit, mark_sensor_unknown

    async with get_sessionmaker()() as session:
        runner_name = await session.scalar(
            select(Sensor.runner_name).where(Sensor.id == sensor_id, Sensor.tenant_id == tenant_id)
        )
        if runner_name is None:
            return
        await lock_assignment_row(session, tenant_id=tenant_id, runner_name=runner_name)
        stale_at = await session.scalar(
            select(RunnerAssignmentRow.stale_at).where(
                RunnerAssignmentRow.tenant_id == tenant_id,
                RunnerAssignmentRow.runner_name == runner_name,
            )
        )
        if stale_at is None:
            return
        sensor = await session.scalar(
            select(Sensor)
            .where(
                Sensor.id == sensor_id,
                Sensor.tenant_id == tenant_id,
                Sensor.status == SensorStatus.ACTIVE.value,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if sensor is None:
            return
        counted = await mark_sensor_unknown(
            session, sensor, now=now, reason="runner_stale", runner=runner_name
        )
        await session.commit()
    await after_commit(tenant_id, [counted])

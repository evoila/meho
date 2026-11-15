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

from sqlalchemy import select

from meho_backplane.checks.schemas import SensorRead
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Sensor, SensorStatus

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
    written and ``before`` and ``after`` are empty.
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
        return SensorStatusResult(
            SensorRead.model_validate(row, from_attributes=True), before, after
        )

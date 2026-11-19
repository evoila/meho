# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run one Sensor once now (``POST /api/v1/sensors/{id}/run``).

The call does not run the operation itself. It moves the Sensor's next due
time to now. The runner then claims the Sensor at its next tick and runs it
like on its normal schedule: same login, same rules, same audit.

The call is safe to send twice. A Sensor that is already due keeps its due
time, and the call still succeeds. A paused Sensor is refused: the runner
does not claim it, so a run would never happen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from meho_backplane.checks.repository import accelerate_sensor_next_fire
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Sensor, SensorStatus

__all__ = ["SensorPausedError", "SensorRunAccepted", "request_sensor_run"]


class SensorPausedError(Exception):
    """The Sensor is paused, so the runner would not run it."""

    error_code = "sensor_paused"


class SensorRunAccepted(BaseModel):
    """Answer of the run route: the Sensor and when it is due."""

    model_config = ConfigDict(frozen=True)

    sensor_id: uuid.UUID
    next_fire_at: datetime


async def request_sensor_run(
    tenant_id: uuid.UUID,
    sensor_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> SensorRunAccepted | None:
    """Make one Sensor of *tenant_id* due now.

    Returns ``None`` when the Sensor does not exist in the tenant (the
    boundary answers 404). Raises :class:`SensorPausedError` for a paused
    Sensor.
    """
    now = now or datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.scalar(
            select(Sensor).where(Sensor.tenant_id == tenant_id, Sensor.id == sensor_id)
        )
        if row is None:
            return None
        if row.status == SensorStatus.PAUSED.value:
            raise SensorPausedError(
                f"the sensor is paused; resume it first (meho sensor resume {sensor_id})"
            )
        await accelerate_sensor_next_fire(session, sensor_id, not_later_than=now)
        await session.commit()
        await session.refresh(row)
        return SensorRunAccepted(sensor_id=sensor_id, next_fire_at=row.next_fire_at or now)

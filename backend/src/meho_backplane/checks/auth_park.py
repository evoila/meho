# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pause a Sensor whose target keeps refusing the login.

When the credential of a target is no longer valid, every evaluation starts
a new login that fails. So the runner counts the evaluations in a row that ended with a refused login
(``connector_auth_failed``). At the limit
(``SENSOR_AUTH_FAILURE_PARK_THRESHOLD``, default 3) it pauses the Sensor
with ``status_reason='auth_failed:ConnectorAuthError'`` and stops calling the
target.

Any other result sets the count back to 0. An operator resumes the Sensor
with ``meho sensor resume`` once the credential is fixed. A resume sets the
count back to 0 as well.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import Sensor, SensorStatus

__all__ = [
    "AUTH_FAILED_ERROR_CODE",
    "AUTH_PARK_REASON",
    "count_auth_outcome",
]

#: The error code the dispatcher gives to a refused login.
AUTH_FAILED_ERROR_CODE = "connector_auth_failed"

#: ``status_reason`` of a Sensor the runner paused after refused logins.
AUTH_PARK_REASON = "auth_failed:ConnectorAuthError"


async def count_auth_outcome(
    session: AsyncSession,
    sensor_id: uuid.UUID,
    *,
    auth_failed: bool,
    threshold: int,
) -> bool:
    """Count one evaluation result. Return ``True`` when this one paused the Sensor.

    Runs in the caller's open transaction, so the count and the result
    commit together. A Sensor that is gone, or is not active, is left alone.
    """
    row = await session.get(Sensor, sensor_id)
    if row is None or row.status != SensorStatus.ACTIVE.value:
        return False
    if not auth_failed:
        if row.auth_failure_count:
            row.auth_failure_count = 0
        return False
    row.auth_failure_count += 1
    if row.auth_failure_count < threshold:
        return False
    row.status = SensorStatus.PAUSED.value
    row.status_reason = AUTH_PARK_REASON
    return True

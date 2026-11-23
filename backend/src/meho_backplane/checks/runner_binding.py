# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Checks that a new Sensor may be bound to a runner.

A Sensor with a ``runner_name`` runs at a site, on that runner, and not in the
central check loop. The name must belong to a live runner of the Sensor's own
tenant, and the switch ``RUNNER_CHECKS_ENABLED`` must be on.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import RunnerPrincipal
from meho_backplane.gateway.repository import ensure_assignment_row
from meho_backplane.settings import get_settings

__all__ = ["SensorRunnerBindingError", "bind_sensor_to_runner"]


class SensorRunnerBindingError(Exception):
    """A Sensor cannot be bound to the runner it names.

    ``error_code`` is ``sensor_runner_checks_disabled`` (the switch is off)
    or ``sensor_runner_unknown`` (no live runner of that name in the tenant).
    The boundary maps both to 422.
    """

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


async def bind_sensor_to_runner(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_name: str,
) -> None:
    """Refuse the binding unless the switch is on and the runner exists.

    Also makes sure the runner has an assignment row, so the dead-man sweeper
    watches a runner that has only Sensors. Does not commit.
    """
    if not get_settings().runner_checks_enabled:
        raise SensorRunnerBindingError(
            "sensor_runner_checks_disabled",
            "runner checks are not turned on (RUNNER_CHECKS_ENABLED)",
        )
    found = await session.scalar(
        select(RunnerPrincipal.id).where(
            RunnerPrincipal.tenant_id == tenant_id,
            RunnerPrincipal.name == runner_name,
            RunnerPrincipal.revoked.is_(False),
        )
    )
    if found is None:
        raise SensorRunnerBindingError(
            "sensor_runner_unknown",
            f"no runner named {runner_name!r} in this tenant",
        )
    await ensure_assignment_row(session, tenant_id=tenant_id, runner_name=runner_name)

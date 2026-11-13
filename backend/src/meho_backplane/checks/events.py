# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Sensor and Dashboard changes as events for event triggers (#6180).

With ``CHECKS_AGENT_EVENTS`` on, a confirmed Sensor change and a Dashboard
change are written to the event outbox. An event trigger can then match them
like any other event. With the switch off, none of this runs.

The matcher compares the trigger filter with the **payload** only, never with
the event kind column. So every payload names its own event in ``event`` and
its producer in ``source.kind``.

Three producers:

* ``check.sensor.changed`` -- one event per confirmed Sensor state change.
  :func:`publish_sensor_changed` adds the row in the caller's open
  transaction, so the event commits together with ``last_state``.
* ``check.sensor.parked`` -- one event when the runner pauses a Sensor
  because its target keeps refusing the login. The reason is
  ``target_credential_stale``. Same transaction rule as above.
* ``check.dashboard.changed`` -- one event per cause group of a worsening
  Dashboard edge, and one event for every other claimed edge.
  :func:`publish_dashboard_changed` uses its own transaction.

No raw evidence is put in a payload. Evidence is text from the target, and
the agent can read it through the Sensor's results.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.events.outbox import publish

__all__ = [
    "CHECK_DASHBOARD_CHANGED",
    "CHECK_SENSOR_CHANGED",
    "CHECK_SENSOR_PARKED",
    "publish_dashboard_changed",
    "publish_sensor_changed",
    "publish_sensor_parked",
    "target_name",
]

CHECK_SENSOR_CHANGED: Final[str] = "check.sensor.changed"
CHECK_DASHBOARD_CHANGED: Final[str] = "check.dashboard.changed"
CHECK_SENSOR_PARKED: Final[str] = "check.sensor.parked"


def target_name(target: dict[str, object] | None) -> str | None:
    """Return the target's name, or ``None`` when it has no usable name.

    Only the name is used. The rest of a target dict (ids, aliases) stays out
    of the event.
    """
    if isinstance(target, dict):
        name = target.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


async def publish_sensor_changed(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    sensor_id: uuid.UUID,
    sensor_name: str,
    previous_state: str,
    state: str,
    severity: str,
    worsening: bool,
    target: dict[str, object] | None,
    connector_id: str,
    op_id: str,
    evaluated_at: datetime,
) -> None:
    """Add one ``check.sensor.changed`` row in *session*'s open transaction."""
    name = target_name(target)
    payload: dict[str, object] = {
        "event": CHECK_SENSOR_CHANGED,
        "source": {"kind": "sensor", "id": str(sensor_id), "name": sensor_name},
        "tenant_id": str(tenant_id),
        "previous_state": previous_state,
        "state": state,
        "severity": severity,
        "worsening": worsening,
        "target": name,
        "connector_id": connector_id,
        "op_id": op_id,
        "evaluated_at": evaluated_at.isoformat(),
        "coalesce_key": f"sensor:{name if name is not None else sensor_id}",
    }
    await publish(
        session,
        tenant_id=tenant_id,
        event_kind=CHECK_SENSOR_CHANGED,
        payload=payload,
    )


async def publish_dashboard_changed(
    *,
    tenant_id: uuid.UUID,
    dashboard_id: uuid.UUID,
    dashboard_name: str,
    previous_state: str,
    state: str,
    worsening: bool,
    group_key: str | None = None,
    members: Sequence[dict[str, object]] = (),
    known_noise: bool = False,
) -> None:
    """Write one ``check.dashboard.changed`` row in its own transaction.

    A cause group (*group_key* set) gives ``worsening: true`` and the group's
    Sensors in ``cause_group.members``. Every other claimed edge gives
    ``worsening: false`` and ``cause_group: null``.
    """
    cause_group: dict[str, object] | None = None
    coalesce_key = f"dashboard:{dashboard_id}"
    if group_key is not None:
        cause_group = {"key": group_key, "members": [dict(member) for member in members]}
        coalesce_key = f"dashboard:{dashboard_id}:{group_key}"
    payload: dict[str, object] = {
        "event": CHECK_DASHBOARD_CHANGED,
        "source": {"kind": "dashboard", "id": str(dashboard_id), "name": dashboard_name},
        "tenant_id": str(tenant_id),
        "previous_state": previous_state,
        "state": state,
        "worsening": worsening,
        "cause_group": cause_group,
        "known_noise": known_noise,
        "coalesce_key": coalesce_key,
    }
    async with get_sessionmaker()() as session:
        await publish(
            session,
            tenant_id=tenant_id,
            event_kind=CHECK_DASHBOARD_CHANGED,
            payload=payload,
        )
        await session.commit()


async def publish_sensor_parked(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    sensor_id: uuid.UUID,
    sensor_name: str,
    target: dict[str, object] | None,
    connector_id: str,
    op_id: str,
    parked_at: datetime,
) -> None:
    """Add one ``check.sensor.parked`` row in *session*'s open transaction."""
    name = target_name(target)
    payload: dict[str, object] = {
        "event": CHECK_SENSOR_PARKED,
        "source": {"kind": "sensor", "id": str(sensor_id), "name": sensor_name},
        "tenant_id": str(tenant_id),
        "reason": "target_credential_stale",
        "target": name,
        "connector_id": connector_id,
        "op_id": op_id,
        "parked_at": parked_at.isoformat(),
        "coalesce_key": f"sensor-parked:{name if name is not None else sensor_id}",
    }
    await publish(
        session,
        tenant_id=tenant_id,
        event_kind=CHECK_SENSOR_PARKED,
        payload=payload,
    )

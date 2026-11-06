# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Edit a Dashboard in place (``PATCH /api/v1/checks/dashboards/{id}``).

An edit adds or removes member Sensors and changes the name and the mail
settings. The id stays, and so do the settings the edit does not name.

Adding a Sensor that is already a member, or removing one that is not,
changes nothing. So the same edit is safe to send twice.

The function returns the before and after values of each changed field.
The route writes them into the audit row of the request.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.checks.dashboard_repository import existing_sensor_ids, members_by_dashboard
from meho_backplane.checks.dashboard_schemas import (
    _MAX_MEMBERS,
    _NAME_MAX_LENGTH,
    DashboardDetail,
    NotifyMinState,
    normalise_notify_email,
)
from meho_backplane.checks.dashboard_service import (
    DashboardNameConflictError,
    SensorNotFoundError,
    _to_detail,
)
from meho_backplane.checks.service import _is_unique_violation
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import CheckDashboard, CheckDashboardSensor

__all__ = [
    "DashboardEditInvalidError",
    "DashboardEditResult",
    "DashboardUpdate",
    "edit_dashboard",
]


class DashboardUpdate(BaseModel):
    """Request body for ``PATCH /api/v1/checks/dashboards/{dashboard_id}``.

    Every field is optional; at least one must be set. A field that is left
    out, or sent as ``null``, keeps its value.

    * ``add_sensor_ids`` / ``remove_sensor_ids`` -- Sensors to add to or
      remove from the members. One id cannot be in both lists.
    * ``notify_email`` -- one or more comma-separated recipients. An empty
      string turns mail off.
    * ``notify_min_state`` -- the state a change must reach before mail is
      sent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=_NAME_MAX_LENGTH)
    add_sensor_ids: list[uuid.UUID] | None = Field(default=None, max_length=_MAX_MEMBERS)
    remove_sensor_ids: list[uuid.UUID] | None = Field(default=None, max_length=_MAX_MEMBERS)
    notify_email: str | None = None
    notify_min_state: NotifyMinState | None = None

    @field_validator("notify_email", mode="after")
    @classmethod
    def _validate_notify_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            return ""
        return normalise_notify_email(value)

    @model_validator(mode="after")
    def _check_body(self) -> DashboardUpdate:
        if not self.model_dump(exclude_none=True):
            raise ValueError("set at least one field to change")
        both = set(self.add_sensor_ids or []) & set(self.remove_sensor_ids or [])
        if both:
            joined = ", ".join(sorted(str(sid) for sid in both))
            raise ValueError(f"a sensor id cannot be added and removed at once: {joined}")
        return self


class DashboardEditInvalidError(Exception):
    """Raised when the Dashboard after the edit would have too many members.

    The boundary maps it to 422 ``dashboard_edit_invalid``.
    """

    error_code = "dashboard_edit_invalid"


@dataclass(frozen=True)
class DashboardEditResult:
    """The Dashboard after the edit, and the changed fields before and after."""

    dashboard: DashboardDetail
    before: dict[str, Any]
    after: dict[str, Any]


def _settings(row: CheckDashboard) -> dict[str, Any]:
    return {
        "name": row.name,
        "notify_email": row.notify_email,
        "notify_min_state": row.notify_min_state,
    }


def _new_settings(row: CheckDashboard, patch: DashboardUpdate) -> dict[str, Any]:
    new = _settings(row)
    if patch.name is not None:
        new["name"] = patch.name
    if patch.notify_email is not None:
        new["notify_email"] = patch.notify_email or None
    if patch.notify_min_state is not None:
        new["notify_min_state"] = patch.notify_min_state
    return new


async def _new_members(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    current: list[uuid.UUID],
    patch: DashboardUpdate,
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Return the ids to add and to remove; check that added ids exist."""
    current_set = set(current)
    to_add = [sid for sid in dict.fromkeys(patch.add_sensor_ids or []) if sid not in current_set]
    to_remove = [sid for sid in dict.fromkeys(patch.remove_sensor_ids or []) if sid in current_set]
    if to_add:
        present = await existing_sensor_ids(session, tenant_id=tenant_id, sensor_ids=to_add)
        missing = [sid for sid in to_add if sid not in present]
        if missing:
            raise SensorNotFoundError(missing)
    if len(current) + len(to_add) - len(to_remove) > _MAX_MEMBERS:
        raise DashboardEditInvalidError(f"a dashboard can have at most {_MAX_MEMBERS} members")
    return to_add, to_remove


async def _apply(
    session: AsyncSession,
    row: CheckDashboard,
    new: dict[str, Any],
    to_add: list[uuid.UUID],
    to_remove: list[uuid.UUID],
) -> None:
    """Write the new settings and members; the caller commits."""
    row.name = new["name"]
    row.notify_email = new["notify_email"]
    row.notify_min_state = new["notify_min_state"]
    if to_remove:
        await session.execute(
            delete(CheckDashboardSensor).where(
                CheckDashboardSensor.dashboard_id == row.id,
                CheckDashboardSensor.sensor_id.in_(to_remove),
            )
        )
    for sid in to_add:
        session.add(CheckDashboardSensor(dashboard_id=row.id, sensor_id=sid))


async def edit_dashboard(
    tenant_id: uuid.UUID,
    dashboard_id: uuid.UUID,
    patch: DashboardUpdate,
    *,
    now: datetime | None = None,
) -> DashboardEditResult | None:
    """Change the fields in *patch* on one Dashboard of *tenant_id*.

    Returns ``None`` when the Dashboard does not exist in the tenant (the
    boundary answers 404). When nothing changes, the row is not written.

    Raises:
        SensorNotFoundError: an added Sensor is not in the tenant.
        DashboardEditInvalidError: the Dashboard would have too many members.
        DashboardNameConflictError: the new name is taken in the tenant.
    """
    now = now or datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.scalar(
            select(CheckDashboard)
            .where(CheckDashboard.tenant_id == tenant_id, CheckDashboard.id == dashboard_id)
            .with_for_update()
        )
        if row is None:
            return None
        current = list(
            (
                await session.scalars(
                    select(CheckDashboardSensor.sensor_id).where(
                        CheckDashboardSensor.dashboard_id == dashboard_id
                    )
                )
            ).all()
        )
        to_add, to_remove = await _new_members(session, tenant_id, current, patch)
        old, new = _settings(row), _new_settings(row, patch)
        before = {k: old[k] for k in old if old[k] != new[k]}
        after = {k: new[k] for k in new if old[k] != new[k]}
        if to_add or to_remove:
            new_ids = [sid for sid in current if sid not in set(to_remove)] + to_add
            before["sensor_ids"] = sorted(str(sid) for sid in current)
            after["sensor_ids"] = sorted(str(sid) for sid in new_ids)
        if after:
            # The member delete flushes the settings write, so a taken name
            # can raise inside ``_apply`` as well as at the commit.
            try:
                row.updated_at = now
                await _apply(session, row, new, to_add, to_remove)
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if _is_unique_violation(exc) and "name" in after:
                    raise DashboardNameConflictError(new["name"]) from exc
                raise
            await session.refresh(row)
        sensors = (await members_by_dashboard(session, dashboard_ids=[row.id])).get(row.id, [])
        return DashboardEditResult(_to_detail(row, sensors, now), before, after)

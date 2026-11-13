# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``spaces.overview`` read: owners, members, labs, versions and health.

Reads stored data only: spaces, members, assignments, targets (their
detected product and version) and the newest result of the checks (Sensors)
that watch each target. It calls no vendor system and writes nothing.

Health rule: the state is the one the newest check run of the target
reported. When that run is older than twice the check's interval, or no run
has a time, the state is ``unknown``: an old "ok" must not look current.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.resolver import resolve_target_version
from meho_backplane.connectors.spaces.common import (
    assignment_dict,
    parse_uuid,
    require_enabled,
)
from meho_backplane.connectors.spaces.writes import owner_left
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Sensor, SensorCadenceKind, Target
from meho_backplane.scheduler.cron import next_fire_after
from meho_backplane.spaces.models import ResourceAssignment, Space, SpaceMember

__all__ = ["health_state", "spaces_overview"]

#: A check result older than this many intervals reads ``unknown``.
_STALE_AFTER_INTERVALS = 2


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _interval_seconds(sensor: Sensor) -> float | None:
    """The time between two runs of the check, or ``None`` when unknown."""
    if sensor.cadence_kind == SensorCadenceKind.INTERVAL.value:
        return float(sensor.interval_seconds) if sensor.interval_seconds else None
    if sensor.cron_expr and sensor.last_evaluated_at is not None:
        try:
            first = next_fire_after(
                sensor.cron_expr, _aware(sensor.last_evaluated_at), sensor.timezone
            )
            second = next_fire_after(sensor.cron_expr, first, sensor.timezone)
        except ValueError:
            return None
        return (second - first).total_seconds()
    return None


def health_state(sensors: list[Sensor], now: datetime) -> dict[str, Any]:
    """The health of one target from the checks that watch it.

    Takes the check with the newest run. ``unknown`` when there is no
    check, no run time, no known interval, or the run is older than twice
    the interval.
    """
    timed = [s for s in sensors if s.last_evaluated_at is not None]
    if not timed:
        return {"state": "unknown", "last_evaluated_at": None, "check_interval_seconds": None}
    newest = max(timed, key=lambda s: _aware(s.last_evaluated_at))  # type: ignore[arg-type]
    evaluated = _aware(newest.last_evaluated_at)  # type: ignore[arg-type]
    interval = _interval_seconds(newest)
    state = newest.last_state
    if interval is None or now - evaluated > timedelta(seconds=_STALE_AFTER_INTERVALS * interval):
        state = "unknown"
    return {
        "state": state,
        "last_evaluated_at": evaluated.isoformat(),
        "check_interval_seconds": interval,
    }


def _detected_at(target: Target) -> str | None:
    fingerprint = target.fingerprint
    if isinstance(fingerprint, dict):
        probed = fingerprint.get("probed_at")
        if isinstance(probed, str):
            return probed
    return None


def _target_entry(target: Target | None, sensors: list[Sensor], now: datetime) -> dict[str, Any]:
    if target is None:
        return {"product": None, "version": None, "detected_at": None, "health": None}
    return {
        "product": target.product,
        "version": resolve_target_version(target),
        "detected_at": _detected_at(target),
        "health": health_state(sensors, now),
    }


async def _load(
    session: AsyncSession, tenant_id: uuid.UUID, space_id: uuid.UUID | None
) -> tuple[
    list[Space],
    dict[uuid.UUID, list[SpaceMember]],
    dict[uuid.UUID, list[ResourceAssignment]],
]:
    stmt = select(Space).where(Space.tenant_id == tenant_id)
    if space_id is not None:
        stmt = stmt.where(Space.id == space_id)
    spaces = list(await session.scalars(stmt.order_by(Space.name, Space.id)))
    ids = [s.id for s in spaces]
    members: dict[uuid.UUID, list[SpaceMember]] = defaultdict(list)
    assignments: dict[uuid.UUID, list[ResourceAssignment]] = defaultdict(list)
    if not ids:
        return spaces, members, assignments
    member_rows = await session.scalars(
        select(SpaceMember)
        .where(
            SpaceMember.tenant_id == tenant_id,
            SpaceMember.space_id.in_(ids),
            SpaceMember.valid_to.is_(None),
        )
        .order_by(SpaceMember.valid_from, SpaceMember.id)
    )
    for member in member_rows:
        members[member.space_id].append(member)
    assignment_rows = await session.scalars(
        select(ResourceAssignment)
        .where(
            ResourceAssignment.tenant_id == tenant_id,
            ResourceAssignment.space_id.in_(ids),
            ResourceAssignment.valid_to.is_(None),
        )
        .order_by(ResourceAssignment.valid_from, ResourceAssignment.id)
    )
    for assignment in assignment_rows:
        assignments[assignment.space_id].append(assignment)
    return spaces, members, assignments


async def _targets_and_sensors(
    session: AsyncSession, tenant_id: uuid.UUID, target_ids: set[uuid.UUID]
) -> tuple[dict[uuid.UUID, Target], dict[str, list[Sensor]]]:
    if not target_ids:
        return {}, {}
    targets = {
        t.id: t
        for t in await session.scalars(
            select(Target).where(
                Target.tenant_id == tenant_id,
                Target.id.in_(target_ids),
                Target.deleted_at.is_(None),
            )
        )
    }
    names = {t.name for t in targets.values()}
    by_name: dict[str, list[Sensor]] = defaultdict(list)
    if names:
        sensors = await session.scalars(
            select(Sensor).where(Sensor.tenant_id == tenant_id, Sensor.target.is_not(None))
        )
        for sensor in sensors:
            name = sensor.target.get("name") if isinstance(sensor.target, dict) else None
            if isinstance(name, str) and name in names:
                by_name[name].append(sensor)
    return targets, by_name


async def spaces_overview(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Show spaces with owner, members, assigned systems, versions and health.

    Op-id: ``spaces.overview``. Targetless and read-only. The tenant comes
    from *operator*. A space of another tenant is never shown.
    """
    require_enabled()
    space_id = parse_uuid(params, "space_id") if params.get("space_id") else None
    now = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        spaces, members, assignments = await _load(session, operator.tenant_id, space_id)
        target_ids = {
            a.target_id for rows in assignments.values() for a in rows if a.target_id is not None
        }
        targets, sensors_by_name = await _targets_and_sensors(
            session, operator.tenant_id, target_ids
        )
    out: list[dict[str, Any]] = []
    for space in spaces:
        current = members.get(space.id, [])
        left = owner_left(current)
        entries: list[dict[str, Any]] = []
        for row in assignments.get(space.id, []):
            entry = assignment_dict(row)
            found = targets.get(row.target_id) if row.target_id is not None else None
            entry.update(
                _target_entry(found, sensors_by_name.get(found.name, []) if found else [], now)
            )
            entry["owner_left"] = left
            entries.append(entry)
        out.append(
            {
                "space_id": str(space.id),
                "name": space.name,
                "kind": space.kind,
                "state": space.state,
                "owner": {"issuer": space.owner_issuer, "sub": space.owner_sub},
                "owner_left": left,
                "members": [
                    {
                        "issuer": m.member_issuer,
                        "sub": m.member_sub,
                        "role": m.role,
                        "state": m.state,
                    }
                    for m in current
                ],
                "assignments": entries,
            }
        )
    return {"spaces": out, "count": len(out)}

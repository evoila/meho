# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Export a Dashboard with its Sensors to a file, and import that file.

Export (:func:`export_dashboard`) reads one Dashboard by name and returns a
:class:`~meho_backplane.checks.dashboard_file_schemas.DashboardFile`. Each
Sensor's target becomes a placeholder: its target name at this site.

Import (:func:`import_dashboard`) first checks the whole file. It refuses
the file, and changes nothing, when:

* a target placeholder has no entry in the target map,
* a mapped target does not exist in the tenant,
* an operation does not exist here, or is not a safe read,
* a connector does not fit its mapped target (unless ``force`` is set).

The refusal lists every problem at once (:class:`DashboardImportRefusedError`).

Then it writes everything in one transaction. Sensors and the Dashboard are
matched by name in the tenant: a missing one is created, a different one is
updated, an equal one is left as it is. The Dashboard's members become the
Sensors of the file. A member that is not in the file leaves the Dashboard
but is not deleted. So the same file imported twice changes nothing the
second time. With ``dry_run`` the import says what it would do and writes
nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.checks.assertions import AssertionSpec
from meho_backplane.checks.connector_fit import _find_target, connector_fit, target_name
from meho_backplane.checks.dashboard_file_schemas import (
    DashboardFile,
    DashboardFileSensor,
    DashboardFileSettings,
    DashboardImportRequest,
    DashboardImportResult,
    ImportAction,
    ImportedSensor,
)
from meho_backplane.checks.dashboard_repository import create_dashboard, members_by_dashboard
from meho_backplane.checks.dashboard_schemas import NotifyMinState
from meho_backplane.checks.repository import create_sensor
from meho_backplane.checks.schemas import SensorCreate
from meho_backplane.checks.service import (
    SensorRequiresSafeOperationError,
    _is_unique_violation,
    _lock_and_require_operation,
    _require_read,
)
from meho_backplane.db.connector_usage_lock import lock_connector_usage_shared
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    CheckDashboard,
    CheckDashboardSensor,
    Sensor,
    SensorCadenceKind,
    SensorSeverity,
    SensorStatus,
)
from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id

__all__ = [
    "DashboardImportConflictError",
    "DashboardImportRefusedError",
    "ImportProblem",
    "export_dashboard",
    "import_dashboard",
]

#: The Sensor fields the file sets. An import compares these to decide
#: between "update" and "unchanged".
_SENSOR_FIELDS: tuple[str, ...] = (
    "connector_id",
    "op_id",
    "target",
    "params",
    "assertion",
    "cadence_kind",
    "interval_seconds",
    "cron_expr",
    "timezone",
    "severity",
    "for_seconds",
    "retry_times",
    "retry_backoff_seconds",
)

#: The Dashboard fields the file sets. The mail address is not one of them.
_DASHBOARD_FIELDS: tuple[str, ...] = ("description", "notify_min_state", "investigator_prompt")


@dataclass(frozen=True)
class ImportProblem:
    """One reason the file cannot be imported."""

    #: Machine tag, for example ``dashboard_import_unmapped_target``.
    kind: str
    #: Where the problem is in the request body.
    loc: tuple[str, ...]
    message: str


class DashboardImportRefusedError(Exception):
    """The file cannot be imported. Nothing was written.

    The boundary maps it to 422 with one validation-error entry per problem.
    """

    def __init__(self, problems: Sequence[ImportProblem]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(p.message for p in self.problems))


class DashboardImportConflictError(Exception):
    """Another write took a name while this import ran. Nothing was written.

    The boundary maps it to 409. Running the import again is safe.
    """

    error_code = "dashboard_import_conflict"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _file_sensor(row: Sensor) -> DashboardFileSensor:
    return DashboardFileSensor(
        name=row.name,
        connector_id=row.connector_id,
        op_id=row.op_id,
        target=target_name(row.target),
        params=dict(row.params),
        assertion=AssertionSpec.model_validate(row.assertion),
        cadence_kind=SensorCadenceKind(row.cadence_kind),
        interval_seconds=row.interval_seconds,
        cron_expr=row.cron_expr,
        timezone=row.timezone,
        severity=SensorSeverity(row.severity),
        for_seconds=row.for_seconds,
        retry_times=row.retry_times,
        retry_backoff_seconds=row.retry_backoff_seconds,
    )


async def _dashboard_by_name(
    session: AsyncSession, tenant_id: uuid.UUID, name: str, *, for_update: bool = False
) -> CheckDashboard | None:
    stmt = select(CheckDashboard).where(
        CheckDashboard.tenant_id == tenant_id, CheckDashboard.name == name
    )
    if for_update:
        stmt = stmt.with_for_update()
    row: CheckDashboard | None = await session.scalar(stmt)
    return row


async def export_dashboard(tenant_id: uuid.UUID, name: str) -> DashboardFile | None:
    """Return the file of the Dashboard *name* in *tenant_id*; ``None`` when absent."""
    async with get_sessionmaker()() as session:
        row = await _dashboard_by_name(session, tenant_id, name)
        if row is None:
            return None
        sensors = (await members_by_dashboard(session, dashboard_ids=[row.id])).get(row.id, [])
        return DashboardFile(
            dashboard=DashboardFileSettings(
                name=row.name,
                description=row.description,
                notify_min_state=cast("NotifyMinState", row.notify_min_state),
                investigator_prompt=row.investigator_prompt,
            ),
            sensors=[_file_sensor(s) for s in sensors],
        )


# ---------------------------------------------------------------------------
# Import: check the whole file first
# ---------------------------------------------------------------------------


def _mapping_problems(request: DashboardImportRequest) -> list[ImportProblem]:
    return [
        ImportProblem(
            kind="dashboard_import_unmapped_target",
            loc=("body", "target_map", placeholder),
            message=(
                f"target {placeholder!r} is not mapped; "
                f"add --map-target {placeholder}=<target at this site>"
            ),
        )
        for placeholder in request.file.placeholders()
        if placeholder not in request.target_map
    ]


async def _find_mapped_targets(
    tenant_id: uuid.UUID, request: DashboardImportRequest
) -> tuple[dict[str, Any], list[ImportProblem]]:
    """Look up each mapped target the file uses; report the ones not found."""
    found: dict[str, Any] = {}
    problems: list[ImportProblem] = []
    async with get_sessionmaker()() as session:
        for placeholder in request.file.placeholders():
            name = request.target_map.get(placeholder)
            if name is None:
                continue
            row = await _find_target(session, tenant_id, name)
            if row is None:
                problems.append(
                    ImportProblem(
                        kind="dashboard_import_target_not_found",
                        loc=("body", "target_map", placeholder),
                        message=(
                            f"target {name!r} (for {placeholder!r}) is not found in this tenant"
                        ),
                    )
                )
            else:
                found[placeholder] = row
    return found, problems


async def _operation_problem(
    tenant_id: uuid.UUID, index: int, sensor: DashboardFileSensor, body: SensorCreate
) -> ImportProblem | None:
    """The problem with *sensor*'s operation, or ``None`` when it is a safe read here."""
    product, version, impl_id = parse_connector_id(sensor.connector_id)
    descriptor = await lookup_descriptor(
        tenant_id=tenant_id, product=product, version=version, impl_id=impl_id, op_id=sensor.op_id
    )
    loc = ("body", "file", "sensors", str(index), "op_id")
    if descriptor is None:
        return ImportProblem(
            kind="dashboard_import_unknown_operation",
            loc=loc,
            message=(
                f"sensor {sensor.name!r}: operation {sensor.op_id!r} of connector "
                f"{sensor.connector_id!r} is not found here"
            ),
        )
    try:
        _require_read(body, descriptor)
    except SensorRequiresSafeOperationError as exc:
        return ImportProblem(
            kind="dashboard_import_unsafe_operation",
            loc=loc,
            message=f"sensor {sensor.name!r}: {exc}",
        )
    return None


async def _check_file(tenant_id: uuid.UUID, request: DashboardImportRequest) -> list[SensorCreate]:
    """Check the whole file; return the create body of each Sensor.

    Raises:
        DashboardImportRefusedError: with every problem found.
    """
    problems = _mapping_problems(request)
    targets, target_problems = await _find_mapped_targets(tenant_id, request)
    problems.extend(target_problems)
    bodies: list[SensorCreate] = []
    for index, sensor in enumerate(request.file.sensors):
        mapped = None if sensor.target is None else request.target_map.get(sensor.target)
        body = sensor.to_create(target_name=mapped)
        bodies.append(body)
        problem = await _operation_problem(tenant_id, index, sensor, body)
        if problem is not None:
            problems.append(problem)
            continue
        target_row = targets.get(sensor.target) if sensor.target is not None else None
        if target_row is None or request.force:
            continue
        warning = connector_fit(sensor.connector_id, target_row).warning
        if warning is not None:
            problems.append(
                ImportProblem(
                    kind="dashboard_import_connector_mismatch",
                    loc=("body", "file", "sensors", str(index), "connector_id"),
                    message=f"sensor {sensor.name!r}: {warning} Set force to import it anyway.",
                )
            )
    if problems:
        raise DashboardImportRefusedError(problems)
    return bodies


# ---------------------------------------------------------------------------
# Import: compare and write
# ---------------------------------------------------------------------------


def _wanted_sensor(body: SensorCreate) -> dict[str, Any]:
    return body.model_dump(mode="json", include=set(_SENSOR_FIELDS))


def _current_sensor(row: Sensor) -> dict[str, Any]:
    try:
        assertion: Any = AssertionSpec.model_validate(row.assertion).model_dump(mode="json")
    except ValidationError:
        assertion = row.assertion
    current = {field: getattr(row, field) for field in _SENSOR_FIELDS}
    current["assertion"] = assertion
    return current


def _changed(current: dict[str, Any], wanted: dict[str, Any]) -> list[str]:
    return [field for field in wanted if current[field] != wanted[field]]


def _update_sensor(row: Sensor, body: SensorCreate, now: datetime) -> None:
    """Write the file's settings onto *row* and make the Sensor due at once.

    Same as a Sensor edit: the waiting state change measured with the old
    settings is dropped, and a Sensor the runner paused is active again.
    """
    for field, value in _wanted_sensor(body).items():
        setattr(row, field, value)
    row.pending_state = None
    row.pending_count = 0
    row.status = SensorStatus.ACTIVE.value
    row.status_reason = None
    row.next_fire_at = now


async def _sync_sensors(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    created_by_sub: str,
    bodies: list[SensorCreate],
    *,
    write: bool,
    now: datetime,
) -> tuple[list[ImportedSensor], dict[str, uuid.UUID | None]]:
    """Create or update each Sensor; return what happened and each Sensor's id."""
    names = [b.name for b in bodies]
    query = select(Sensor).where(Sensor.tenant_id == tenant_id, Sensor.name.in_(names))
    if write:
        query = query.with_for_update()
    existing = {row.name: row for row in (await session.scalars(query)).all()}
    report: list[ImportedSensor] = []
    ids: dict[str, uuid.UUID | None] = {}
    for body in bodies:
        row = existing.get(body.name)
        if row is None:
            report.append(ImportedSensor(name=body.name, action="create"))
            ids[body.name] = None
            if write:
                await _lock_and_require_operation(session, tenant_id, body)
                created = await create_sensor(
                    session,
                    tenant_id=tenant_id,
                    name=body.name,
                    connector_id=body.connector_id,
                    op_id=body.op_id,
                    target=body.target,
                    params=body.params,
                    assertion=body.assertion.model_dump(mode="json"),
                    cadence_kind=body.cadence_kind,
                    interval_seconds=body.interval_seconds,
                    cron_expr=body.cron_expr,
                    timezone=body.timezone,
                    severity=body.severity.value,
                    for_seconds=body.for_seconds,
                    retry_times=body.retry_times,
                    retry_backoff_seconds=body.retry_backoff_seconds,
                    identity_sub=body.identity_sub,
                    created_by_sub=created_by_sub,
                    base=now,
                )
                ids[body.name] = created.id
            continue
        ids[body.name] = row.id
        changed = _changed(_current_sensor(row), _wanted_sensor(body))
        action: ImportAction = "update" if changed else "unchanged"
        report.append(ImportedSensor(name=body.name, action=action, changed_fields=changed))
        if write and changed:
            await _lock_and_require_operation(session, tenant_id, body)
            _update_sensor(row, body, now)
    return report, ids


async def _sync_dashboard(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    created_by_sub: str,
    settings: DashboardFileSettings,
    sensor_ids: dict[str, uuid.UUID | None],
    *,
    write: bool,
    now: datetime,
) -> tuple[uuid.UUID | None, ImportAction, list[str], list[str], list[str]]:
    """Create or update the Dashboard and set its members to the file's Sensors.

    Returns the id, the action, the changed fields, and the Sensor names
    that join and leave.
    """
    row = await _dashboard_by_name(session, tenant_id, settings.name, for_update=write)
    wanted = settings.model_dump(include=set(_DASHBOARD_FIELDS))
    if row is None:
        dashboard_id = None
        if write:
            created = await create_dashboard(
                session,
                tenant_id=tenant_id,
                name=settings.name,
                description=settings.description,
                sensor_ids=[sid for sid in sensor_ids.values() if sid is not None],
                created_by_sub=created_by_sub,
                notify_min_state=settings.notify_min_state,
                investigator_prompt=settings.investigator_prompt,
            )
            dashboard_id = created.id
        return dashboard_id, "create", [], sorted(sensor_ids), []
    members = (await members_by_dashboard(session, dashboard_ids=[row.id])).get(row.id, [])
    current_names = {s.name: s.id for s in members}
    added = sorted(name for name in sensor_ids if name not in current_names)
    removed = sorted(name for name in current_names if name not in sensor_ids)
    changed = [field for field in _DASHBOARD_FIELDS if getattr(row, field) != wanted[field]]
    if write and (changed or added or removed):
        for field in changed:
            setattr(row, field, wanted[field])
        row.updated_at = now
        if removed:
            await session.execute(
                delete(CheckDashboardSensor).where(
                    CheckDashboardSensor.dashboard_id == row.id,
                    CheckDashboardSensor.sensor_id.in_([current_names[n] for n in removed]),
                )
            )
        for name in added:
            sid = sensor_ids[name]
            assert sid is not None  # a written import has an id for every Sensor
            session.add(CheckDashboardSensor(dashboard_id=row.id, sensor_id=sid))
    action: ImportAction = "update" if (changed or added or removed) else "unchanged"
    return row.id, action, changed, added, removed


async def import_dashboard(
    tenant_id: uuid.UUID,
    created_by_sub: str,
    request: DashboardImportRequest,
    *,
    now: datetime | None = None,
) -> DashboardImportResult:
    """Import *request.file* into *tenant_id*; see the module text for the rules.

    Raises:
        DashboardImportRefusedError: the file cannot be imported (nothing written).
        DashboardImportConflictError: a name was taken by another write
            while the import ran (nothing written).
        SensorOperationNotFoundError: an operation was removed while the
            import ran (nothing written).
    """
    now = now or datetime.now(UTC)
    bodies = await _check_file(tenant_id, request)
    write = not request.dry_run
    async with get_sessionmaker()() as session:
        if write:
            await lock_connector_usage_shared(session)
        try:
            sensors, ids = await _sync_sensors(
                session, tenant_id, created_by_sub, bodies, write=write, now=now
            )
            if write:
                await session.flush()
            dashboard_id, action, changed_fields, added, removed = await _sync_dashboard(
                session,
                tenant_id,
                created_by_sub,
                request.file.dashboard,
                ids,
                write=write,
                now=now,
            )
            if write:
                await session.commit()
            else:
                await session.rollback()
        except IntegrityError as exc:
            await session.rollback()
            if _is_unique_violation(exc):
                raise DashboardImportConflictError(
                    "a sensor or dashboard name was taken while the import ran"
                ) from exc
            raise
    changed = action != "unchanged" or any(s.action != "unchanged" for s in sensors)
    return DashboardImportResult(
        dry_run=request.dry_run,
        changed=changed,
        dashboard_id=dashboard_id,
        dashboard_name=request.file.dashboard.name,
        dashboard_action=action,
        dashboard_changed_fields=changed_fields,
        sensors=sensors,
        members_added=added,
        members_removed=removed,
    )

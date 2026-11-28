# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Edit a Sensor in place (``PATCH /api/v1/sensors/{id}``).

An edit changes some fields of a Sensor and keeps the rest. The id, the
result history and the Dashboard memberships stay, because the row is
updated, not deleted and created again.

The edit is checked the same way as a create: the changed Sensor must be a
valid :class:`~meho_backplane.checks.schemas.SensorCreate`, and its
operation must exist and be a safe read. After the change the Sensor is due
at once, so the runner checks it on its next tick.

The function returns the before and after values of each changed field.
The route writes them into the audit row of the request.

A Sensor runs under its ``identity_sub``. When an edit changes what the
Sensor runs (its connector, operation, params or check) and that identity
is another person, not the editor and not the Sensor's own identity
``__sensor__``, MEHO sets it to ``__sensor__``. The before and after
values show this change too. So a Sensor never runs under a person who did
not choose its operation. The Dashboard file import follows the same rule
(:func:`identity_after_change`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from meho_backplane.checks.assertions import AssertionSpec
from meho_backplane.checks.runner_binding import require_runner_can_run
from meho_backplane.checks.schemas import (
    _CONNECTOR_ID_MAX_LENGTH,
    _CRON_EXPR_MAX_LENGTH,
    _INTERVAL_SECONDS_MAX,
    _INTERVAL_SECONDS_MIN,
    _NAME_MAX_LENGTH,
    _OP_ID_MAX_LENGTH,
    _TIMEZONE_MAX_LENGTH,
    SensorCreate,
    SensorParams,
    SensorRead,
)
from meho_backplane.checks.sensor_pause import set_sensor_status
from meho_backplane.checks.service import (
    SENSOR_IDENTITY_SENTINEL,
    SensorNameConflictError,
    SensorOperationNotFoundError,
    _is_unique_violation,
    _lock_and_require_operation,
    _require_read,
)
from meho_backplane.db.connector_usage_lock import lock_connector_usage_shared
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Sensor, SensorCadenceKind, SensorSeverity, SensorStatus
from meho_backplane.input_refusal import FixedMessageError, refusal_lines
from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id

__all__ = [
    "SensorEditInvalidError",
    "SensorEditResult",
    "SensorUpdate",
    "edit_sensor",
    "identity_after_change",
]

#: The Sensor fields an edit can change, in a stable order for the audit row.
_EDITABLE_FIELDS: tuple[str, ...] = (
    "name",
    "connector_id",
    "op_id",
    "params",
    "assertion",
    "cadence_kind",
    "interval_seconds",
    "cron_expr",
    "timezone",
    "severity",
)

#: The fields that decide what a Sensor runs. Changing one of them resets an
#: identity that belongs to another person (:func:`identity_after_change`).
_RUN_FIELDS: frozenset[str] = frozenset({"connector_id", "op_id", "target", "params", "assertion"})


class SensorUpdate(BaseModel):
    """Request body for ``PATCH /api/v1/sensors/{sensor_id}``.

    Every field is optional. A field that is left out, or sent as ``null``,
    keeps its value. At least one field must be set.

    To change the cadence kind, send ``cadence_kind`` with its matching
    field: ``interval_seconds`` for ``interval``, ``cron_expr`` (and
    optionally ``timezone``) for ``cron``. The field of the old kind is
    cleared.

    ``status`` (``paused`` or ``active``) pauses or resumes the Sensor. It is
    sent alone, without other fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=_NAME_MAX_LENGTH)] | None = None
    connector_id: (
        Annotated[str, Field(min_length=1, max_length=_CONNECTOR_ID_MAX_LENGTH)] | None
    ) = None
    op_id: Annotated[str, Field(min_length=1, max_length=_OP_ID_MAX_LENGTH)] | None = None
    params: SensorParams | None = None
    assertion: AssertionSpec | None = None
    cadence_kind: SensorCadenceKind | None = None
    interval_seconds: (
        Annotated[int, Field(ge=_INTERVAL_SECONDS_MIN, le=_INTERVAL_SECONDS_MAX)] | None
    ) = None
    cron_expr: Annotated[str, Field(min_length=1, max_length=_CRON_EXPR_MAX_LENGTH)] | None = None
    timezone: Annotated[str, Field(min_length=1, max_length=_TIMEZONE_MAX_LENGTH)] | None = None
    severity: SensorSeverity | None = None
    status: Literal["active", "paused"] | None = None

    @model_validator(mode="after")
    def _require_a_change(self) -> SensorUpdate:
        changes = self.model_dump(exclude_none=True)
        if not changes:
            raise FixedMessageError("set at least one field to change")
        if "status" in changes and len(changes) > 1:
            raise ValueError("status cannot be changed together with other fields")
        return self


class SensorEditInvalidError(Exception):
    """Raised when the Sensor after the edit is not a valid Sensor.

    For example a cron cadence without a cron expression. The boundary
    maps it to 422 ``sensor_edit_invalid``.
    """

    error_code = "sensor_edit_invalid"

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass(frozen=True)
class SensorEditResult:
    """The Sensor after the edit, and the changed fields before and after."""

    sensor: SensorRead
    before: dict[str, Any]
    after: dict[str, Any]


def _normalised_assertion(stored: dict[str, object]) -> dict[str, object]:
    """The stored assertion in the form a create would store it.

    So an assertion that was not changed is not reported as changed only
    because it was stored in an older form. A stored value that no longer
    parses is returned as it is.
    """
    try:
        return AssertionSpec.model_validate(stored).model_dump(mode="json")
    except ValidationError:
        return stored


def _current_values(row: Sensor) -> dict[str, Any]:
    """The editable fields of *row* in their wire form."""
    return {
        "name": row.name,
        "connector_id": row.connector_id,
        "op_id": row.op_id,
        "params": row.params,
        "assertion": _normalised_assertion(row.assertion),
        "cadence_kind": row.cadence_kind,
        "interval_seconds": row.interval_seconds,
        "cron_expr": row.cron_expr,
        "timezone": row.timezone,
        "severity": row.severity,
    }


def _merge(row: Sensor, patch: SensorUpdate) -> SensorCreate:
    """Apply *patch* to *row* and check the result like a create body."""
    changes = patch.model_dump(mode="json", exclude_none=True)
    merged: dict[str, Any] = {
        **_current_values(row),
        "target": row.target,
        "for_seconds": row.for_seconds,
        "retry_times": row.retry_times,
        "retry_backoff_seconds": row.retry_backoff_seconds,
        "identity_sub": row.identity_sub,
    }
    if changes.get("cadence_kind") == SensorCadenceKind.INTERVAL.value:
        merged["cron_expr"] = None
    elif changes.get("cadence_kind") == SensorCadenceKind.CRON.value:
        merged["interval_seconds"] = None
    merged.update(changes)
    try:
        return SensorCreate.model_validate(merged)
    except ValidationError as exc:
        raise SensorEditInvalidError(refusal_lines(exc)) from exc


def _changed_fields(row: Sensor, merged: SensorCreate) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the before and after values of every field the edit changes."""
    current = _current_values(row)
    new = merged.model_dump(mode="json", include=set(_EDITABLE_FIELDS))
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for field in _EDITABLE_FIELDS:
        if current[field] != new[field]:
            before[field] = current[field]
            after[field] = new[field]
    return before, after


def identity_after_change(identity_sub: str, editor_sub: str, changed: Iterable[str]) -> str:
    """The ``identity_sub`` a Sensor runs as after a change of the fields *changed*.

    ``__sensor__`` when the change touches a field of :data:`_RUN_FIELDS`
    (connector, operation, target, params or check) and *identity_sub* is
    neither ``__sensor__`` nor *editor_sub*. Else *identity_sub* stays.
    Every route that changes these fields of a Sensor uses this rule: the
    edit (:func:`edit_sensor`) and the Dashboard file import.
    """
    if _RUN_FIELDS.isdisjoint(changed) or identity_sub in (
        SENSOR_IDENTITY_SENTINEL,
        editor_sub,
    ):
        return identity_sub
    return SENSOR_IDENTITY_SENTINEL


async def _require_safe_read(
    tenant_id: uuid.UUID, merged: SensorCreate, runner_name: str | None
) -> None:
    """Refuse an operation that is unknown, not safe, or a write.

    For a Sensor bound to a runner, also refuse an operation the runner
    cannot run unattended (:func:`require_runner_can_run`).
    """
    product, version, impl_id = parse_connector_id(merged.connector_id)
    descriptor = await lookup_descriptor(
        tenant_id=tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=merged.op_id,
    )
    if descriptor is None:
        raise SensorOperationNotFoundError(merged.connector_id, merged.op_id)
    _require_read(merged, descriptor)
    if runner_name is not None:
        require_runner_can_run(descriptor, merged.op_id, merged.params)


def _apply(row: Sensor, merged: SensorCreate, now: datetime) -> None:
    """Write *merged* onto *row* and make the Sensor due at once.

    The waiting state change of the old check (``pending_state``) is
    dropped, because it was measured with the old settings. A Sensor the
    runner paused for a broken cadence is active again: the new cadence
    passed the same check as a create. A Sensor an operator paused, or the
    runner paused after refused logins, stays paused until it is resumed.
    """
    row.name = merged.name
    row.connector_id = merged.connector_id
    row.op_id = merged.op_id
    row.params = merged.params
    row.assertion = merged.assertion.model_dump(mode="json")
    row.cadence_kind = merged.cadence_kind.value
    row.interval_seconds = merged.interval_seconds
    row.cron_expr = merged.cron_expr
    row.timezone = merged.timezone
    row.severity = merged.severity.value
    row.pending_state = None
    row.pending_count = 0
    if (row.status_reason or "").startswith("invalid_cadence"):
        row.status = SensorStatus.ACTIVE.value
        row.status_reason = None
    row.next_fire_at = now


async def edit_sensor(
    tenant_id: uuid.UUID,
    sensor_id: uuid.UUID,
    patch: SensorUpdate,
    *,
    editor_sub: str,
    now: datetime | None = None,
) -> SensorEditResult | None:
    """Change the fields in *patch* on one Sensor of *tenant_id*.

    *editor_sub* is the ``sub`` of the person who sends the edit. When the
    edit changes what the Sensor runs and the Sensor's ``identity_sub`` is
    another person, the identity becomes ``__sensor__``
    (:func:`identity_after_change`).

    Returns ``None`` when the Sensor does not exist in the tenant (the
    boundary answers 404). When nothing changes, the row is not written and
    the Sensor is not run again.

    Raises:
        SensorEditInvalidError: the Sensor after the edit is not valid.
        SensorOperationNotFoundError: the operation does not exist.
        SensorRequiresSafeOperationError: the operation is not a safe read.
        SensorNameConflictError: the new name is taken in the tenant.
    """
    now = now or datetime.now(UTC)
    if patch.status is not None:
        changed = await set_sensor_status(tenant_id, sensor_id, SensorStatus(patch.status), now=now)
        if changed is None:
            return None
        return SensorEditResult(changed.sensor, changed.before, changed.after)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await lock_connector_usage_shared(session)
        row = await session.scalar(
            select(Sensor)
            .where(Sensor.tenant_id == tenant_id, Sensor.id == sensor_id)
            .with_for_update()
        )
        if row is None:
            return None
        merged = _merge(row, patch)
        before, after = _changed_fields(row, merged)
        if not after:
            return SensorEditResult(SensorRead.model_validate(row, from_attributes=True), {}, {})
        await _require_safe_read(tenant_id, merged, row.runner_name)
        await _lock_and_require_operation(session, tenant_id, merged)
        identity_sub = identity_after_change(row.identity_sub, editor_sub, after)
        if identity_sub != row.identity_sub:
            before["identity_sub"] = row.identity_sub
            after["identity_sub"] = identity_sub
        _apply(row, merged, now)
        row.identity_sub = identity_sub
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if _is_unique_violation(exc):
                raise SensorNameConflictError(merged.name) from exc
            raise
        await session.refresh(row)
        return SensorEditResult(SensorRead.model_validate(row, from_attributes=True), before, after)

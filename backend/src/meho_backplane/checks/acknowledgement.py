# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Acknowledge a Sensor finding: who, until when, and why (#4128).

A person looks at a Sensor that is not green and says "we know". They give
an end time, a reason from a fixed set and a short text. MEHO records who.
While the acknowledgement is active:

* the in-band advisory says "acknowledged" instead of the warning
  (:func:`acknowledged_dashboards`, read by :mod:`.advisory`);
* the warning mails for this Sensor are not sent again
  (:func:`all_acknowledged`, read by :mod:`.notify`);
* the Dashboard detail shows who, until when and why next to the real state
  (:func:`active_acknowledgements`, read by :mod:`.dashboard_service`).

The Sensor keeps running, and its real state stays visible everywhere.

An acknowledgement ends in two ways, each with an audit row written in the
same transaction as the Sensor result that ends it
(:func:`settle_acknowledgement`, called by ``record_sensor_result``):

* at its end time (``expired``). It stops counting at that instant on every
  read; the row and the audit row are written at the next result of the
  Sensor;
* early, when the Sensor's state gets worse than the state that was
  acknowledged (``state_worse``), for example ``degraded`` to ``critical``.

The create audit row is the request's own row (``sensor.acknowledge``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

import structlog
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.checks.assertions import CheckState
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    SENSOR_ACK_STATES,
    AuditEventClass,
    AuditLog,
    CheckDashboardSensor,
    Sensor,
    SensorAcknowledgement,
)

__all__ = [
    "MAX_ACK_DURATION",
    "SensorAckAlreadyOpenError",
    "SensorAckEndTimeError",
    "SensorAckNotFailingError",
    "SensorAcknowledgementCreate",
    "SensorAcknowledgementRead",
    "acknowledge_sensor",
    "acknowledged_dashboards",
    "active_acknowledgements",
    "all_acknowledged",
    "settle_acknowledgement",
]

_log = structlog.get_logger(__name__)

#: The longest time one acknowledgement may last. A finding that stays
#: accepted for longer needs a new decision, not a silent forever.
MAX_ACK_DURATION: Final[timedelta] = timedelta(days=90)

#: Synthetic ``operator_sub`` on the expiry and early-end audit rows.
ACK_SYSTEM_SUB: Final[str] = "system:checks-acknowledgement"

#: ``path`` of the audit rows MEHO writes when an acknowledgement ends.
ACK_EXPIRE_PATH: Final[str] = "sensor.acknowledgement.expire"
ACK_END_PATH: Final[str] = "sensor.acknowledgement.end"

#: How bad a state is, for "did it get worse". ``unknown`` counts like
#: ``degraded``, as in the rollup and the mail rule.
_RANK: Final[dict[str, int]] = {
    "ok": 0,
    "skip": 0,
    "unknown": 1,
    "degraded": 1,
    "critical": 2,
}

#: Link-row memo states that count as "not green" on a Dashboard.
_NON_GREEN_MEMBER_STATES: Final[tuple[str, ...]] = ("degraded", "critical")

AckReason = Literal["accepted_risk", "fix_planned", "false_alarm"]


class SensorAckNotFailingError(Exception):
    """The Sensor is green or was never checked, so there is nothing to acknowledge."""

    error_code = "sensor_ack_not_failing"


class SensorAckEndTimeError(Exception):
    """The end time is in the past or too far away."""

    error_code = "sensor_ack_invalid_end_time"


class SensorAckAlreadyOpenError(Exception):
    """The Sensor already has an active acknowledgement."""

    error_code = "sensor_ack_already_open"


class SensorAcknowledgementCreate(BaseModel):
    """Body of ``POST /api/v1/sensors/{id}/acknowledgement``.

    ``expires_at`` is required and must carry a time zone: an acknowledgement
    without an end time is refused with 422.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    expires_at: AwareDatetime
    reason: AckReason
    note: str = Field(min_length=1, max_length=500)

    @field_validator("note")
    @classmethod
    def _note_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("note must not be blank")
        return stripped


class SensorAcknowledgementRead(BaseModel):
    """One acknowledgement as REST, the CLI and the Dashboard detail show it."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    sensor_id: uuid.UUID
    acked_state: CheckState
    reason: AckReason
    note: str
    created_by_sub: str
    created_at: datetime
    expires_at: datetime
    ended_at: datetime | None = None
    end_reason: Literal["expired", "state_worse"] | None = None


def _as_utc(value: datetime) -> datetime:
    """Read a naive stored time (the SQLite test driver) as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _to_read(row: SensorAcknowledgement) -> SensorAcknowledgementRead:
    return SensorAcknowledgementRead.model_validate(
        {
            "id": row.id,
            "sensor_id": row.sensor_id,
            "acked_state": row.acked_state,
            "reason": row.reason,
            "note": row.note,
            "created_by_sub": row.created_by_sub,
            "created_at": _as_utc(row.created_at),
            "expires_at": _as_utc(row.expires_at),
            "ended_at": _as_utc(row.ended_at) if row.ended_at is not None else None,
            "end_reason": row.end_reason,
        }
    )


async def _open_row(session: AsyncSession, sensor_id: uuid.UUID) -> SensorAcknowledgement | None:
    """The Sensor's acknowledgement that has not ended yet, if any."""
    row: SensorAcknowledgement | None = await session.scalar(
        select(SensorAcknowledgement).where(
            SensorAcknowledgement.sensor_id == sensor_id,
            SensorAcknowledgement.ended_at.is_(None),
        )
    )
    return row


def _end(
    session: AsyncSession,
    sensor: Sensor,
    ack: SensorAcknowledgement,
    *,
    ended_at: datetime,
    end_reason: Literal["expired", "state_worse"],
) -> None:
    """End *ack* and add its audit row to the same transaction."""
    ack.ended_at = ended_at
    ack.end_reason = end_reason
    session.add(
        AuditLog(
            id=uuid.uuid4(),
            occurred_at=datetime.now(UTC),
            operator_sub=ACK_SYSTEM_SUB,
            tenant_id=sensor.tenant_id,
            event_class=AuditEventClass.OPERATION_EXECUTION,
            method="INTERNAL",
            path=ACK_EXPIRE_PATH if end_reason == "expired" else ACK_END_PATH,
            status_code=200,
            duration_ms=Decimal("0"),
            payload={
                "sensor_id": str(sensor.id),
                "sensor_name": sensor.name,
                "acknowledgement_id": str(ack.id),
                "acknowledged_by": ack.created_by_sub,
                "reason": ack.reason,
                "acked_state": ack.acked_state,
                "state": sensor.last_state,
                "expires_at": _as_utc(ack.expires_at).isoformat(),
                "end_reason": end_reason,
            },
        )
    )
    _log.info(
        "sensor_acknowledgement_ended",
        sensor_id=str(sensor.id),
        acknowledgement_id=str(ack.id),
        end_reason=end_reason,
    )


async def settle_acknowledgement(session: AsyncSession, sensor: Sensor, now: datetime) -> None:
    """End the Sensor's open acknowledgement when its time is up or its state got worse.

    Called by ``record_sensor_result`` after the new state is committed to
    the row, inside its transaction, so the end and its audit row commit
    with the result that caused them. Expiry wins over a worse state: an
    acknowledgement past its end time is ``expired``, at its end time.
    """
    ack = await _open_row(session, sensor.id)
    if ack is None:
        return
    if _as_utc(ack.expires_at) <= _as_utc(now):
        _end(session, sensor, ack, ended_at=ack.expires_at, end_reason="expired")
    elif _RANK.get(sensor.last_state, 0) > _RANK[ack.acked_state]:
        _end(session, sensor, ack, ended_at=now, end_reason="state_worse")


async def active_acknowledgements(
    session: AsyncSession,
    sensor_ids: Iterable[uuid.UUID],
    now: datetime,
) -> dict[uuid.UUID, SensorAcknowledgementRead]:
    """The active acknowledgement of each listed Sensor, keyed by Sensor id.

    Active means not ended and the end time is still in the future, so an
    acknowledgement stops counting at its end time even before the Sensor's
    next result writes the end.
    """
    ids = list(dict.fromkeys(sensor_ids))
    if not ids:
        return {}
    rows = await session.scalars(
        select(SensorAcknowledgement).where(
            SensorAcknowledgement.sensor_id.in_(ids),
            SensorAcknowledgement.ended_at.is_(None),
        )
    )
    return {row.sensor_id: _to_read(row) for row in rows if _as_utc(row.expires_at) > _as_utc(now)}


async def all_acknowledged(sensor_ids: Sequence[uuid.UUID], now: datetime) -> bool:
    """True when the list is not empty and every Sensor in it is acknowledged.

    The mail path asks this before a warning mail. It opens its own session.
    """
    if not sensor_ids:
        return False
    async with get_sessionmaker()() as session:
        active = await active_acknowledgements(session, sensor_ids, now)
    return all(sid in active for sid in sensor_ids)


async def acknowledged_dashboards(
    session: AsyncSession,
    dashboard_ids: Sequence[uuid.UUID],
    now: datetime,
) -> dict[uuid.UUID, datetime]:
    """Dashboards whose every non-green member is acknowledged.

    Reads the member state each Dashboard last counted
    (``check_dashboard_sensors.last_member_state``). A Dashboard with no
    non-green member on record is not in the answer, so it keeps its normal
    warning. The value is the earliest end time of those acknowledgements:
    the moment the warning can come back.
    """
    if not dashboard_ids:
        return {}
    links = (
        await session.execute(
            select(CheckDashboardSensor.dashboard_id, CheckDashboardSensor.sensor_id).where(
                CheckDashboardSensor.dashboard_id.in_(dashboard_ids),
                CheckDashboardSensor.last_member_state.in_(_NON_GREEN_MEMBER_STATES),
            )
        )
    ).all()
    if not links:
        return {}
    active = await active_acknowledgements(session, (sid for _d, sid in links), now)
    members: dict[uuid.UUID, list[uuid.UUID]] = {}
    for dashboard_id, sensor_id in links:
        members.setdefault(dashboard_id, []).append(sensor_id)
    return {
        dashboard_id: min(active[sid].expires_at for sid in sensor_ids)
        for dashboard_id, sensor_ids in members.items()
        if all(sid in active for sid in sensor_ids)
    }


async def acknowledge_sensor(
    *,
    tenant_id: uuid.UUID,
    sensor_id: uuid.UUID,
    created_by_sub: str,
    payload: SensorAcknowledgementCreate,
    now: datetime | None = None,
) -> SensorAcknowledgementRead | None:
    """Acknowledge one Sensor of *tenant_id*. ``None`` when it is not in the tenant.

    Raises:
        SensorAckEndTimeError: the end time is not in the future, or is
            more than :data:`MAX_ACK_DURATION` away.
        SensorAckNotFailingError: the Sensor is green or was never checked.
        SensorAckAlreadyOpenError: the Sensor already has an active one.
    """
    now = now or datetime.now(UTC)
    expires_at = payload.expires_at.astimezone(UTC)
    async with get_sessionmaker()() as session:
        sensor = await session.scalar(
            select(Sensor)
            .where(Sensor.id == sensor_id, Sensor.tenant_id == tenant_id)
            .with_for_update()
        )
        if sensor is None:
            return None
        if expires_at <= now or expires_at > now + MAX_ACK_DURATION:
            raise SensorAckEndTimeError(
                f"expires_at must be in the future and at most {MAX_ACK_DURATION.days} days away"
            )
        if sensor.last_evaluated_at is None or sensor.last_state not in SENSOR_ACK_STATES:
            raise SensorAckNotFailingError(sensor.last_state)
        # An open row past its end time is ended here first, so it does not
        # block the new one.
        await settle_acknowledgement(session, sensor, now)
        if await _open_row(session, sensor.id) is not None:
            raise SensorAckAlreadyOpenError(str(sensor.id))
        row = SensorAcknowledgement(
            id=uuid.uuid4(),
            sensor_id=sensor.id,
            acked_state=sensor.last_state,
            reason=payload.reason,
            note=payload.note,
            created_by_sub=created_by_sub,
            created_at=now,
            expires_at=expires_at,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            # Two people acknowledged at the same moment: the partial unique
            # index lets one win.
            await session.rollback()
            raise SensorAckAlreadyOpenError(str(sensor_id)) from exc
        return _to_read(row)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Mail a warning when the runner pauses a Sensor after refused logins.

A paused Sensor stops checking its target, so the operator must learn about
the pause. The runner calls :func:`schedule_sensor_parked_notification` after
the pause commits. The mail goes to the ``notify_email`` of every Dashboard
that holds the Sensor, through the same path as the other check mails
(:mod:`meho_backplane.checks.notify`): each address is checked against
``MAIL_RECIPIENT_ALLOWLIST``, and each attempt is stored on the Dashboard and
in the audit log (:func:`~meho_backplane.checks.mail_result.record_mail_result`).

The mail does not depend on ``CHECKS_AGENT_EVENTS``. The reason it names is
``target_credential_stale``. Like the other check mails, it never raises.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import structlog
from sqlalchemy import select

from meho_backplane.checks.events import target_name
from meho_backplane.checks.mail_result import AlertMail, record_mail_result
from meho_backplane.checks.notify import _NOTIFICATIONS, _allowed_recipients
from meho_backplane.connectors.mail.transport import send_email
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import CheckDashboard, CheckDashboardSensor

__all__ = [
    "PARK_MAIL_REASON",
    "ParkNotice",
    "notify_sensor_parked",
    "schedule_sensor_parked_notification",
]

#: The reason the warning mail names.
PARK_MAIL_REASON: Final[str] = "target_credential_stale"


@dataclass(frozen=True, slots=True)
class ParkNotice:
    """The Sensor the runner paused, detached from the runner's session."""

    tenant_id: uuid.UUID
    sensor_id: uuid.UUID
    sensor_name: str
    target: dict[str, object] | None
    connector_id: str
    op_id: str
    parked_at: datetime


@dataclass(frozen=True, slots=True)
class _Dashboard:
    id: uuid.UUID
    name: str
    state: str
    notify_email: str


def _log() -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(__name__)
    return logger


def _subject(notice: ParkNotice) -> str:
    name = " ".join(notice.sensor_name.split())
    return f"[MEHO] sensor paused ({PARK_MAIL_REASON}): {name}"


def _body(notice: ParkNotice, dashboard: _Dashboard) -> str:
    target = target_name(notice.target)
    lines = [
        f"Dashboard: {dashboard.name}",
        f"Sensor: {notice.sensor_name} ({notice.sensor_id})",
        f"Target: {target if target is not None else '-'}",
        f"Operation: {notice.connector_id} {notice.op_id}",
        f"Reason: {PARK_MAIL_REASON}",
        f"Paused at: {notice.parked_at.isoformat()}",
        "",
        "The target refused the login several times in a row, so MEHO paused",
        "this Sensor. It no longer checks the target. Fix the credential, then",
        f"run: meho sensor resume {notice.sensor_id}",
    ]
    return "\n".join(lines) + "\n"


async def _dashboards(notice: ParkNotice) -> list[_Dashboard]:
    """The Dashboards of the tenant that hold the Sensor and have a recipient."""
    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(
                select(CheckDashboard)
                .join(
                    CheckDashboardSensor,
                    CheckDashboardSensor.dashboard_id == CheckDashboard.id,
                )
                .where(
                    CheckDashboardSensor.sensor_id == notice.sensor_id,
                    CheckDashboard.tenant_id == notice.tenant_id,
                    CheckDashboard.notify_email.is_not(None),
                )
                .order_by(CheckDashboard.name)
            )
        ).all()
        return [
            _Dashboard(row.id, row.name, row.last_rollup_state or "unknown", row.notify_email or "")
            for row in rows
        ]


async def _mail_one(notice: ParkNotice, dashboard: _Dashboard) -> None:
    """Send the warning to one Dashboard's recipients and record the attempt."""
    log_fields = {"dashboard_id": str(dashboard.id), "sensor_id": str(notice.sensor_id)}
    recipients = _allowed_recipients(dashboard.notify_email, **log_fields)
    configured = [part.strip() for part in dashboard.notify_email.split(",")]
    mail = AlertMail(
        dashboard_name=dashboard.name,
        previous_state=dashboard.state,
        current_state=dashboard.state,
        subject=_subject(notice),
        to=tuple(recipients),
        refused=tuple(entry for entry in configured if entry and entry not in recipients),
        sensor={
            "id": str(notice.sensor_id),
            "name": notice.sensor_name,
            "status": "paused",
            "reason": PARK_MAIL_REASON,
        },
    )
    sent, reason = False, "no_allowed_recipients"
    if recipients:
        try:
            result = await send_email(
                to=recipients, subject=mail.subject, body=_body(notice, dashboard)
            )
            sent, reason = result.sent, result.reason or "unknown"
        except Exception:
            _log().warning("checks_park_mail_failed", exc_info=True, **log_fields)
            reason = "unexpected_error"
    if sent:
        _log().info("checks_park_mail_sent", recipient_count=len(recipients), **log_fields)
    else:
        _log().warning("checks_park_mail_not_sent", reason=reason, **log_fields)
    await record_mail_result(
        tenant_id=notice.tenant_id,
        dashboard_id=dashboard.id,
        mail=mail,
        sent=sent,
        reason=None if sent else reason,
    )


async def notify_sensor_parked(notice: ParkNotice) -> None:
    """Mail the pause warning to every Dashboard that holds the Sensor. Never raises."""
    try:
        dashboards = await _dashboards(notice)
        if not dashboards:
            _log().warning(
                "checks_park_mail_no_recipient",
                sensor_id=str(notice.sensor_id),
                reason=PARK_MAIL_REASON,
            )
        for dashboard in dashboards:
            await _mail_one(notice, dashboard)
    except Exception:
        _log().warning("checks_park_mail_failed", sensor_id=str(notice.sensor_id), exc_info=True)


def schedule_sensor_parked_notification(notice: ParkNotice) -> None:
    """Send the pause warning in a tracked background task.

    The task joins the task set of :mod:`meho_backplane.checks.notify`, so
    shutdown and the test drain handle it like every other check mail.
    """
    task = asyncio.create_task(
        notify_sensor_parked(notice),
        name=f"checks-park-mail-{notice.sensor_id}",
    )
    _NOTIFICATIONS.add(task)
    task.add_done_callback(_NOTIFICATIONS.discard)

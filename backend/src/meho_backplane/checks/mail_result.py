# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Remember the result of a Dashboard's alert mail.

Before this module, a failed alert mail left one warning log line and
nothing else. An operator could not see that mails were failing without
reading the logs. Now each attempt does three things:

* it stores the result on the Dashboard row (``last_mail_at``,
  ``last_mail_sent``, ``last_mail_reason``), so ``meho dashboard show`` and
  the console can show it;
* it writes one audit row (``op_id`` :data:`ALERT_MAIL_OP_ID`), so
  ``meho audit query --op-id checks.alert_mail`` lists every attempt, sent
  or failed, with the Dashboard, its old and new state, the result and the
  recipients. The recipients are the plain addresses, as on a ``mail.send``
  record; the mail body is never stored;
* it adds one to the ``checks_alert_mail_total`` counter, labelled by
  ``result``: ``sent``, or the reason code of the failure
  (``smtp_connect_error``, ``smtp_auth_error``, ``unexpected_error``, ...).

The Dashboard row and the audit row are written in one transaction.

Only the mail to the Dashboard's recipients is recorded. A mail that was
skipped on purpose (no recipient set, below the threshold, held back by the
flap window) is not an attempt and is not recorded.

:func:`record_mail_result` never raises. A broken database must not turn a
failed mail into a second failure on the notify path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

import structlog
from prometheus_client import Counter
from sqlalchemy import update

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog, CheckDashboard

__all__ = [
    "ALERT_MAIL_OP_ID",
    "ALERT_MAIL_SYSTEM_SUB",
    "ALERT_MAIL_TOTAL",
    "SENT_RESULT",
    "AlertMail",
    "record_mail_result",
]

#: The ``result`` label value of a delivered mail.
SENT_RESULT: Final[str] = "sent"

#: Alert-mail attempts by result. The label set is small and closed: the
#: transport's stable reason codes plus ``unexpected_error`` and
#: ``no_allowed_recipients``, so it cannot grow without bound.
ALERT_MAIL_TOTAL: Counter = Counter(
    "checks_alert_mail_total",
    "Dashboard alert mail attempts by result (sent, or the failure reason).",
    labelnames=("result",),
)

#: The reason column is short by design; a transport reason code is far
#: shorter than this.
_MAX_REASON_CHARS: Final[int] = 100

#: ``op_id`` (and ``path``) of the audit row of one alert mail attempt.
ALERT_MAIL_OP_ID: Final[str] = "checks.alert_mail"

#: Synthetic ``operator_sub`` of that row: MEHO sent the mail, not a person.
ALERT_MAIL_SYSTEM_SUB: Final[str] = "system:checks-notify"

#: ``status_code`` of the row: ``200`` when the mail was sent, ``502`` when it
#: was not, so ``meho audit query --result-status error`` finds the failures.
_SENT_STATUS: Final[int] = 200
_FAILED_STATUS: Final[int] = 502


@dataclass(frozen=True, slots=True)
class AlertMail:
    """What one alert mail attempt was about, for its audit row.

    :attr:`to` are the recipients the mail was addressed to (the allowlisted
    entries of ``notify_email``); :attr:`refused` are the entries the
    allowlist dropped. :attr:`sensor` is set on a "new problem" or
    "acknowledgement ended" mail: the member Sensor and its own edge.
    """

    dashboard_name: str
    previous_state: str
    current_state: str
    subject: str
    to: tuple[str, ...]
    refused: tuple[str, ...] = ()
    sensor: dict[str, Any] | None = None


def _audit_row(
    *,
    tenant_id: uuid.UUID,
    dashboard_id: uuid.UUID,
    mail: AlertMail,
    sent: bool,
    result: str,
    now: datetime,
) -> AuditLog:
    return AuditLog(
        id=uuid.uuid4(),
        occurred_at=now,
        operator_sub=ALERT_MAIL_SYSTEM_SUB,
        tenant_id=tenant_id,
        event_class=AuditEventClass.OPERATION_EXECUTION,
        method="INTERNAL",
        path=ALERT_MAIL_OP_ID,
        status_code=_SENT_STATUS if sent else _FAILED_STATUS,
        duration_ms=Decimal("0"),
        payload={
            "op_id": ALERT_MAIL_OP_ID,
            "dashboard_id": str(dashboard_id),
            "dashboard_name": mail.dashboard_name,
            "previous_state": mail.previous_state,
            "current_state": mail.current_state,
            "sensor": mail.sensor,
            "sent": sent,
            "result": result,
            "to": list(mail.to),
            "refused": list(mail.refused),
            "subject": mail.subject,
        },
    )


async def record_mail_result(
    *,
    tenant_id: uuid.UUID,
    dashboard_id: uuid.UUID,
    mail: AlertMail,
    sent: bool,
    reason: str | None,
) -> None:
    """Count one alert-mail attempt, store it on the Dashboard row and audit it."""
    result = SENT_RESULT if sent else (reason or "unknown")[:_MAX_REASON_CHARS]
    ALERT_MAIL_TOTAL.labels(result=result).inc()
    now = datetime.now(UTC)
    try:
        async with get_sessionmaker()() as session:
            await session.execute(
                update(CheckDashboard)
                .where(
                    CheckDashboard.id == dashboard_id,
                    CheckDashboard.tenant_id == tenant_id,
                )
                .values(
                    last_mail_at=now,
                    last_mail_sent=sent,
                    last_mail_reason=None if sent else result,
                )
            )
            session.add(
                _audit_row(
                    tenant_id=tenant_id,
                    dashboard_id=dashboard_id,
                    mail=mail,
                    sent=sent,
                    result=result,
                    now=now,
                )
            )
            await session.commit()
    except Exception:
        structlog.get_logger(__name__).warning(
            "checks_mail_result_record_failed",
            dashboard_id=str(dashboard_id),
            exc_info=True,
        )

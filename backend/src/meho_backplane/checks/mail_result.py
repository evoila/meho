# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Remember the result of a Dashboard's alert mail.

Before this module, a failed alert mail left one warning log line and
nothing else. An operator could not see that mails were failing without
reading the logs. Now each attempt does two things:

* it stores the result on the Dashboard row (``last_mail_at``,
  ``last_mail_sent``, ``last_mail_reason``), so ``meho dashboard show`` and
  the console can show it;
* it adds one to the ``checks_alert_mail_total`` counter, labelled by
  ``result``: ``sent``, or the reason code of the failure
  (``smtp_connect_error``, ``smtp_auth_error``, ``unexpected_error``, ...).

Only the mail to the Dashboard's recipients is recorded. A mail that was
skipped on purpose (no recipient set, below the threshold, held back by the
flap window) is not an attempt and is not recorded.

:func:`record_mail_result` never raises. A broken database must not turn a
failed mail into a second failure on the notify path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

import structlog
from prometheus_client import Counter
from sqlalchemy import update

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import CheckDashboard

__all__ = ["ALERT_MAIL_TOTAL", "SENT_RESULT", "record_mail_result"]

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


async def record_mail_result(
    *,
    tenant_id: uuid.UUID,
    dashboard_id: uuid.UUID,
    sent: bool,
    reason: str | None,
) -> None:
    """Count one alert-mail attempt and store it on the Dashboard row."""
    result = SENT_RESULT if sent else (reason or "unknown")[:_MAX_REASON_CHARS]
    ALERT_MAIL_TOTAL.labels(result=result).inc()
    try:
        async with get_sessionmaker()() as session:
            await session.execute(
                update(CheckDashboard)
                .where(
                    CheckDashboard.id == dashboard_id,
                    CheckDashboard.tenant_id == tenant_id,
                )
                .values(
                    last_mail_at=datetime.now(UTC),
                    last_mail_sent=sent,
                    last_mail_reason=None if sent else result,
                )
            )
            await session.commit()
    except Exception:
        structlog.get_logger(__name__).warning(
            "checks_mail_result_record_failed",
            dashboard_id=str(dashboard_id),
            exc_info=True,
        )

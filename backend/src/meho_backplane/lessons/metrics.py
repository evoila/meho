# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The four learning numbers of ``meho audit reflex``.

They answer one question: do stored lessons help? Each number comes with
its count and its base, and reads ``None`` when the base is 0.

Let a *session* be one ``agent_session_id``. An operation *failed* when
its ``DISPATCH`` audit row has a status of 400 or more, other than 429
(a rate limit is not a mistake). An operation *succeeded* when the status
is 200.

1. **Repeat-mistake rate** -- of the failed operations in the window that
   belong to a session, the share where the same operation (``op_id``)
   on the same target already failed in another, earlier session (looked
   back up to :data:`REPEAT_LOOKBACK_DAYS` days). A lesson should stop a
   mistake from happening twice.
2. **Hit rate** -- of the entries searches gave to sessions in the window,
   the share that were used: the session ran an operation that succeeded
   after it (:func:`~meho_backplane.lessons.retrievals.find_uses`).
3. **Precision** -- of the retrieved entries that still exist, the share
   that were right. An entry was wrong when its ``status`` was or is now
   ``disputed``. It was stale when it had expired, or its ``status`` was
   ``replaced`` or ``retired``, at the moment of the search. Everything
   else counts as right. A deleted entry cannot be judged and is left out.
4. **Time to the first correct action on a known system** -- for each
   session in the window whose first successful operation ran on a known
   target, the seconds from the session's start to that operation. A
   target is known when an operation on it succeeded in another, earlier
   session. The session starts at its first audit row, usage event or
   retrieval. The number is the median.
"""

from __future__ import annotations

import statistics
import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import AuditLog, Document, RequestUsageEvent
from meho_backplane.lessons.retrievals import (
    DISPATCH_METHOD,
    SUCCESS_STATUS,
    Retrieval,
    aware,
    fetch_retrievals,
    find_uses,
)

__all__ = [
    "REPEAT_LOOKBACK_DAYS",
    "LearningMetrics",
    "compute_learning_metrics",
]

#: How far back before the window a failure can make a later one a repeat.
REPEAT_LOOKBACK_DAYS: Final[int] = 90

#: Statuses that make a retrieved entry stale when it had them at the search.
_STALE_STATUSES: Final[frozenset[str]] = frozenset({"replaced", "retired"})
_WRONG_STATUS: Final[str] = "disputed"
_RATE_LIMITED: Final[int] = 429

_SessionKey = tuple[uuid.UUID, uuid.UUID | None]


class LearningMetrics(BaseModel):
    """The four numbers that show whether stored lessons help.

    For one tenant and window. Each ratio comes with its count and its
    base, and is ``null`` when the base is 0. The median time is in
    seconds.
    """

    model_config = ConfigDict(frozen=True)

    repeat_mistake_pct: float | None
    repeat_mistake_failures: int
    repeat_mistake_repeats: int

    hit_rate_pct: float | None
    hit_rate_retrievals: int
    hit_rate_used: int

    precision_pct: float | None
    precision_judged: int
    precision_right: int

    first_correct_action_median_seconds: float | None
    first_correct_action_sessions: int


def _pct(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator * 100.0, 2)


def _is_failure(status_code: int) -> bool:
    return status_code >= 400 and status_code != _RATE_LIMITED


async def compute_learning_metrics(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime,
    tenant_id: uuid.UUID | None,
) -> LearningMetrics:
    """Compute the four numbers; ``tenant_id=None`` means every tenant. Read-only."""
    retrievals = await fetch_retrievals(session, since=since, until=until, tenant_id=tenant_id)
    uses = await find_uses(session, retrievals, until=until)
    judged, right = await _precision(session, retrievals)
    failures, repeats = await _repeat_mistakes(
        session, since=since, until=until, tenant_id=tenant_id
    )
    samples = await _first_correct_action_seconds(
        session, since=since, until=until, tenant_id=tenant_id, retrievals=retrievals
    )
    return LearningMetrics(
        repeat_mistake_pct=_pct(repeats, failures),
        repeat_mistake_failures=failures,
        repeat_mistake_repeats=repeats,
        hit_rate_pct=_pct(len(uses), len(retrievals)),
        hit_rate_retrievals=len(retrievals),
        hit_rate_used=len(uses),
        precision_pct=_pct(right, judged),
        precision_judged=judged,
        precision_right=right,
        first_correct_action_median_seconds=(
            round(statistics.median(samples), 2) if samples else None
        ),
        first_correct_action_sessions=len(samples),
    )


async def _precision(session: AsyncSession, retrievals: list[Retrieval]) -> tuple[int, int]:
    """Return ``(judged, right)`` over *retrievals* whose entry still exists."""
    if not retrievals:
        return 0, 0
    current: dict[uuid.UUID, Any] = {
        row.id: row.doc_metadata
        for row in (
            await session.execute(
                select(Document.id, Document.doc_metadata).where(
                    Document.id.in_({r.entry_id for r in retrievals})
                )
            )
        ).all()
    }
    judged = right = 0
    for retrieval in retrievals:
        if retrieval.entry_id not in current:
            continue
        judged += 1
        now_status = (current[retrieval.entry_id] or {}).get("status")
        wrong = _WRONG_STATUS in (retrieval.status, now_status)
        stale = retrieval.expired or retrieval.status in _STALE_STATUSES
        if not wrong and not stale:
            right += 1
    return judged, right


def _dispatch_rows(
    *,
    since: datetime,
    until: datetime,
    tenant_id: uuid.UUID | None,
) -> Any:
    stmt = select(
        AuditLog.agent_session_id,
        AuditLog.tenant_id,
        AuditLog.path,
        AuditLog.target_id,
        AuditLog.status_code,
        AuditLog.occurred_at,
    ).where(
        AuditLog.method == DISPATCH_METHOD,
        AuditLog.occurred_at >= since,
        AuditLog.occurred_at <= until,
        AuditLog.agent_session_id.is_not(None),
    )
    if tenant_id is not None:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
    return stmt


async def _repeat_mistakes(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime,
    tenant_id: uuid.UUID | None,
) -> tuple[int, int]:
    """Return ``(failures in the window, repeats among them)``."""
    lookback = since - timedelta(days=REPEAT_LOOKBACK_DAYS)
    rows = (
        await session.execute(_dispatch_rows(since=lookback, until=until, tenant_id=tenant_id))
    ).all()
    # (tenant, op, target) -> session -> first failure time
    first_failure: dict[tuple[Any, str, Any], dict[uuid.UUID, datetime]] = defaultdict(dict)
    in_window: list[tuple[tuple[Any, str, Any], uuid.UUID, datetime]] = []
    for row in rows:
        if not _is_failure(row.status_code):
            continue
        key = (row.tenant_id, row.path, row.target_id)
        at = aware(row.occurred_at)
        held = first_failure[key].get(row.agent_session_id)
        if held is None or at < held:
            first_failure[key][row.agent_session_id] = at
        if at >= since:
            in_window.append((key, row.agent_session_id, at))
    repeats = sum(
        1
        for key, session_id, at in in_window
        if any(other != session_id and first < at for other, first in first_failure[key].items())
    )
    return len(in_window), repeats


async def _first_correct_action_seconds(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime,
    tenant_id: uuid.UUID | None,
    retrievals: Iterable[Retrieval],
) -> list[float]:
    """Return one sample per session whose first success ran on a known target."""
    rows = (
        await session.execute(_dispatch_rows(since=since, until=until, tenant_id=tenant_id))
    ).all()
    first_success: dict[_SessionKey, tuple[datetime, uuid.UUID | None]] = {}
    for row in rows:
        if row.status_code != SUCCESS_STATUS:
            continue
        key = (row.agent_session_id, row.tenant_id)
        at = aware(row.occurred_at)
        if key not in first_success or at < first_success[key][0]:
            first_success[key] = (at, row.target_id)
    candidates = {key: value for key, value in first_success.items() if value[1] is not None}
    if not candidates:
        return []
    known_since = await _target_first_success_by_session(
        session, {target for _, target in candidates.values()}, until=until
    )
    starts = await _session_starts(session, {sid for sid, _ in candidates}, retrievals)
    samples: list[float] = []
    for (session_id, tenant), (at, target) in candidates.items():
        earlier = known_since.get((tenant, target), {})
        start = starts.get(session_id, at)
        if any(other != session_id and first < start for other, first in earlier.items()):
            samples.append(max(0.0, (at - min(start, at)).total_seconds()))
    return samples


async def _target_first_success_by_session(
    session: AsyncSession,
    targets: set[Any],
    *,
    until: datetime,
) -> dict[tuple[Any, Any], dict[uuid.UUID, datetime]]:
    """Return ``(tenant, target) -> session -> first successful operation time``."""
    stmt = (
        select(
            AuditLog.tenant_id,
            AuditLog.target_id,
            AuditLog.agent_session_id,
            func.min(AuditLog.occurred_at),
        )
        .where(
            AuditLog.method == DISPATCH_METHOD,
            AuditLog.status_code == SUCCESS_STATUS,
            AuditLog.target_id.in_(targets),
            AuditLog.agent_session_id.is_not(None),
            AuditLog.occurred_at <= until,
        )
        .group_by(AuditLog.tenant_id, AuditLog.target_id, AuditLog.agent_session_id)
    )
    result: dict[tuple[Any, Any], dict[uuid.UUID, datetime]] = defaultdict(dict)
    for tenant, target, session_id, first in (await session.execute(stmt)).all():
        result[(tenant, target)][session_id] = aware(first)
    return result


async def _session_starts(
    session: AsyncSession,
    session_ids: set[uuid.UUID],
    retrievals: Iterable[Retrieval],
) -> dict[uuid.UUID, datetime]:
    """Return the earliest audit row, usage event or retrieval of each session."""
    starts: dict[uuid.UUID, datetime] = {}

    def keep(session_id: uuid.UUID, at: datetime) -> None:
        if session_id not in starts or at < starts[session_id]:
            starts[session_id] = at

    audit = (
        select(AuditLog.agent_session_id, func.min(AuditLog.occurred_at))
        .where(AuditLog.agent_session_id.in_(session_ids))
        .group_by(AuditLog.agent_session_id)
    )
    usage = (
        select(RequestUsageEvent.session_id, func.min(RequestUsageEvent.occurred_at))
        .where(RequestUsageEvent.session_id.in_(session_ids))
        .group_by(RequestUsageEvent.session_id)
    )
    for stmt in (audit, usage):
        for session_id, first in (await session.execute(stmt)).all():
            keep(session_id, aware(first))
    for retrieval in retrievals:
        if retrieval.agent_session_id in session_ids:
            keep(retrieval.agent_session_id, retrieval.retrieved_at)
    return starts

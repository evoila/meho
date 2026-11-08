# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-connector, per-day numbers from the search and call records (#4750).

:func:`compute_pick_report` reads one tenant's records in a window and
returns one :class:`OperationPickBucket` per ``(date, connector_id)``.
Searches count on the day of the search, calls on the day of the call.

"The right operation was chosen after a search" here means: the call
used an operation from the search's results and returned ``ok``
(``calls_in_results_ok``). That is a signal from real use, not a proof.

The aggregation runs in SQL with ``date()``, ``COUNT`` and ``SUM(CASE)``,
which work the same on PostgreSQL and SQLite. The caller authorizes the
tenant first; this module never reads an :class:`Operator`.
"""

from __future__ import annotations

import uuid
from datetime import date as date_type
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import OperationCallRecord, OperationSearchRecord

__all__ = ["OperationPickBucket", "OperationPickReport", "compute_pick_report"]

_S = OperationSearchRecord
_C = OperationCallRecord


class OperationPickBucket(BaseModel):
    """The numbers of one connector on one day."""

    model_config = ConfigDict(frozen=True)

    date: date_type
    connector_id: str
    searches: int = Field(description="search_operations calls.")
    searches_followed_by_call: int = Field(
        description="Searches that a later call on the same connector was linked to."
    )
    calls: int = Field(description="call_operation calls.")
    calls_in_results: int = Field(description="Calls whose op_id was in the linked search.")
    calls_at_rank_1: int = Field(description="Calls whose op_id was the first result.")
    calls_in_top_3: int = Field(description="Calls whose op_id was in the first three results.")
    calls_in_results_ok: int = Field(
        description="Calls whose op_id was in the linked search and that returned ok."
    )
    calls_not_in_results: int = Field(
        description="Calls after a search whose op_id was not in its results."
    )
    calls_unknown_op: int = Field(description="Calls to an op_id that does not exist.")
    calls_without_search: int = Field(
        description="Calls with no search on the same connector in the 5 minutes before."
    )


class OperationPickReport(BaseModel):
    """The report for one tenant and window."""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    since: datetime
    until: datetime
    connector_id: str | None
    buckets: list[OperationPickBucket]


def _count_if(condition: Any) -> Any:
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def _day(value: Any) -> date_type:
    """Turn a ``date()`` result into a date (SQLite returns text)."""
    if isinstance(value, date_type):
        return value
    return date_type.fromisoformat(str(value))


async def compute_pick_report(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
    until: datetime,
    connector_id: str | None = None,
) -> OperationPickReport:
    """Return the per-connector, per-day numbers for one tenant."""
    search_where = [_S.tenant_id == tenant_id, _S.occurred_at >= since, _S.occurred_at <= until]
    call_where = [_C.tenant_id == tenant_id, _C.occurred_at >= since, _C.occurred_at <= until]
    if connector_id is not None:
        search_where.append(_S.connector_id == connector_id)
        call_where.append(_C.connector_id == connector_id)

    search_day = func.date(_S.occurred_at)
    searches = select(search_day, _S.connector_id, func.count()).where(*search_where)
    followed = (
        select(search_day, _S.connector_id, func.count(func.distinct(_C.search_id)))
        .join(_C, _C.search_id == _S.id)
        .where(*search_where, _C.tenant_id == tenant_id)
    )
    call_day = func.date(_C.occurred_at)
    linked = _C.search_id.is_not(None)
    calls = select(
        call_day,
        _C.connector_id,
        func.count(),
        _count_if(_C.rank.is_not(None)),
        _count_if(_C.rank == 1),
        _count_if(_C.rank <= 3),
        _count_if(_C.rank.is_not(None) & (_C.outcome == "ok")),
        _count_if(linked & _C.rank.is_(None)),
        _count_if(_C.outcome == "unknown_op"),
        _count_if(_C.search_id.is_(None)),
    ).where(*call_where)

    rows: dict[tuple[date_type, str], dict[str, int]] = {}

    def _bucket(day: Any, connector: str) -> dict[str, int]:
        return rows.setdefault((_day(day), connector), {})

    for day, connector, count in await session.execute(
        searches.group_by(search_day, _S.connector_id)
    ):
        _bucket(day, connector)["searches"] = count
    for day, connector, count in await session.execute(
        followed.group_by(search_day, _S.connector_id)
    ):
        _bucket(day, connector)["searches_followed_by_call"] = count
    call_fields = (
        "calls",
        "calls_in_results",
        "calls_at_rank_1",
        "calls_in_top_3",
        "calls_in_results_ok",
        "calls_not_in_results",
        "calls_unknown_op",
        "calls_without_search",
    )
    for day, connector, *counts in await session.execute(calls.group_by(call_day, _C.connector_id)):
        _bucket(day, connector).update(zip(call_fields, (int(c) for c in counts), strict=True))

    empty = dict.fromkeys(("searches", "searches_followed_by_call", *call_fields), 0)
    buckets = [
        OperationPickBucket(date=day, connector_id=connector, **{**empty, **numbers})
        for (day, connector), numbers in sorted(rows.items())
    ]
    return OperationPickReport(
        tenant_id=tenant_id,
        since=since,
        until=until,
        connector_id=connector_id,
        buckets=buckets,
    )

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Record which entries a search gave to a session, and find the ones it used.

Recording
---------

``search_knowledge`` and ``search_memory`` call :func:`record_kb_retrievals`
and :func:`record_memory_retrievals` with the entries they return. Each
entry becomes one :class:`~meho_backplane.db.models.LessonRetrieval` row,
with the entry's header ``status`` and whether its ``expires_at`` had
passed at that moment.

* Only a call with a session id is recorded. Without a session nothing
  can show later that the entry was used.
* Recording never fails the search. An error is logged as
  ``lesson_retrieval_record_failed`` and the search answers as before.

Use
---

A retrieved entry counts as **used** when the same session later ran an
operation that succeeded: a ``DISPATCH`` audit row with status ``200`` and
the same ``agent_session_id``, at or after the retrieval. The audit row
carries the checked result of the operation, so this is the feedback
MEHO already has. :func:`find_uses` returns, per retrieval, the time and
target of the first such operation.
"""

from __future__ import annotations

import bisect
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Document, LessonRetrieval
from meho_backplane.kb.schemas import KB_SOURCE
from meho_backplane.kb.shared import visible_to
from meho_backplane.lesson_header import header_expires_at
from meho_backplane.memory._internal import MEMORY_SOURCE
from meho_backplane.operations._audit import resolve_agent_session_id

__all__ = [
    "DISPATCH_METHOD",
    "SUCCESS_STATUS",
    "Retrieval",
    "Use",
    "aware",
    "fetch_retrievals",
    "find_uses",
    "record_kb_retrievals",
    "record_memory_retrievals",
]

_log = structlog.get_logger(__name__)

#: ``method`` of the audit row the dispatcher writes for each operation.
DISPATCH_METHOD: Final[str] = "DISPATCH"
#: Status of a dispatch audit row whose operation succeeded.
SUCCESS_STATUS: Final[int] = 200


@dataclass(frozen=True)
class Retrieval:
    """One recorded retrieval, with aware datetimes."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    store: str
    entry_id: uuid.UUID
    entry_tenant_id: uuid.UUID
    agent_session_id: uuid.UUID
    retrieved_at: datetime
    status: str | None
    expired: bool


@dataclass(frozen=True)
class Use:
    """The first successful operation of the session after a retrieval."""

    occurred_at: datetime
    target_id: uuid.UUID | None


def aware(moment: datetime) -> datetime:
    """Return *moment* with a time zone; SQLite gives back UTC without one."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def record_kb_retrievals(tenant_id: uuid.UUID, slugs: Sequence[str]) -> None:
    """Record the knowledge entries *slugs* that a search of *tenant_id* returned.

    A slug resolves like a read: the tenant's own entry first, else a
    shared entry of another tenant.
    """
    if not slugs:
        return
    query = select(
        Document.id, Document.tenant_id, Document.source_id, Document.doc_metadata
    ).where(
        Document.source == KB_SOURCE,
        Document.source_id.in_(set(slugs)),
        visible_to(tenant_id),
    )
    await _record(tenant_id, KB_SOURCE, query, own_first=True)


async def record_memory_retrievals(tenant_id: uuid.UUID, entry_ids: Sequence[uuid.UUID]) -> None:
    """Record the memory entries *entry_ids* that a search of *tenant_id* returned."""
    if not entry_ids:
        return
    query = select(
        Document.id, Document.tenant_id, Document.source_id, Document.doc_metadata
    ).where(
        Document.source == MEMORY_SOURCE,
        Document.id.in_(set(entry_ids)),
        Document.tenant_id == tenant_id,
    )
    await _record(tenant_id, MEMORY_SOURCE, query, own_first=False)


async def _record(tenant_id: uuid.UUID, store: str, query: Any, *, own_first: bool) -> None:
    """Insert one row per entry *query* finds; log and swallow any error."""
    session_id = resolve_agent_session_id()
    if session_id is None:
        return
    try:
        now = datetime.now(UTC)
        async with get_sessionmaker()() as session:
            rows = (await session.execute(query)).all()
            chosen: dict[str, Any] = {}
            for row in rows:
                held = chosen.get(row.source_id)
                if held is None or (own_first and row.tenant_id == tenant_id):
                    chosen[row.source_id] = row
            for row in chosen.values():
                session.add(_retrieval_row(tenant_id, store, session_id, row, now))
            await session.commit()
    except Exception as exc:
        _log.warning(
            "lesson_retrieval_record_failed",
            tenant_id=str(tenant_id),
            store=store,
            error_type=type(exc).__name__,
        )


def _retrieval_row(
    tenant_id: uuid.UUID,
    store: str,
    session_id: uuid.UUID,
    row: Any,
    now: datetime,
) -> LessonRetrieval:
    metadata: Mapping[str, Any] = row.doc_metadata or {}
    status = metadata.get("status")
    expires_at = header_expires_at(metadata)
    return LessonRetrieval(
        tenant_id=tenant_id,
        store=store,
        entry_id=row.id,
        entry_tenant_id=row.tenant_id,
        agent_session_id=session_id,
        retrieved_at=now,
        status=status if isinstance(status, str) else None,
        expired=expires_at is not None and expires_at < now,
    )


async def fetch_retrievals(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime,
    tenant_id: uuid.UUID | None,
) -> list[Retrieval]:
    """Return the retrievals in ``[since, until]``; ``tenant_id=None`` means every tenant."""
    stmt = select(LessonRetrieval).where(
        LessonRetrieval.retrieved_at >= since,
        LessonRetrieval.retrieved_at <= until,
    )
    if tenant_id is not None:
        stmt = stmt.where(LessonRetrieval.tenant_id == tenant_id)
    return [
        Retrieval(
            id=row.id,
            tenant_id=row.tenant_id,
            store=row.store,
            entry_id=row.entry_id,
            entry_tenant_id=row.entry_tenant_id,
            agent_session_id=row.agent_session_id,
            retrieved_at=aware(row.retrieved_at),
            status=row.status,
            expired=row.expired,
        )
        for row in (await session.scalars(stmt)).all()
    ]


async def find_uses(
    session: AsyncSession,
    retrievals: Iterable[Retrieval],
    *,
    until: datetime,
) -> dict[uuid.UUID, Use]:
    """Return ``retrieval id -> first use``; a retrieval that was not used is left out."""
    retrievals = list(retrievals)
    if not retrievals:
        return {}
    sessions = {r.agent_session_id for r in retrievals}
    earliest = min(r.retrieved_at for r in retrievals)
    stmt = select(
        AuditLog.agent_session_id,
        AuditLog.tenant_id,
        AuditLog.occurred_at,
        AuditLog.target_id,
    ).where(
        AuditLog.method == DISPATCH_METHOD,
        AuditLog.status_code == SUCCESS_STATUS,
        AuditLog.agent_session_id.in_(sessions),
        AuditLog.occurred_at >= earliest,
        AuditLog.occurred_at <= until,
    )
    by_session: dict[tuple[uuid.UUID, uuid.UUID | None], list[Use]] = defaultdict(list)
    for row in (await session.execute(stmt)).all():
        by_session[(row.agent_session_id, row.tenant_id)].append(
            Use(occurred_at=aware(row.occurred_at), target_id=row.target_id)
        )
    times: dict[tuple[uuid.UUID, uuid.UUID | None], list[datetime]] = {}
    for key, uses in by_session.items():
        uses.sort(key=lambda use: use.occurred_at)
        times[key] = [use.occurred_at for use in uses]

    found: dict[uuid.UUID, Use] = {}
    for retrieval in retrievals:
        key = (retrieval.agent_session_id, retrieval.tenant_id)
        index = bisect.bisect_left(times.get(key, []), retrieval.retrieved_at)
        if index < len(times.get(key, [])):
            found[retrieval.id] = by_session[key][index]
    return found

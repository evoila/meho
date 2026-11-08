# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The weekly knowledge upkeep job and its report.

Too many old or doubled entries make the agent worse. Once per interval
(``KNOWLEDGE_UPKEEP_INTERVAL_SECONDS``, one week by default) this job:

1. **Merges duplicates.** Knowledge entries of one tenant with exactly
   the same body are one entry. Case and spaces count: ``MODE=prod`` and
   ``MODE=PROD`` say different things. The job keeps one: a ``confirmed``
   entry first, else the oldest. Each other copy gets ``status: replaced``
   and ``replaced_by: <slug of the kept entry>``. Before the change the job
   locks both entries and reads them again. When either was edited or
   deleted after the scan, it leaves both as they are.
2. **Retires expired entries.** A knowledge entry whose ``expires_at`` has
   passed gets ``status: retired``. A candidate gets an ``expires_at`` six
   months after it was learned (:mod:`meho_backplane.lesson_header`).
   Expired memory entries are deleted by the memory expiry sweeper, so
   this job does not touch them.
3. **Refreshes confirmed entries.** A ``confirmed`` knowledge or memory
   entry that a session used in the last interval gets ``confirmed_at``
   set to the time of that use
   (:func:`~meho_backplane.lessons.retrievals.find_uses`).
4. **Lists used candidates for a reviewer.** The job never confirms a
   candidate. A used candidate is listed with the number of different
   sessions and different targets that used it: copies of one story are
   not independent proof. A reviewer of the tenant (a person who may
   decide approvals) or a tenant admin sets ``status: confirmed``.
5. **Measures.** The report carries the four learning numbers
   (:mod:`meho_backplane.lessons.metrics`) for the interval.

The job changes only the metadata. It keeps every entry, so its history
stays readable. Retrievals older than :data:`RETRIEVAL_RETENTION_DAYS` are
deleted.

The report
----------

One ``INTERNAL`` audit row per tenant, ``operator_sub`` =
:data:`SYSTEM_OPERATOR_SUB`, ``path`` = :data:`UPKEEP_PATH`, with the
:class:`UpkeepReport` as payload (each list holds at most
:data:`REPORT_LIST_LIMIT` items; the counts are complete). A tenant's
changes and its report row commit in one transaction: when the report
cannot be written, the changes roll back too, and the next run makes and
reports them. One more row with the totals goes to the system tenant
:data:`UPKEEP_SYSTEM_TENANT_ID`, only when every tenant committed.
The schedule reads that row as the last run
(:func:`~meho_backplane.loop_schedule.wait_until_first_run`), so a restart
does not start the week again. On PostgreSQL an advisory lock lets only
one replica run the job at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.advisory import advisory_lock
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditEventClass,
    AuditLog,
    Document,
    LessonRetrieval,
    Tenant,
)
from meho_backplane.kb.revisions import update_head_metadata
from meho_backplane.kb.schemas import KB_SOURCE
from meho_backplane.lesson_header import header_expires_at
from meho_backplane.lessons.metrics import LearningMetrics, compute_learning_metrics
from meho_backplane.lessons.retrievals import aware, fetch_retrievals, find_uses
from meho_backplane.loop_schedule import wait_until_first_run
from meho_backplane.memory._internal import slug_from_source_id
from meho_backplane.memory.audit import INTERNAL_METHOD, write_internal_audit_row
from meho_backplane.memory.revisions import add_revision as add_memory_revision
from meho_backplane.metrics import note_loop_tick
from meho_backplane.settings import get_settings

__all__ = [
    "REPORT_LIST_LIMIT",
    "RETRIEVAL_RETENTION_DAYS",
    "SYSTEM_OPERATOR_SUB",
    "UPKEEP_PATH",
    "UPKEEP_SYSTEM_TENANT_ID",
    "UpkeepIncompleteError",
    "UpkeepReport",
    "run_upkeep",
    "start_knowledge_upkeep",
    "stop_knowledge_upkeep",
]

_log = structlog.get_logger(__name__)

SYSTEM_OPERATOR_SUB: Final[str] = "system:knowledge-upkeep"
UPKEEP_PATH: Final[str] = "knowledge.upkeep"
UPKEEP_SYSTEM_TENANT_ID: Final[uuid.UUID] = uuid.UUID("00000000-0000-0000-0000-000000005733")
#: Retrievals older than this are deleted by each run.
RETRIEVAL_RETENTION_DAYS: Final[int] = 180
#: Each list in a report holds at most this many items.
REPORT_LIST_LIMIT: Final[int] = 100

_ADVISORY_LOCK_KEY: Final[int] = 0x4D45_484F_4B55_504B  # "MEHOKUPK"
_CLOSED: Final[frozenset[str]] = frozenset({"replaced", "retired"})


class MergedEntry(BaseModel):
    """A knowledge entry marked ``replaced`` because *into* has the same body."""

    model_config = ConfigDict(frozen=True)

    slug: str
    into: str


class RefreshedEntry(BaseModel):
    """A confirmed entry whose ``confirmed_at`` the job set again."""

    model_config = ConfigDict(frozen=True)

    store: str
    slug: str
    confirmed_at: datetime


class ReviewCandidate(BaseModel):
    """A used candidate, for a reviewer to confirm or not."""

    model_config = ConfigDict(frozen=True)

    store: str
    slug: str
    sessions: int
    targets: int


class UpkeepReport(BaseModel):
    """What one run did in one tenant, and the tenant's learning numbers."""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    since: datetime
    until: datetime
    merged_count: int = 0
    retired_count: int = 0
    refreshed_count: int = 0
    for_review_count: int = 0
    merged: list[MergedEntry] = []
    retired: list[str] = []
    refreshed: list[RefreshedEntry] = []
    for_review: list[ReviewCandidate] = []
    numbers: LearningMetrics | None = None


class _Changes:
    """What a run did, per owner tenant."""

    def __init__(self) -> None:
        self.merged: dict[uuid.UUID, list[MergedEntry]] = defaultdict(list)
        self.retired: dict[uuid.UUID, list[str]] = defaultdict(list)
        self.refreshed: dict[uuid.UUID, list[RefreshedEntry]] = defaultdict(list)
        self.for_review: dict[uuid.UUID, list[ReviewCandidate]] = defaultdict(list)


class _Seen(BaseModel):
    """One entry as the scan read it, before any lock."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    source_id: str
    body: str
    metadata: dict[str, Any]


class _Plan:
    """What the scan found to do, per owner tenant. Each step checks again under a lock."""

    def __init__(self) -> None:
        self.merges: dict[uuid.UUID, list[tuple[_Seen, _Seen]]] = defaultdict(list)
        self.retires: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        self.refreshes: dict[uuid.UUID, list[tuple[Any, datetime]]] = defaultdict(list)


def _status(metadata: dict[str, Any] | None) -> object:
    return (metadata or {}).get("status")


def _slug(source: str, source_id: str) -> str:
    """Return the entry's slug; a memory ``source_id`` also carries its scope."""
    return source_id if source == KB_SOURCE else slug_from_source_id(source_id)


def _body_key(tenant_id: uuid.UUID, body: str) -> tuple[uuid.UUID, str]:
    # The exact body: a change of case or spaces can change what a lesson says.
    return tenant_id, hashlib.sha256(body.encode()).hexdigest()


async def _store_metadata(session: AsyncSession, doc: Document, metadata: dict[str, Any]) -> None:
    """Write *metadata* to *doc*; also its passages (knowledge) or a new revision (memory)."""
    if doc.source == KB_SOURCE:
        await update_head_metadata(session, doc, metadata)
    else:
        doc.doc_metadata = metadata
        doc.updated_at = datetime.now(UTC)
        await add_memory_revision(session, doc, SYSTEM_OPERATOR_SUB)


async def _change_metadata(
    session: AsyncSession,
    doc_id: uuid.UUID,
    change: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> Document | None:
    """Apply *change* to one entry's metadata under a row lock; return the row if changed.

    *change* sees the metadata as stored now and returns ``None`` when
    nothing is to be done. The caller commits.
    """
    doc = await session.scalar(select(Document).where(Document.id == doc_id).with_for_update())
    if doc is None:
        return None
    new = change(dict(doc.doc_metadata or {}))
    if new is None:
        return None
    await _store_metadata(session, doc, new)
    return doc


async def _read(stmt: Any) -> list[Any]:
    """Run one read in a short session, so no read stays open while the job writes."""
    async with get_sessionmaker()() as session:
        return list((await session.execute(stmt)).all())


async def _plan_merges(plan: _Plan) -> None:
    """Plan to mark every copy but one of each knowledge body ``replaced``."""
    rows = await _read(
        select(
            Document.id,
            Document.tenant_id,
            Document.source_id,
            Document.body,
            Document.created_at,
            Document.doc_metadata,
        ).where(Document.source == KB_SOURCE)
    )
    groups: dict[tuple[uuid.UUID, str], list[Any]] = defaultdict(list)
    for row in rows:
        if _status(row.doc_metadata) not in _CLOSED:
            groups[_body_key(row.tenant_id, row.body)].append(row)
    for copies in groups.values():
        if len(copies) < 2:
            continue
        copies.sort(
            key=lambda r: (_status(r.doc_metadata) != "confirmed", aware(r.created_at), r.source_id)
        )
        seen = [
            _Seen(id=r.id, source_id=r.source_id, body=r.body, metadata=dict(r.doc_metadata or {}))
            for r in copies
        ]
        plan.merges[copies[0].tenant_id].extend((copy, seen[0]) for copy in seen[1:])


async def _mark_replaced(session: AsyncSession, copy: _Seen, keeper: _Seen) -> bool:
    """Mark *copy* replaced by *keeper*, if neither changed since the scan.

    Both rows are locked first (in id order, so two jobs cannot wait on
    each other). An entry edited or deleted after the scan is left as it is.
    """
    docs = {
        doc.id: doc
        for doc in await session.scalars(
            select(Document)
            .where(Document.id.in_((copy.id, keeper.id)))
            .order_by(Document.id)
            .with_for_update()
        )
    }
    for seen in (copy, keeper):
        doc = docs.get(seen.id)
        if doc is None or doc.body != seen.body or dict(doc.doc_metadata or {}) != seen.metadata:
            return False
    await _store_metadata(
        session,
        docs[copy.id],
        {**copy.metadata, "status": "replaced", "replaced_by": keeper.source_id},
    )
    return True


async def _plan_retires(plan: _Plan, *, now: datetime) -> None:
    """Plan to set ``status: retired`` on knowledge entries whose ``expires_at`` has passed."""
    rows = await _read(
        select(Document.id, Document.tenant_id, Document.doc_metadata).where(
            Document.source == KB_SOURCE
        )
    )
    for row in rows:
        if _retire(dict(row.doc_metadata or {}), now) is not None:
            plan.retires[row.tenant_id].append(row.id)


def _retire(metadata: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    expires_at = header_expires_at(metadata)
    if _status(metadata) in _CLOSED or expires_at is None or expires_at >= now:
        return None
    return {**metadata, "status": "retired"}


async def _plan_refreshes_and_list_candidates(
    plan: _Plan, changes: _Changes, *, since: datetime, now: datetime
) -> None:
    """Plan ``confirmed_at`` for used confirmed entries; list used candidates."""
    async with get_sessionmaker()() as session:
        retrievals = await fetch_retrievals(session, since=since, until=now, tenant_id=None)
        uses = await find_uses(session, retrievals, until=now)
    by_entry: dict[uuid.UUID, list[tuple[uuid.UUID, Any, datetime]]] = defaultdict(list)
    for retrieval in retrievals:
        use = uses.get(retrieval.id)
        if use is not None:
            by_entry[retrieval.entry_id].append(
                (retrieval.agent_session_id, use.target_id, use.occurred_at)
            )
    if not by_entry:
        return
    docs = await _read(
        select(
            Document.id,
            Document.tenant_id,
            Document.source,
            Document.source_id,
            Document.doc_metadata,
        ).where(Document.id.in_(by_entry))
    )
    for doc in docs:
        entry_uses = by_entry[doc.id]
        status = _status(doc.doc_metadata)
        if status == "candidate":
            changes.for_review[doc.tenant_id].append(
                ReviewCandidate(
                    store=doc.source,
                    slug=_slug(doc.source, doc.source_id),
                    sessions=len({sid for sid, _, _ in entry_uses}),
                    targets=len({target for _, target, _ in entry_uses if target is not None}),
                )
            )
        elif status == "confirmed":
            plan.refreshes[doc.tenant_id].append((doc, max(at for _, _, at in entry_uses)))


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return aware(datetime.fromisoformat(value))
    except ValueError:
        return None


async def _refresh(session: AsyncSession, doc: Any, used_at: datetime, changes: _Changes) -> None:
    def refresh(metadata: dict[str, Any]) -> dict[str, Any] | None:
        if _status(metadata) != "confirmed":
            return None
        stored = _parse_date(metadata.get("confirmed_at"))
        if stored is not None and stored >= used_at:
            return None
        return {**metadata, "confirmed_at": used_at.isoformat()}

    if await _change_metadata(session, doc.id, refresh) is not None:
        changes.refreshed[doc.tenant_id].append(
            RefreshedEntry(
                store=doc.source, slug=_slug(doc.source, doc.source_id), confirmed_at=used_at
            )
        )


async def _run_tenant(
    tenant_id: uuid.UUID,
    plan: _Plan,
    changes: _Changes,
    *,
    since: datetime,
    now: datetime,
) -> UpkeepReport:
    """Make one tenant's changes and write its report in one transaction.

    When the report cannot be written, the changes roll back with it, so
    the next run finds them again and reports them then.
    """
    started = time.perf_counter()
    async with get_sessionmaker()() as session:
        for copy, keeper in plan.merges[tenant_id]:
            if await _mark_replaced(session, copy, keeper):
                changes.merged[tenant_id].append(
                    MergedEntry(slug=copy.source_id, into=keeper.source_id)
                )
        for doc_id in plan.retires[tenant_id]:
            doc = await _change_metadata(session, doc_id, lambda m: _retire(m, now))
            if doc is not None:
                changes.retired[tenant_id].append(doc.source_id)
        for doc, used_at in plan.refreshes[tenant_id]:
            await _refresh(session, doc, used_at, changes)
        report = _report(
            tenant_id,
            changes,
            since=since,
            now=now,
            numbers=await compute_learning_metrics(
                session, since=since, until=now, tenant_id=tenant_id
            ),
        )
        session.add(
            _report_row(
                tenant_id,
                report.model_dump(mode="json"),
                (time.perf_counter() - started) * 1000.0,
            )
        )
        await session.commit()
    return report


async def run_upkeep(*, now: datetime | None = None) -> dict[uuid.UUID, UpkeepReport]:
    """Run the job once over every tenant; return the report of each tenant.

    Writes each tenant's report row together with its changes. Safe to run
    twice: a second run finds nothing more to merge, retire or refresh.
    Raises :class:`UpkeepIncompleteError` after the other tenants when a
    tenant failed.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(seconds=get_settings().knowledge_upkeep_interval_seconds)
    plan = _Plan()
    changes = _Changes()
    await _plan_merges(plan)
    await _plan_retires(plan, now=now)
    await _plan_refreshes_and_list_candidates(plan, changes, since=since, now=now)
    async with get_sessionmaker()() as session:
        await session.execute(
            delete(LessonRetrieval).where(
                LessonRetrieval.retrieved_at < now - timedelta(days=RETRIEVAL_RETENTION_DAYS)
            )
        )
        await session.commit()
        tenant_ids = set((await session.scalars(select(Tenant.id))).all())
    tenant_ids |= {*plan.merges, *plan.retires, *plan.refreshes}
    reports: dict[uuid.UUID, UpkeepReport] = {}
    failed: list[uuid.UUID] = []
    for tenant_id in sorted(tenant_ids):
        try:
            reports[tenant_id] = await _run_tenant(tenant_id, plan, changes, since=since, now=now)
        except Exception:
            _log.exception("knowledge_upkeep_tenant_failed", tenant_id=str(tenant_id))
            failed.append(tenant_id)
    if failed:
        raise UpkeepIncompleteError(failed)
    return reports


class UpkeepIncompleteError(RuntimeError):
    """The changes or the report of some tenants did not commit; they rolled back."""

    def __init__(self, tenant_ids: list[uuid.UUID]) -> None:
        self.tenant_ids = tenant_ids
        super().__init__(f"knowledge upkeep failed for {len(tenant_ids)} tenant(s)")


def _report(
    tenant_id: uuid.UUID,
    changes: _Changes,
    *,
    since: datetime,
    now: datetime,
    numbers: LearningMetrics,
) -> UpkeepReport:
    limit = REPORT_LIST_LIMIT
    return UpkeepReport(
        tenant_id=tenant_id,
        since=since,
        until=now,
        merged_count=len(changes.merged[tenant_id]),
        retired_count=len(changes.retired[tenant_id]),
        refreshed_count=len(changes.refreshed[tenant_id]),
        for_review_count=len(changes.for_review[tenant_id]),
        merged=changes.merged[tenant_id][:limit],
        retired=sorted(changes.retired[tenant_id])[:limit],
        refreshed=changes.refreshed[tenant_id][:limit],
        for_review=sorted(
            changes.for_review[tenant_id], key=lambda c: (-c.sessions, -c.targets, c.slug)
        )[:limit],
        numbers=numbers,
    )


def _report_row(tenant_id: uuid.UUID, payload: dict[str, Any], duration_ms: float) -> AuditLog:
    """Build one report row, the same shape as :func:`write_internal_audit_row` writes."""
    return AuditLog(
        id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        operator_sub=SYSTEM_OPERATOR_SUB,
        tenant_id=tenant_id,
        event_class=AuditEventClass.OPERATION_EXECUTION,
        method=INTERNAL_METHOD,
        path=UPKEEP_PATH,
        status_code=200,
        request_id=None,
        duration_ms=Decimal(str(duration_ms)),
        payload=payload,
    )


async def _write_total(reports: dict[uuid.UUID, UpkeepReport], duration_ms: float) -> None:
    """Write the row with the totals: the schedule reads it as the last complete run."""
    await write_internal_audit_row(
        event_class=AuditEventClass.OPERATION_EXECUTION,
        operator_sub=SYSTEM_OPERATOR_SUB,
        tenant_id=UPKEEP_SYSTEM_TENANT_ID,
        method=INTERNAL_METHOD,
        path=UPKEEP_PATH,
        status_code=200,
        duration_ms=duration_ms,
        payload={
            "tenants": len(reports),
            "merged_count": sum(r.merged_count for r in reports.values()),
            "retired_count": sum(r.retired_count for r in reports.values()),
            "refreshed_count": sum(r.refreshed_count for r in reports.values()),
            "for_review_count": sum(r.for_review_count for r in reports.values()),
        },
    )


async def _run_one_tick() -> None:
    """Run the job once, if no other replica runs it now, and write the reports."""
    async with advisory_lock(_ADVISORY_LOCK_KEY, subsystem="knowledge_upkeep") as locked:
        if not locked:
            _log.info("knowledge_upkeep_skipped_lock_held")
            return
        started = time.perf_counter()
        reports = await run_upkeep()
        duration_ms = (time.perf_counter() - started) * 1000.0
        await _write_total(reports, duration_ms)
    _log.info(
        "knowledge_upkeep_done",
        tenants=len(reports),
        merged=sum(r.merged_count for r in reports.values()),
        retired=sum(r.retired_count for r in reports.values()),
        refreshed=sum(r.refreshed_count for r in reports.values()),
        for_review=sum(r.for_review_count for r in reports.values()),
        duration_ms=duration_ms,
    )


async def _upkeep_loop() -> None:
    """Wait until a run is due, run, then run once per interval."""
    interval = get_settings().knowledge_upkeep_interval_seconds
    await wait_until_first_run(
        job="knowledge_upkeep",
        tenant_id=UPKEEP_SYSTEM_TENANT_ID,
        operator_sub=SYSTEM_OPERATOR_SUB,
        path=UPKEEP_PATH,
        interval_seconds=interval,
    )
    while True:
        try:
            await _run_one_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("knowledge_upkeep_tick_failed", exc_info=True)
        note_loop_tick("knowledge_upkeep", get_settings().knowledge_upkeep_interval_seconds)
        await asyncio.sleep(get_settings().knowledge_upkeep_interval_seconds)


def start_knowledge_upkeep() -> asyncio.Task[None]:
    """Start the upkeep loop; the lifespan keeps the task and stops it on shutdown."""
    return asyncio.create_task(_upkeep_loop(), name="knowledge-upkeep")


async def stop_knowledge_upkeep(task: asyncio.Task[None]) -> None:
    """Cancel the upkeep loop and wait for it to end."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

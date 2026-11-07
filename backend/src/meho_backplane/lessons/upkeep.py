# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The weekly knowledge upkeep job and its report.

Too many old or doubled entries make the agent worse. Once per interval
(``KNOWLEDGE_UPKEEP_INTERVAL_SECONDS``, one week by default) this job:

1. **Merges duplicates.** Knowledge entries of one tenant with the same
   body (spaces and case do not count) are one entry. The job keeps one:
   a ``confirmed`` entry first, else the oldest. Each other copy gets
   ``status: replaced`` and ``replaced_by: <slug of the kept entry>``.
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
:data:`REPORT_LIST_LIMIT` items; the counts are complete). One more row
with the totals goes to the system tenant :data:`UPKEEP_SYSTEM_TENANT_ID`.
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
from typing import Any, Final

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select

from meho_backplane.db.advisory import advisory_lock
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, Document, LessonRetrieval, Tenant
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


def _status(metadata: dict[str, Any] | None) -> object:
    return (metadata or {}).get("status")


def _slug(source: str, source_id: str) -> str:
    """Return the entry's slug; a memory ``source_id`` also carries its scope."""
    return source_id if source == KB_SOURCE else slug_from_source_id(source_id)


def _body_key(tenant_id: uuid.UUID, body: str) -> tuple[uuid.UUID, str]:
    normalised = " ".join(body.split()).casefold()
    return tenant_id, hashlib.sha256(normalised.encode()).hexdigest()


async def _change_metadata(
    doc_id: uuid.UUID,
    change: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> Document | None:
    """Apply *change* to one entry's metadata under a row lock; return the row if changed.

    *change* sees the metadata as stored now and returns ``None`` when
    nothing is to be done. A knowledge entry gets its passages updated
    too; a memory entry gets a new revision.
    """
    async with get_sessionmaker()() as session:
        doc = await session.scalar(select(Document).where(Document.id == doc_id).with_for_update())
        if doc is None:
            return None
        new = change(dict(doc.doc_metadata or {}))
        if new is None:
            return None
        if doc.source == KB_SOURCE:
            await update_head_metadata(session, doc, new)
        else:
            doc.doc_metadata = new
            doc.updated_at = datetime.now(UTC)
            await add_memory_revision(session, doc, SYSTEM_OPERATOR_SUB)
        await session.commit()
        return doc


async def _read(stmt: Any) -> list[Any]:
    """Run one read in a short session, so no read stays open while the job writes."""
    async with get_sessionmaker()() as session:
        return list((await session.execute(stmt)).all())


async def _merge_duplicates(changes: _Changes) -> set[uuid.UUID]:
    """Mark every copy but one of each knowledge body ``replaced``; return their ids."""
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
    replaced: set[uuid.UUID] = set()
    for copies in groups.values():
        if len(copies) < 2:
            continue
        copies.sort(
            key=lambda r: (_status(r.doc_metadata) != "confirmed", aware(r.created_at), r.source_id)
        )
        keeper = copies[0].source_id
        for copy in copies[1:]:

            def mark(metadata: dict[str, Any], into: str = keeper) -> dict[str, Any] | None:
                if _status(metadata) in _CLOSED:
                    return None
                return {**metadata, "status": "replaced", "replaced_by": into}

            if await _change_metadata(copy.id, mark) is not None:
                replaced.add(copy.id)
                changes.merged[copy.tenant_id].append(MergedEntry(slug=copy.source_id, into=keeper))
    return replaced


async def _retire_expired(changes: _Changes, *, now: datetime, skip: set[uuid.UUID]) -> None:
    """Set ``status: retired`` on knowledge entries whose ``expires_at`` has passed."""
    rows = await _read(
        select(Document.id, Document.tenant_id, Document.source_id, Document.doc_metadata).where(
            Document.source == KB_SOURCE
        )
    )

    def retire(metadata: dict[str, Any]) -> dict[str, Any] | None:
        expires_at = header_expires_at(metadata)
        if _status(metadata) in _CLOSED or expires_at is None or expires_at >= now:
            return None
        return {**metadata, "status": "retired"}

    for row in rows:
        if row.id in skip or retire(dict(row.doc_metadata or {})) is None:
            continue
        if await _change_metadata(row.id, retire) is not None:
            changes.retired[row.tenant_id].append(row.source_id)


async def _refresh_and_list_candidates(
    changes: _Changes, *, since: datetime, now: datetime
) -> None:
    """Set ``confirmed_at`` on used confirmed entries; list used candidates."""
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
            await _refresh(doc, max(at for _, _, at in entry_uses), changes)


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return aware(datetime.fromisoformat(value))
    except ValueError:
        return None


async def _refresh(doc: Any, used_at: datetime, changes: _Changes) -> None:
    def refresh(metadata: dict[str, Any]) -> dict[str, Any] | None:
        if _status(metadata) != "confirmed":
            return None
        stored = _parse_date(metadata.get("confirmed_at"))
        if stored is not None and stored >= used_at:
            return None
        return {**metadata, "confirmed_at": used_at.isoformat()}

    if await _change_metadata(doc.id, refresh) is not None:
        changes.refreshed[doc.tenant_id].append(
            RefreshedEntry(
                store=doc.source, slug=_slug(doc.source, doc.source_id), confirmed_at=used_at
            )
        )


async def run_upkeep(*, now: datetime | None = None) -> dict[uuid.UUID, UpkeepReport]:
    """Run the job once over every tenant; return the report of each tenant.

    Writes the audit rows (see the module docstring). Safe to run twice:
    a second run finds nothing more to merge, retire or refresh.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(seconds=get_settings().knowledge_upkeep_interval_seconds)
    changes = _Changes()
    replaced = await _merge_duplicates(changes)
    await _retire_expired(changes, now=now, skip=replaced)
    await _refresh_and_list_candidates(changes, since=since, now=now)
    async with get_sessionmaker()() as session:
        await session.execute(
            delete(LessonRetrieval).where(
                LessonRetrieval.retrieved_at < now - timedelta(days=RETRIEVAL_RETENTION_DAYS)
            )
        )
        await session.commit()
        tenant_ids = list((await session.scalars(select(Tenant.id))).all())
        reports = {
            tenant_id: _report(
                tenant_id,
                changes,
                since=since,
                now=now,
                numbers=await compute_learning_metrics(
                    session, since=since, until=now, tenant_id=tenant_id
                ),
            )
            for tenant_id in tenant_ids
        }
    return reports


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


async def _write_reports(reports: dict[uuid.UUID, UpkeepReport], duration_ms: float) -> None:
    """Write one audit row per tenant and one with the totals; log write errors."""
    rows: list[tuple[uuid.UUID, dict[str, Any]]] = [
        (tenant_id, report.model_dump(mode="json")) for tenant_id, report in reports.items()
    ]
    rows.append(
        (
            UPKEEP_SYSTEM_TENANT_ID,
            {
                "tenants": len(reports),
                "merged_count": sum(r.merged_count for r in reports.values()),
                "retired_count": sum(r.retired_count for r in reports.values()),
                "refreshed_count": sum(r.refreshed_count for r in reports.values()),
                "for_review_count": sum(r.for_review_count for r in reports.values()),
            },
        )
    )
    for tenant_id, payload in rows:
        try:
            await write_internal_audit_row(
                event_class=AuditEventClass.OPERATION_EXECUTION,
                operator_sub=SYSTEM_OPERATOR_SUB,
                tenant_id=tenant_id,
                method=INTERNAL_METHOD,
                path=UPKEEP_PATH,
                status_code=200,
                duration_ms=duration_ms,
                payload=payload,
            )
        except Exception:
            _log.exception("knowledge_upkeep_audit_write_failed", tenant_id=str(tenant_id))


async def _run_one_tick() -> None:
    """Run the job once, if no other replica runs it now, and write the reports."""
    async with advisory_lock(_ADVISORY_LOCK_KEY, subsystem="knowledge_upkeep") as locked:
        if not locked:
            _log.info("knowledge_upkeep_skipped_lock_held")
            return
        started = time.perf_counter()
        reports = await run_upkeep()
        duration_ms = (time.perf_counter() - started) * 1000.0
        await _write_reports(reports, duration_ms)
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

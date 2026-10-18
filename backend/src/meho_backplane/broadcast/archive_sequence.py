# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-tenant commit-order sequence for the announcement archive (#3748).

The publisher calls :func:`next_announcement_seq` inside the transaction
that inserts the announcement, right before the INSERT. The UPDATE on the
tenant's :class:`~meho_backplane.db.models.AgentAnnouncementSequence` row
locks that row until the transaction ends (PostgreSQL row-level locks are
released at commit or rollback). A second writer in the same tenant waits
on the UPDATE and reads the new value only after the first one is done.
So:

* sequence numbers follow commit order, per tenant;
* a rolled-back write gives its number back (the UPDATE rolls back too),
  so committed numbers have no holes;
* a reader that saw sequence number N has seen every announcement with a
  smaller number, because those committed earlier.

The lock only orders writes. It is held for the INSERT and the commit,
not across any Valkey call, and it is not a lease or a claim.

A pod that still runs the previous release during a rolling upgrade
inserts rows without a number. :func:`number_unnumbered_rows` gives such
rows the next numbers, oldest first, under the same counter lock. Readers
call it before every scan, so an older pod's announcement shows up like a
new one instead of staying invisible.

SQLite (dev/test only) has one writer at a time anyway, so it keeps the
same order without row locks.

Reference: https://www.postgresql.org/docs/16/explicit-locking.html
(13.3.2 Row-Level Locks).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Insert

from meho_backplane.db.models import AgentAnnouncement, AgentAnnouncementSequence

__all__ = [
    "archive_watermark",
    "next_announcement_seq",
    "number_unnumbered_rows",
    "oldest_retained_seq",
]


async def _bump(session: AsyncSession, tenant_id: UUID) -> int | None:
    stmt = (
        update(AgentAnnouncementSequence)
        .where(AgentAnnouncementSequence.tenant_id == tenant_id)
        .values(last_seq=AgentAnnouncementSequence.last_seq + 1)
        .returning(AgentAnnouncementSequence.last_seq)
        .execution_options(synchronize_session=False)
    )
    value = (await session.execute(stmt)).scalar_one_or_none()
    return None if value is None else int(value)


async def _create_counter(session: AsyncSession, tenant_id: UUID) -> None:
    conn = await session.connection()
    values = {"tenant_id": tenant_id, "last_seq": 0}
    stmt: Insert
    if conn.dialect.name == "postgresql":
        stmt = pg_insert(AgentAnnouncementSequence).values(**values).on_conflict_do_nothing()
    else:
        stmt = sqlite_insert(AgentAnnouncementSequence).values(**values).on_conflict_do_nothing()
    await session.execute(stmt)


async def next_announcement_seq(session: AsyncSession, tenant_id: UUID) -> int:
    """Lock the tenant's counter row and return the next sequence number.

    Call it inside the transaction that inserts the announcement, as late
    as possible, so the lock is held only for the INSERT and the commit.
    The first announcement of a tenant creates the counter row; two first
    writers racing on it both end up on the same row (``ON CONFLICT DO
    NOTHING``), and the second one waits on the first one's lock.
    """
    seq = await _bump(session, tenant_id)
    if seq is None:
        await _create_counter(session, tenant_id)
        seq = await _bump(session, tenant_id)
    if seq is None:  # pragma: no cover - the row was just created or already existed
        raise RuntimeError("agent_announcement_sequence row missing after create")
    return seq


async def _lock_counter(session: AsyncSession, tenant_id: UUID) -> None:
    """Lock the tenant's counter row for the rest of the transaction.

    ``SELECT ... FOR UPDATE`` on PostgreSQL; SQLite drops the clause and
    has one writer at a time anyway. Creates the row when the tenant has
    none yet.
    """
    stmt = (
        select(AgentAnnouncementSequence.tenant_id)
        .where(AgentAnnouncementSequence.tenant_id == tenant_id)
        .with_for_update()
    )
    if (await session.execute(stmt)).scalar_one_or_none() is None:
        await _create_counter(session, tenant_id)
        await session.execute(stmt)


async def _unnumbered_ids(session: AsyncSession, tenant_id: UUID) -> list[UUID]:
    """Ids of the tenant's rows without a number, oldest first."""
    stmt = (
        select(AgentAnnouncement.id)
        .where(AgentAnnouncement.tenant_id == tenant_id, AgentAnnouncement.seq.is_(None))
        .order_by(AgentAnnouncement.created_at, AgentAnnouncement.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def number_unnumbered_rows(session: AsyncSession, tenant_id: UUID) -> int:
    """Give rows written without a number the tenant's next numbers.

    A pod on the previous release writes ``seq = NULL`` during a rolling
    upgrade. This numbers such rows oldest first (``created_at``, then
    ``id``) so they take their place at the end of the order, like
    announcements that commit now. The counter row is locked first, so a
    reader numbering rows, a second reader, and a publisher take turns and
    no number is handed out twice. The row list is read again under the
    lock, because another reader may have numbered the rows meanwhile.

    The caller commits. Returns how many rows got a number; the common
    case after the upgrade is ``0`` after one cheap indexed read.
    """
    if not await _unnumbered_ids(session, tenant_id):
        return 0
    await _lock_counter(session, tenant_id)
    ids = await _unnumbered_ids(session, tenant_id)
    for row_id in ids:
        seq = await _bump(session, tenant_id)
        await session.execute(
            update(AgentAnnouncement)
            .where(AgentAnnouncement.id == row_id)
            .values(seq=seq)
            .execution_options(synchronize_session=False)
        )
    return len(ids)


async def archive_watermark(session: AsyncSession, tenant_id: UUID) -> int:
    """Return the sequence number of the newest committed announcement (0 if none)."""
    value = (
        await session.execute(
            select(AgentAnnouncementSequence.last_seq).where(
                AgentAnnouncementSequence.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    return 0 if value is None else int(value)


async def oldest_retained_seq(session: AsyncSession, tenant_id: UUID) -> int | None:
    """Return the smallest sequence number still in the archive, or ``None``."""
    value = (
        await session.execute(
            select(func.min(AgentAnnouncement.seq)).where(AgentAnnouncement.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    return None if value is None else int(value)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retained revisions of memory entries.

A memory entry is one ``documents`` row (the head). Each write that
changes the body or the metadata also stores one ``memory_revision``
row, so an older version can still be read. A write that changes
nothing stores no revision.

:func:`write_entry` writes the head and its revision in one
transaction, so a failed write leaves neither. Writes to the same entry
are ordered: on PostgreSQL the transaction first locks the head row
(``SELECT ... FOR UPDATE``), so a second writer waits until the first
commits and then numbers its revision after it. SQLite has no row lock;
it lets one writer at a time hold the database. A first write has no
row to lock; the unique natural key on ``documents`` and the unique
``(entry_id, revision_number)`` index make a clashing write fail, and
:func:`write_entry` runs it again.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document, MemoryRevision
from meho_backplane.memory._internal import MEMORY_SOURCE
from meho_backplane.retrieval.indexer import index_document

__all__ = [
    "MemoryEntryRevision",
    "add_revision",
    "delete_revisions",
    "list_revisions",
    "write_entry",
]

#: How many times a write runs when another write to the same entry clashes with it.
_ATTEMPTS: Final[int] = 3


class MemoryEntryRevision(BaseModel):
    """One retained version of a memory entry, newest has the highest number."""

    model_config = ConfigDict(frozen=True)

    revision_number: int
    body: str
    metadata: dict[str, Any]
    written_by_sub: str | None
    created_at: datetime


async def write_entry(
    *,
    tenant_id: uuid.UUID,
    source_id: str,
    kind: str,
    body: str,
    metadata: dict[str, Any],
    written_by_sub: str | None,
) -> Document:
    """Write the memory head and its revision in one transaction; return the head.

    A write that clashes with another write to the same entry runs again,
    up to :data:`_ATTEMPTS` times; then the last error propagates.
    """
    sessionmaker = get_sessionmaker()
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            async with sessionmaker() as session:
                await _lock_head(session, tenant_id, source_id)
                doc = await index_document(
                    tenant_id=tenant_id,
                    source=MEMORY_SOURCE,
                    source_id=source_id,
                    kind=kind,
                    body=body,
                    metadata=metadata,
                    session=session,
                )
                await add_revision(session, doc, written_by_sub)
                await session.commit()
                return doc
        except IntegrityError:
            if attempt == _ATTEMPTS:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


async def add_revision(session: AsyncSession, doc: Document, written_by_sub: str | None) -> bool:
    """Add *doc* as the next revision of its entry to *session*. Return whether one was added.

    Nothing is added when the newest revision already has this body and
    these metadata. The caller commits.
    """
    metadata = dict(doc.doc_metadata or {})
    newest = (
        await session.execute(
            select(MemoryRevision)
            .where(MemoryRevision.entry_id == doc.id)
            .order_by(MemoryRevision.revision_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if newest is not None and newest.body == doc.body and newest.doc_metadata == metadata:
        return False
    session.add(
        MemoryRevision(
            tenant_id=doc.tenant_id,
            entry_id=doc.id,
            revision_number=1 if newest is None else newest.revision_number + 1,
            body=doc.body,
            doc_metadata=metadata,
            written_by_sub=written_by_sub,
        )
    )
    await session.flush()
    return True


async def _lock_head(session: AsyncSession, tenant_id: uuid.UUID, source_id: str) -> None:
    """Lock the entry's head row on PostgreSQL until this transaction ends."""
    stmt = select(Document.id).where(
        Document.tenant_id == tenant_id,
        Document.source == MEMORY_SOURCE,
        Document.source_id == source_id,
    )
    conn = await session.connection()
    if conn.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    await session.execute(stmt)


async def list_revisions(tenant_id: uuid.UUID, entry_id: uuid.UUID) -> list[MemoryEntryRevision]:
    """Return every revision of *entry_id* in *tenant_id*, oldest first."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(MemoryRevision)
                    .where(
                        MemoryRevision.entry_id == entry_id,
                        MemoryRevision.tenant_id == tenant_id,
                    )
                    .order_by(MemoryRevision.revision_number)
                )
            )
            .scalars()
            .all()
        )
    return [
        MemoryEntryRevision(
            revision_number=row.revision_number,
            body=row.body,
            metadata=dict(row.doc_metadata),
            written_by_sub=row.written_by_sub,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def delete_revisions(entry_ids: list[uuid.UUID]) -> None:
    """Delete the revisions of the given entries (the caller deletes the entries)."""
    if not entry_ids:
        return
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(delete(MemoryRevision).where(MemoryRevision.entry_id.in_(entry_ids)))
        await session.commit()

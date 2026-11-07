# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retained revisions of memory entries.

A memory entry is one ``documents`` row (the head). Each write that
changes the body or the metadata also stores one ``memory_revision``
row, so an older version can still be read. A write that changes
nothing stores no revision.

The revision is stored right after the head is written, in its own
transaction. When storing it fails, the write call raises; the head
already holds the new version and the next write that changes something
stores the missing revision.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document, MemoryRevision

__all__ = [
    "MemoryEntryRevision",
    "delete_revisions",
    "list_revisions",
    "record_revision",
]

#: How often a write tries again when another write took its revision number.
_ATTEMPTS: Final[int] = 3


class MemoryEntryRevision(BaseModel):
    """One retained version of a memory entry, newest has the highest number."""

    model_config = ConfigDict(frozen=True)

    revision_number: int
    body: str
    metadata: dict[str, Any]
    written_by_sub: str | None
    created_at: datetime


async def record_revision(doc: Document, written_by_sub: str | None) -> bool:
    """Store *doc* as the next revision of its entry. Return whether one was stored.

    Nothing is stored when the newest revision already has this body and
    these metadata.
    """
    metadata = dict(doc.doc_metadata or {})
    sessionmaker = get_sessionmaker()
    for attempt in range(_ATTEMPTS):
        async with sessionmaker() as session:
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
            try:
                await session.commit()
            except IntegrityError:
                # Another write took this number between our read and our insert.
                if attempt == _ATTEMPTS - 1:
                    raise
                continue
            return True
    return False  # pragma: no cover


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

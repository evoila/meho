# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retained kb revisions: store, read and delete them with the entry head.

A kb entry has two parts:

* The **head** -- one ``documents`` row (``source="kb"``). Search and
  ``meho://kb/{slug}`` read it. A new body overwrites it.
* The **revisions** -- ``kb_revision`` rows. Each write that changes
  the body adds one, in the same transaction as the head update. A
  revision is never updated, so an older body stays readable by its
  revision id after the head changes.

:func:`write_entry` is the only write path, used by both direct create
and directory ingest. :func:`get_revision` is the exact read: it
filters on tenant, entry and revision together, so a foreign, deleted
or mismatched id finds nothing. :func:`delete_entry_rows` removes the
revisions and the head in one transaction.

Two writes to the same entry at the same moment can both try to add
the next revision number. The unique index on
``(entry_id, revision_number)`` makes the second one fail with an
``IntegrityError``; its transaction rolls back (head included) and the
caller can retry. No revision is lost or silently replaced.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import ScalarSelect, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document, KbRevision
from meho_backplane.kb.provenance import read_provenance
from meho_backplane.kb.schemas import (
    KB_KIND_ENTRY,
    KB_SOURCE,
    META_PROVENANCE,
    KbEntryRevision,
)
from meho_backplane.retrieval.indexer import index_document
from meho_backplane.settings import get_settings

__all__ = [
    "CHUNKER_VERSION",
    "current_revision_id",
    "delete_entry_rows",
    "get_revision",
    "write_entry",
]

#: How the body is split for indexing. Today the whole body is one
#: unit; a passage chunker would get a new value here.
CHUNKER_VERSION: Final[str] = "whole-body-v1"


async def write_entry(
    *,
    tenant_id: uuid.UUID,
    slug: str,
    body: str,
    metadata: dict[str, Any],
) -> tuple[Document, uuid.UUID]:
    """Upsert the head row and its current revision in one transaction.

    Returns the head row and the id of the revision that holds its body.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        doc = await index_document(
            tenant_id=tenant_id,
            source=KB_SOURCE,
            source_id=slug,
            kind=KB_KIND_ENTRY,
            body=body,
            metadata=metadata,
            session=session,
        )
        revision_id = await _record_revision(session, doc)
        await session.commit()
    return doc, revision_id


async def _record_revision(session: AsyncSession, doc: Document) -> uuid.UUID:
    """Add a revision when *doc*'s body differs from its latest revision.

    Returns the latest revision id when the body is unchanged, otherwise
    the id of the new revision.
    """
    latest = (
        await session.execute(
            select(KbRevision.id, KbRevision.revision_number, KbRevision.body_hash)
            .where(KbRevision.entry_id == doc.id)
            .order_by(KbRevision.revision_number.desc())
            .limit(1)
        )
    ).one_or_none()
    if latest is not None and latest.body_hash == doc.body_hash:
        latest_id: uuid.UUID = latest.id
        return latest_id

    provenance = doc.doc_metadata.get(META_PROVENANCE)
    revision = KbRevision(
        id=uuid.uuid4(),
        tenant_id=doc.tenant_id,
        entry_id=doc.id,
        revision_number=1 if latest is None else latest.revision_number + 1,
        body=doc.body,
        body_hash=doc.body_hash,
        provenance=dict(provenance) if isinstance(provenance, dict) else None,
        origin="write",
        embedding_model=get_settings().retrieval_embedding_model,
        chunker_version=CHUNKER_VERSION,
        created_at=datetime.now(UTC),
    )
    session.add(revision)
    await session.flush()
    return revision.id


def current_revision_id() -> ScalarSelect[uuid.UUID]:
    """Return a scalar subquery: the highest-numbered revision of each ``Document`` row."""
    return (
        select(KbRevision.id)
        .where(KbRevision.entry_id == Document.id)
        .order_by(KbRevision.revision_number.desc())
        .limit(1)
        .correlate(Document)
        .scalar_subquery()
    )


async def get_revision(
    tenant_id: uuid.UUID,
    entry_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> KbEntryRevision | None:
    """Return one retained revision, or ``None`` when it is not visible.

    The revision must belong to *entry_id*, the entry must be a kb row,
    and both must belong to *tenant_id*. Any mismatch, a deleted entry,
    or an unknown id gives ``None``, so the caller cannot tell which
    case it was.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(KbRevision, Document.source_id)
                .join(Document, Document.id == KbRevision.entry_id)
                .where(
                    KbRevision.id == revision_id,
                    KbRevision.entry_id == entry_id,
                    KbRevision.tenant_id == tenant_id,
                    Document.tenant_id == tenant_id,
                    Document.source == KB_SOURCE,
                )
            )
        ).one_or_none()
    if row is None:
        return None
    revision, slug = row
    stored = {META_PROVENANCE: revision.provenance} if revision.provenance is not None else {}
    return KbEntryRevision(
        entry_id=revision.entry_id,
        revision_id=revision.id,
        tenant_id=revision.tenant_id,
        slug=slug,
        revision_number=revision.revision_number,
        body=revision.body,
        body_hash=revision.body_hash,
        provenance=read_provenance(stored),
        origin=revision.origin,
        embedding_model=revision.embedding_model,
        chunker_version=revision.chunker_version,
        created_at=revision.created_at,
    )


async def delete_entry_rows(tenant_id: uuid.UUID, slug: str) -> bool:
    """Delete one kb entry's revisions and head row in one transaction.

    Returns whether a head row existed. When any statement fails the
    whole transaction rolls back: the entry stays fully readable and
    the caller sees the error and can retry. There is no state where
    the head is gone but a revision can still be read.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        entry_ids = select(Document.id).where(
            Document.tenant_id == tenant_id,
            Document.source == KB_SOURCE,
            Document.source_id == slug,
        )
        await session.execute(
            delete(KbRevision).where(
                KbRevision.tenant_id == tenant_id,
                KbRevision.entry_id.in_(entry_ids),
            )
        )
        result = await session.execute(
            delete(Document).where(
                Document.tenant_id == tenant_id,
                Document.source == KB_SOURCE,
                Document.source_id == slug,
            )
        )
        await session.commit()
    # DML execute() is typed ``Result[Any]``; ``rowcount`` lives on the
    # concrete ``CursorResult`` every supported driver returns.
    rowcount: int = result.rowcount  # type: ignore[attr-defined]
    return rowcount > 0

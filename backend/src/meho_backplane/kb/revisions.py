# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retained kb revisions and passages: write, read and delete them with the entry head.

A kb entry has three parts:

* The **head** -- one ``documents`` row (``source="kb"``). ``meho://kb/{slug}``
  reads it. A new body overwrites it.
* The **revisions** -- ``kb_revision`` rows. Each write that changes
  the body adds one, in the same transaction as the head update. A
  revision is never updated, so an older body stays readable by its
  revision id after the head changes.
* The **passages** (#3761) -- ``documents`` rows with
  ``source="kb-passage"``, made by :mod:`meho_backplane.kb.passages`
  from the **current** revision only. Search ranks these. When the body
  changes, the old passages are deleted and the new ones inserted in
  the same transaction, so the passages of an older revision are never
  searchable next to the current ones.

:func:`write_entry` is the only write path, used by both direct create
and directory ingest. It reads the entry, prepares every row and
embedding with no transaction open, and then publishes head, revision
and passages in one short transaction. :func:`get_revision` is the exact
read: it filters on tenant, entry and revision together, so a foreign,
deleted or mismatched id finds nothing. :func:`delete_entry_rows`
locks the head like a write does, then removes passages, revisions and
the head in one transaction.

Two writes to the same entry at the same moment must not mix. On
PostgreSQL the publish step locks the head row first (``SELECT ... FOR
UPDATE``), so the second writer waits for the first to commit. Under
the lock (or, on SQLite, which has no row lock, just before writing)
the publish step reads the entry again. When it no longer matches what
the write prepared against, the write is rolled back and runs again
from the start. The unique index on ``(entry_id, revision_number)`` and
the unique natural key on ``documents`` are the last guards; a clash
there also triggers a rerun. The id that is returned always names the
body that was written.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from sqlalchemy import ScalarSelect, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document, KbRevision
from meho_backplane.kb.passages import (
    CHUNKER_VERSION,
    chunk_markdown,
    index_generation,
    passage_items,
)
from meho_backplane.kb.provenance import read_provenance
from meho_backplane.kb.schemas import (
    KB_KIND_ENTRY,
    KB_PASSAGE_SOURCE,
    KB_SOURCE,
    META_KB_PASSAGE,
    META_PROVENANCE,
    KbEntryRevision,
)
from meho_backplane.retrieval.indexer import (
    IndexItem,
    PreparedIndexBatch,
    compute_body_hash,
    get_passage_tokenizer,
    prepare_index_batch,
)
from meho_backplane.settings import get_settings

__all__ = [
    "CHUNKER_VERSION",
    "current_revision_id",
    "delete_entry_rows",
    "get_revision",
    "write_entry",
]

#: How often one write runs again after another write to the same
#: entry got in between. Each rerun reads the current head, so two
#: tries cover one overlap; the third is slack.
_WRITE_ATTEMPTS: Final[int] = 3


class _StaleHeadError(RuntimeError):
    """The entry changed after this write read it."""


@dataclass(frozen=True)
class _Snapshot:
    """What one write saw of an entry before it prepared its rows.

    ``passage_key`` is ``(revision_id, index_generation)`` of the
    entry's passage rows, or ``None`` when it has none.
    """

    entry_id: uuid.UUID | None = None
    body_hash: str | None = None
    revision_id: uuid.UUID | None = None
    revision_number: int = 0
    revision_hash: str | None = None
    passage_key: tuple[str, str] | None = None


@dataclass(frozen=True)
class _Plan:
    """The rows one write publishes, with every embedding already computed."""

    entry_id: uuid.UUID
    revision_id: uuid.UUID
    new_revision: bool
    head_changed: bool
    rebuild_passages: bool
    batch: PreparedIndexBatch


async def write_entry(
    *,
    tenant_id: uuid.UUID,
    slug: str,
    body: str,
    metadata: dict[str, Any],
) -> tuple[Document, uuid.UUID]:
    """Publish the head, its current revision and its passages together.

    Three steps:

    1. Read the entry as it is now (:class:`_Snapshot`).
    2. Prepare: split the body into passages and compute every
       embedding. No transaction is open, and nothing is written.
    3. Publish in one short transaction: lock the head, check it still
       matches the snapshot, write the head, add the revision, swap the
       passage rows, commit.

    A failure in step 2 or 3 leaves the old head, revision and passages
    as they were. When another write to the same entry got in between,
    this write runs again from step 1 (see the module docstring). After
    :data:`_WRITE_ATTEMPTS` failed tries the last error propagates.
    Returns the head row and the id of the revision that holds its body.
    """
    log = structlog.get_logger()
    for attempt in range(1, _WRITE_ATTEMPTS + 1):
        try:
            snapshot = await _read_snapshot(tenant_id, slug)
            plan = await _prepare(tenant_id, slug, body, metadata, snapshot)
            return await _publish(tenant_id, slug, body, metadata, snapshot, plan)
        except (IntegrityError, _StaleHeadError) as exc:
            if attempt == _WRITE_ATTEMPTS:
                raise
            log.info(
                "kb_write_rerun",
                tenant_id=str(tenant_id),
                slug=slug,
                attempt=attempt,
                reason=type(exc).__name__,
            )
    raise AssertionError("unreachable")  # pragma: no cover


async def _read_snapshot(tenant_id: uuid.UUID, slug: str) -> _Snapshot:
    """Read the entry's current state in a short read-only session."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return await _snapshot_in(session, tenant_id, slug)


async def _snapshot_in(session: AsyncSession, tenant_id: uuid.UUID, slug: str) -> _Snapshot:
    """Read head, latest revision and passage key with *session*.

    Column selects only, so the values come from the database and not
    from rows the session already holds.
    """
    head = (
        await session.execute(
            select(Document.id, Document.body_hash).where(
                Document.tenant_id == tenant_id,
                Document.source == KB_SOURCE,
                Document.source_id == slug,
            )
        )
    ).one_or_none()
    if head is None:
        return _Snapshot()
    latest = (
        await session.execute(
            select(KbRevision.id, KbRevision.revision_number, KbRevision.body_hash)
            .where(KbRevision.entry_id == head.id)
            .order_by(KbRevision.revision_number.desc())
            .limit(1)
        )
    ).one_or_none()
    locator = await session.scalar(
        select(Document.doc_metadata[META_KB_PASSAGE])
        .where(*_passage_filter(tenant_id, head.id))
        .limit(1)
    )
    passage_key = (
        (str(locator.get("revision_id")), str(locator.get("index_generation")))
        if isinstance(locator, dict)
        else None
    )
    return _Snapshot(
        entry_id=head.id,
        body_hash=head.body_hash,
        revision_id=latest.id if latest is not None else None,
        revision_number=latest.revision_number if latest is not None else 0,
        revision_hash=latest.body_hash if latest is not None else None,
        passage_key=passage_key,
    )


def _passage_filter(tenant_id: uuid.UUID, entry_id: uuid.UUID) -> tuple[Any, ...]:
    """WHERE terms that select every passage row of one entry."""
    return (
        Document.tenant_id == tenant_id,
        Document.source == KB_PASSAGE_SOURCE,
        Document.doc_metadata[(META_KB_PASSAGE, "entry_id")].as_string() == str(entry_id),
    )


async def _prepare(
    tenant_id: uuid.UUID,
    slug: str,
    body: str,
    metadata: dict[str, Any],
    snapshot: _Snapshot,
) -> _Plan:
    """Decide what changes and compute its embeddings; write nothing.

    The head is re-embedded only when its body changed. A new revision
    is stored only when the body differs from the latest revision. The
    passages are rebuilt when they do not belong to the current
    revision and the current chunker and model (a new indexing
    generation); otherwise they are kept.
    """
    body_hash = compute_body_hash(body)
    model = get_settings().retrieval_embedding_model
    entry_id = snapshot.entry_id or uuid.uuid4()
    head_changed = snapshot.body_hash != body_hash
    if snapshot.revision_id is not None and snapshot.revision_hash == body_hash:
        revision_id, new_revision = snapshot.revision_id, False
    else:
        revision_id, new_revision = uuid.uuid4(), True
    rebuild = snapshot.passage_key != (str(revision_id), index_generation(model))

    items: list[IndexItem] = []
    if head_changed:
        items.append(
            IndexItem(
                source=KB_SOURCE,
                source_id=slug,
                kind=KB_KIND_ENTRY,
                body=body,
                metadata=metadata,
                document_id=entry_id,
            )
        )
    if rebuild:
        tokenizer = await get_passage_tokenizer()
        passages = await asyncio.to_thread(chunk_markdown, body, tokenizer.count, tokenizer.window)
        items.extend(
            passage_items(
                passages=passages,
                entry_id=entry_id,
                revision_id=revision_id,
                slug=slug,
                metadata=metadata,
                embedding_model=model,
            )
        )
    return _Plan(
        entry_id=entry_id,
        revision_id=revision_id,
        new_revision=new_revision,
        head_changed=head_changed,
        rebuild_passages=rebuild,
        batch=await prepare_index_batch(tenant_id, items),
    )


async def _publish(
    tenant_id: uuid.UUID,
    slug: str,
    body: str,
    metadata: dict[str, Any],
    snapshot: _Snapshot,
    plan: _Plan,
) -> tuple[Document, uuid.UUID]:
    """Write the prepared rows in one transaction; raise when the entry moved."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await _lock_head(session, tenant_id, slug)
        if await _snapshot_in(session, tenant_id, slug) != snapshot:
            raise _StaleHeadError(f"kb entry {slug!r} changed during write")
        if plan.rebuild_passages:
            await session.execute(
                delete(Document).where(*_passage_filter(tenant_id, plan.entry_id))
            )
        await plan.batch.apply(session)
        doc = await session.get_one(Document, plan.entry_id)
        if not plan.head_changed:
            doc.doc_metadata = metadata
            doc.updated_at = datetime.now(UTC)
        if not plan.rebuild_passages:
            await _refresh_passage_metadata(session, tenant_id, plan.entry_id, metadata)
        if plan.new_revision:
            _add_revision(session, doc, body, plan.revision_id, snapshot.revision_number + 1)
        await session.flush()
        # Read back what this transaction now sees. On SQLite another
        # write can commit between the check above and this write's
        # first statement; then the head no longer holds this body.
        after = await _snapshot_in(session, tenant_id, slug)
        if after.body_hash != compute_body_hash(body) or after.revision_id != plan.revision_id:
            raise _StaleHeadError(f"kb entry {slug!r} changed during write")
        await session.commit()
    structlog.get_logger().info(
        "kb_entry_published",
        tenant_id=str(tenant_id),
        slug=slug,
        head_changed=plan.head_changed,
        new_revision=plan.new_revision,
        passages_rebuilt=plan.rebuild_passages,
    )
    return doc, plan.revision_id


async def _lock_head(session: AsyncSession, tenant_id: uuid.UUID, slug: str) -> list[uuid.UUID]:
    """Lock the entry's head row for this transaction; return the head ids.

    On PostgreSQL this is ``SELECT ... FOR UPDATE``. A second writer or
    a delete of the same entry waits here until the first transaction
    commits, and every later statement in this transaction then sees
    the rows that transaction committed. SQLite has no row lock; there
    the snapshot check in :func:`_publish` catches an overlap instead.
    A first write has no row to lock; the unique natural key on
    ``documents`` makes a second first-write fail, and
    :func:`write_entry` reruns it.
    """
    stmt = select(Document.id).where(
        Document.tenant_id == tenant_id,
        Document.source == KB_SOURCE,
        Document.source_id == slug,
    )
    conn = await session.connection()
    if conn.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    return list((await session.scalars(stmt)).all())


async def _refresh_passage_metadata(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    entry_id: uuid.UUID,
    metadata: dict[str, Any],
) -> None:
    """Copy the head's new metadata onto its kept passage rows."""
    rows = await session.scalars(select(Document).where(*_passage_filter(tenant_id, entry_id)))
    for row in rows:
        updated = {**metadata, META_KB_PASSAGE: row.doc_metadata[META_KB_PASSAGE]}
        if updated != row.doc_metadata:
            row.doc_metadata = updated


def _add_revision(
    session: AsyncSession,
    doc: Document,
    body: str,
    revision_id: uuid.UUID,
    revision_number: int,
) -> None:
    """Store *body* as a new retained revision of *doc* (flushed by the caller)."""
    provenance = doc.doc_metadata.get(META_PROVENANCE)
    session.add(
        KbRevision(
            id=revision_id,
            tenant_id=doc.tenant_id,
            entry_id=doc.id,
            revision_number=revision_number,
            body=body,
            body_hash=compute_body_hash(body),
            provenance=dict(provenance) if isinstance(provenance, dict) else None,
            origin="write",
            embedding_model=get_settings().retrieval_embedding_model,
            chunker_version=CHUNKER_VERSION,
            created_at=datetime.now(UTC),
        )
    )


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
    """Delete one kb entry's passages, revisions and head row in one transaction.

    Returns whether a head row existed. The head is locked first, like
    a write locks it. A delete that overlaps a write therefore waits
    until that write has committed and then removes the passages and
    the revision that write added, so no passage of a deleted entry
    stays searchable. Every delete uses the same locked head ids.

    When any statement fails the whole transaction rolls back: the
    entry stays fully readable and the caller sees the error and can
    retry. There is no state where the head is gone but a revision or
    a passage can still be read.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        entry_ids = await _lock_head(session, tenant_id, slug)
        for entry_id in entry_ids:
            await session.execute(delete(Document).where(*_passage_filter(tenant_id, entry_id)))
        if entry_ids:
            await session.execute(
                delete(KbRevision).where(
                    KbRevision.tenant_id == tenant_id,
                    KbRevision.entry_id.in_(entry_ids),
                )
            )
            await session.execute(
                delete(Document).where(
                    Document.tenant_id == tenant_id,
                    Document.id.in_(entry_ids),
                )
            )
        await session.commit()
    return bool(entry_ids)

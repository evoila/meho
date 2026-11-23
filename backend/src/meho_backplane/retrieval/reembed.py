# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Embed every stored vector again with the set model.

Run it in the backplane container after a change of
``RETRIEVAL_EMBEDDING_MODEL``:

    python -m meho_backplane.retrieval.reembed [--dry-run] [--batch-size N]

It works through three groups, in batches, and prints progress:

* every ``documents`` row that is not a knowledge passage (knowledge
  heads, memory rows, every other source): the ``body`` is embedded again;
* the knowledge passages: each current entry goes through the normal write
  path (:func:`~meho_backplane.kb.revisions.write_entry`) with its own body
  and metadata. That path rebuilds the passages when they were made under
  another model, so the passage window and ids follow the new model. The
  entry keeps its revision and its metadata. The write names the revision
  it read, so an entry that was changed or deleted in the meantime is read
  again (or skipped when it is gone), never written back with old content;
* every ``endpoint_descriptor`` row: the embedding text is composed again
  from the stored fields and embedded. A row without a vector gets one.

In the first and the third group, each row is read again right before its
vector is written. A row whose text changed while it was encoded is encoded
again with the new text, and a deleted row is skipped. So an edit made
during the run keeps its text, its time and a vector of that text.

No row gets a new ``created_at`` or ``updated_at``: searches filter on these
times, and a re-embed changes no content.

A row or entry that changed during each of its tries is not written. Then
the run is not complete: the command still works through every group, but
it keeps the old model in ``embedding_state`` and exits with 1. Run it again.

When every row is done, it stores the set model in ``embedding_state``. It is safe to run
twice: a second run writes the same vectors, and passages that already
belong to the set model are left as they are. Searches mix old and new
vectors until it ends, so plan a maintenance window.

``--dry-run`` writes nothing and prints the counts.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update

from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import Document, EndpointDescriptor
from meho_backplane.kb.revisions import ExpectedRevision, current_revision_id, write_entry
from meho_backplane.kb.schemas import KB_PASSAGE_SOURCE, KB_SOURCE
from meho_backplane.kb.stale_revision import KbStaleRevisionError
from meho_backplane.operations.embed import build_embedding_text
from meho_backplane.retrieval.embedding import EmbeddingService, get_embedding_service
from meho_backplane.retrieval.embedding_state import write_embedding_state
from meho_backplane.settings import get_settings

__all__ = ["ReembedCounts", "ReembedIncompleteError", "main", "reembed_all"]

DEFAULT_BATCH_SIZE = 50

#: How often a row or a knowledge entry is read again after another write changed it.
_ATTEMPTS = 3


@dataclass
class ReembedCounts:
    """How many rows of each group were (or, in a dry run, would be) embedded again."""

    documents: int = 0
    kb_entries: int = 0
    endpoints: int = 0
    #: Rows and knowledge entries not written because they changed during each try.
    skipped: int = 0


class ReembedIncompleteError(Exception):
    """Some rows changed during each try, so the run did not embed them all."""


def _document_texts(rows: Sequence[Any]) -> list[str]:
    return [row.body for row in rows]


def _endpoint_texts(rows: Sequence[Any]) -> list[str]:
    return [
        build_embedding_text(
            summary=row.summary or "",
            description=row.description or "",
            custom_description=row.custom_description,
            tags=row.tags,
        )
        for row in rows
    ]


@dataclass(frozen=True)
class _Group:
    """A table whose rows get a vector made from some of their columns."""

    label: str
    table: type[Document] | type[EndpointDescriptor]
    columns: tuple[Any, ...]
    texts_of: Callable[[Sequence[Any]], list[str]]
    where: tuple[Any, ...] = ()


_DOCUMENTS = _Group(
    "documents",
    Document,
    (Document.body,),
    _document_texts,
    where=(Document.source != KB_PASSAGE_SOURCE,),
)
_ENDPOINTS = _Group(
    "operations",
    EndpointDescriptor,
    (
        EndpointDescriptor.summary,
        EndpointDescriptor.description,
        EndpointDescriptor.custom_description,
        EndpointDescriptor.tags,
    ),
    _endpoint_texts,
)


async def _read_rows(group: _Group, ids: Sequence[uuid.UUID]) -> list[Any]:
    table = group.table
    async with get_sessionmaker()() as session:
        query = select(table.id, *group.columns).where(table.id.in_(ids)).order_by(table.id)
        return list((await session.execute(query)).all())


async def _write_vectors(
    group: _Group, rows: Sequence[Any], vectors: Sequence[list[float]]
) -> list[uuid.UUID]:
    """Write each vector when its row still holds the content it was made from.

    The rows are read again right before the write, locked on PostgreSQL.
    A row whose content columns changed since *rows* were read is not
    written; its id is returned so the caller can encode it again. A
    deleted row is skipped. A written row keeps the ``updated_at`` it has
    now, so an edit that committed meanwhile keeps its time.
    """
    table = group.table
    encoded = {row.id: (tuple(row)[1:], vec) for row, vec in zip(rows, vectors, strict=True)}
    async with get_sessionmaker()() as session:
        query = select(table.id, table.updated_at, *group.columns).where(table.id.in_(encoded))
        if (await session.connection()).dialect.name == "postgresql":
            query = query.with_for_update()
        changed: list[uuid.UUID] = []
        writes: list[dict[str, Any]] = []
        for row in (await session.execute(query)).all():
            content, vector = encoded[row.id]
            if tuple(row)[2:] != content:
                changed.append(row.id)
                continue
            writes.append({"id": row.id, "embedding": vector, "updated_at": row.updated_at})
        if writes:
            await session.execute(update(table), writes)
        await session.commit()
    return changed


async def _reembed_table(
    group: _Group,
    service: EmbeddingService,
    batch_size: int,
    dry_run: bool,
    say: Callable[[str], None],
) -> tuple[int, int]:
    """Return how many rows were embedded and how many were skipped."""
    table = group.table
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        total = int(
            await session.scalar(select(func.count()).select_from(table).where(*group.where)) or 0
        )
    if dry_run:
        return total, 0
    done = 0
    skipped = 0
    last: uuid.UUID | None = None
    while True:
        async with sessionmaker() as session:
            query = select(table.id, *group.columns).where(*group.where).order_by(table.id)
            if last is not None:
                query = query.where(table.id > last)
            rows = list((await session.execute(query.limit(batch_size))).all())
        if not rows:
            return done - skipped, skipped
        last = rows[-1].id
        pending = rows
        for _attempt in range(_ATTEMPTS):
            vectors = await service.encode(group.texts_of(pending))
            changed = await _write_vectors(group, pending, vectors)
            pending = await _read_rows(group, changed) if changed else []
            if not pending:
                break
        if pending:
            skipped += len(pending)
            say(f"{group.label}: {len(pending)} rows skipped, they changed during each try")
        done += len(rows)
        say(f"{group.label}: {done}/{total}")


class _EntryGoneError(Exception):
    """The knowledge entry was deleted after the command read it."""


def _current_metadata(current: dict[str, Any] | None) -> dict[str, Any]:
    if current is None:
        raise _EntryGoneError
    return current


async def _reembed_kb_entry(tenant_id: uuid.UUID, slug: str) -> str:
    """Rebuild the passages of one entry.

    Return ``"done"``, ``"deleted"`` when the entry is gone, or ``"changed"``
    when another write changed it during each try.
    """
    for _attempt in range(_ATTEMPTS):
        async with get_sessionmaker()() as session:
            row = (
                await session.execute(
                    select(Document.body, current_revision_id().label("revision_id")).where(
                        Document.tenant_id == tenant_id,
                        Document.source == KB_SOURCE,
                        Document.source_id == slug,
                    )
                )
            ).one_or_none()
        if row is None:  # deleted since the list was read
            return "deleted"
        try:
            await write_entry(
                tenant_id=tenant_id,
                slug=slug,
                body=row.body,
                metadata_for=_current_metadata,
                expected=ExpectedRevision(row.revision_id),
                keep_timestamps=True,
            )
        except _EntryGoneError:
            return "deleted"
        except KbStaleRevisionError:
            continue  # another write got in between; read the entry again
        return "done"
    return "changed"


async def _reembed_kb_entries(dry_run: bool, say: Callable[[str], None]) -> tuple[int, int]:
    """Return how many entries were embedded or are gone, and how many were skipped."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        entries = (
            await session.execute(
                select(Document.tenant_id, Document.source_id).where(Document.source == KB_SOURCE)
            )
        ).all()
    if dry_run:
        return len(entries), 0
    skipped = 0
    for index, (tenant_id, slug) in enumerate(entries, start=1):
        outcome = await _reembed_kb_entry(tenant_id, slug)
        if outcome == "deleted":
            say(f"knowledge entry {slug}: skipped, it was deleted during the run")
        elif outcome == "changed":
            skipped += 1
            say(f"knowledge entry {slug}: skipped, it changed during each try")
        if index % 10 == 0 or index == len(entries):
            say(f"knowledge entries: {index}/{len(entries)}")
    return len(entries) - skipped, skipped


async def reembed_all(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    service: EmbeddingService | None = None,
    say: Callable[[str], None] = print,
) -> ReembedCounts:
    """Embed every stored vector again, then store the set model. See the module text.

    Raise :class:`ReembedIncompleteError`, and keep the old model in the
    record, when a row or entry changed during each of its tries.
    """
    if batch_size < 1:
        raise ValueError("batch size must be at least 1")
    model = get_settings().retrieval_embedding_model
    service = service if service is not None else get_embedding_service()
    say(f"model: {model}" + (" (dry run, nothing is written)" if dry_run else ""))
    counts = ReembedCounts()
    counts.documents, skipped_documents = await _reembed_table(
        _DOCUMENTS, service, batch_size, dry_run, say
    )
    counts.kb_entries, skipped_entries = await _reembed_kb_entries(dry_run, say)
    counts.endpoints, skipped_endpoints = await _reembed_table(
        _ENDPOINTS, service, batch_size, dry_run, say
    )
    counts.skipped = skipped_documents + skipped_entries + skipped_endpoints
    if counts.skipped:
        raise ReembedIncompleteError(
            f"{counts.skipped} rows or knowledge entries changed during each try and keep "
            "their old vector; the record stays on the old model. Run the command again."
        )
    if not dry_run:
        async with get_sessionmaker()() as session:
            await write_embedding_state(session, model)
            await session.commit()
    say(
        f"{'would embed' if dry_run else 'embedded'}: {counts.documents} documents, "
        f"{counts.kb_entries} knowledge entries, {counts.endpoints} operations"
        + ("" if dry_run else ". Restart MEHO to clear the start warning.")
    )
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command. Return a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m meho_backplane.retrieval.reembed",
        description="Embed every stored vector again with the set embedding model.",
    )
    parser.add_argument("--dry-run", action="store_true", help="count rows only; write nothing")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)

    async def _run() -> None:
        try:
            await reembed_all(batch_size=args.batch_size, dry_run=args.dry_run)
        finally:
            await get_engine().dispose()

    try:
        asyncio.run(_run())
    except Exception as exc:
        print(f"[reembed] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

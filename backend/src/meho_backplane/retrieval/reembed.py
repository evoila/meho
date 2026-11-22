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

No row gets a new ``created_at`` or ``updated_at``: searches filter on these
times, and a re-embed changes no content.

At the end it stores the set model in ``embedding_state``. It is safe to run
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

__all__ = ["ReembedCounts", "main", "reembed_all"]

DEFAULT_BATCH_SIZE = 50

#: How often one knowledge entry is read again after another write changed it.
_KB_ATTEMPTS = 3


@dataclass
class ReembedCounts:
    """How many rows of each group were (or, in a dry run, would be) embedded again."""

    documents: int = 0
    kb_entries: int = 0
    endpoints: int = 0


async def _reembed_documents(
    service: EmbeddingService, batch_size: int, dry_run: bool, say: Callable[[str], None]
) -> int:
    where = Document.source != KB_PASSAGE_SOURCE
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        total = int(
            await session.scalar(select(func.count()).select_from(Document).where(where)) or 0
        )
    if dry_run:
        return total
    done = 0
    last: uuid.UUID | None = None
    while True:
        async with sessionmaker() as session:
            query = (
                select(Document.id, Document.body, Document.updated_at)
                .where(where)
                .order_by(Document.id)
            )
            if last is not None:
                query = query.where(Document.id > last)
            rows = (await session.execute(query.limit(batch_size))).all()
            if not rows:
                return done
            vectors = await service.encode([row.body for row in rows])
            await session.execute(
                update(Document),
                [
                    {"id": row.id, "embedding": vec, "updated_at": row.updated_at}
                    for row, vec in zip(rows, vectors, strict=True)
                ],
            )
            await session.commit()
        last = rows[-1].id
        done += len(rows)
        say(f"documents: {done}/{total}")


class _EntryGoneError(Exception):
    """The knowledge entry was deleted after the command read it."""


def _current_metadata(current: dict[str, Any] | None) -> dict[str, Any]:
    if current is None:
        raise _EntryGoneError
    return current


async def _reembed_kb_entry(tenant_id: uuid.UUID, slug: str) -> bool:
    """Rebuild the passages of one entry; return ``False`` when it was skipped."""
    for _attempt in range(_KB_ATTEMPTS):
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
            return False
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
            return False
        except KbStaleRevisionError:
            continue  # another write got in between; read the entry again
        return True
    return False


async def _reembed_kb_entries(dry_run: bool, say: Callable[[str], None]) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        entries = (
            await session.execute(
                select(Document.tenant_id, Document.source_id).where(Document.source == KB_SOURCE)
            )
        ).all()
    if dry_run:
        return len(entries)
    for index, (tenant_id, slug) in enumerate(entries, start=1):
        if not await _reembed_kb_entry(tenant_id, slug):
            say(f"knowledge entry {slug}: skipped, it was deleted or changed during the run")
        if index % 10 == 0 or index == len(entries):
            say(f"knowledge entries: {index}/{len(entries)}")
    return len(entries)


async def _reembed_endpoints(
    service: EmbeddingService, batch_size: int, dry_run: bool, say: Callable[[str], None]
) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        total = int(await session.scalar(select(func.count()).select_from(EndpointDescriptor)) or 0)
    if dry_run:
        return total
    done = 0
    last: uuid.UUID | None = None
    while True:
        async with sessionmaker() as session:
            query = select(
                EndpointDescriptor.id,
                EndpointDescriptor.summary,
                EndpointDescriptor.description,
                EndpointDescriptor.custom_description,
                EndpointDescriptor.tags,
                EndpointDescriptor.updated_at,
            ).order_by(EndpointDescriptor.id)
            if last is not None:
                query = query.where(EndpointDescriptor.id > last)
            rows = (await session.execute(query.limit(batch_size))).all()
            if not rows:
                return done
            texts = [
                build_embedding_text(
                    summary=row.summary or "",
                    description=row.description or "",
                    custom_description=row.custom_description,
                    tags=row.tags,
                )
                for row in rows
            ]
            vectors = await service.encode(texts)
            await session.execute(
                update(EndpointDescriptor),
                [
                    {"id": row.id, "embedding": vec, "updated_at": row.updated_at}
                    for row, vec in zip(rows, vectors, strict=True)
                ],
            )
            await session.commit()
        last = rows[-1].id
        done += len(rows)
        say(f"operations: {done}/{total}")


async def reembed_all(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    service: EmbeddingService | None = None,
    say: Callable[[str], None] = print,
) -> ReembedCounts:
    """Embed every stored vector again, then store the set model. See the module text."""
    if batch_size < 1:
        raise ValueError("batch size must be at least 1")
    model = get_settings().retrieval_embedding_model
    service = service if service is not None else get_embedding_service()
    say(f"model: {model}" + (" (dry run, nothing is written)" if dry_run else ""))
    counts = ReembedCounts()
    counts.documents = await _reembed_documents(service, batch_size, dry_run, say)
    counts.kb_entries = await _reembed_kb_entries(dry_run, say)
    counts.endpoints = await _reembed_endpoints(service, batch_size, dry_run, say)
    if not dry_run:
        async with get_sessionmaker()() as session:
            await write_embedding_state(session, model)
            await session.commit()
    say(
        f"{'would embed' if dry_run else 'embedded'}: {counts.documents} documents, "
        f"{counts.kb_entries} knowledge entries, {counts.endpoints} operations"
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

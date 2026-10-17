# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Directory ingest for the kb: sorted pre-walk, slug conflicts, provenance.

:meth:`KbService.ingest_directory <meho_backplane.kb.service.KbService.ingest_directory>`
delegates here. The rule this module owns: read every file first, in
sorted relative-path order, and find every slug claimed by two or more
files before any write. Those files are reported as conflicts and not
written, so filesystem listing order never decides which file wins and
an existing entry with a conflicting slug stays unchanged. Direct
create (:meth:`KbService.create_entry`) is a different, explicit
last-write-wins path.

Each written entry gets a ``directory`` provenance block (relative
path, one ``ingested_at`` per run) via :mod:`meho_backplane.kb.provenance`.
No absolute host path is stored or returned.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.kb.file_walker import KbFileRecord, walk_kb_directory
from meho_backplane.kb.provenance import stamp_provenance
from meho_backplane.kb.schemas import (
    KB_KIND_ENTRY,
    KB_SOURCE,
    KbIngestConflict,
    KbIngestionResult,
)
from meho_backplane.retrieval.indexer import compute_body_hash, index_document
from meho_backplane.settings import get_settings

__all__ = ["KbIngestRootError", "ingest_directory"]


class KbIngestRootError(ValueError):
    """The requested ingest directory resolves outside ``KB_INGEST_ROOT``.

    Raised by :meth:`KbService.ingest_directory` before any file is
    read when the supplied directory -- after :meth:`Path.resolve`
    follows every symlink and normalises ``..`` segments -- does not
    land within the configured :attr:`~meho_backplane.settings.Settings.kb_ingest_root`.
    This is the path-traversal / local-file-inclusion guard for the
    privileged bulk-ingest surface (#101 rows L8 + L14): a
    ``tenant_admin`` cannot point the ingest at an arbitrary ``.md``
    file elsewhere on the backplane host.

    Subclass of :class:`ValueError` -- same parent as
    :class:`~meho_backplane.kb.schemas.InvalidKbSlugError` -- so a
    caller treating it as an input-shape problem can use one ``except``
    clause. The route layer maps it to a structured 400.
    """


def _resolve_within_ingest_root(directory: Path) -> Path:
    """Resolve *directory* and confirm it lands within ``KB_INGEST_ROOT``.

    Both the configured root and the requested directory are resolved
    -- :meth:`Path.resolve` follows symlinks and collapses ``..`` --
    so a traversal payload (``<root>/../../etc``) and an escaping
    symlink (a link inside the root pointing at ``/etc``) both resolve
    outside the root and are rejected. Returns the resolved directory
    (the walker re-resolves idempotently); raises
    :class:`KbIngestRootError` when it escapes the root (#101 L8 + L14).
    """
    root = Path(get_settings().kb_ingest_root).resolve()
    resolved = directory.resolve()
    if not resolved.is_relative_to(root):
        raise KbIngestRootError(
            f"kb_ingest_path_outside_root: {directory} resolves to "
            f"{resolved}, which is outside the configured ingest root "
            f"{root}"
        )
    return resolved


_IngestAction = Literal["inserted", "updated", "skipped"]


async def ingest_directory(
    directory: Path,
    tenant_id: uuid.UUID,
    *,
    dry_run: bool = False,
) -> KbIngestionResult:
    """Walk *directory*, ingest every ``.md`` file under it.

    Deterministic and conflict-safe: every file is read first, in
    sorted relative-path order. Any slug claimed by two or more
    files is reported in :attr:`KbIngestionResult.conflicts` and
    none of those files is written -- an existing entry with that
    slug stays unchanged. Each written entry gets a provenance
    block with the file's relative path, ``source_kind="directory"``
    and the run's start time (one timestamp for the whole run).

    Idempotent: an unchanged corpus re-ingests as all ``skipped``
    (the body-hash short-circuit skips re-embedding). Per-file
    errors (unreadable bytes, invalid slug, malformed front-matter,
    index failure) are counted in :attr:`KbIngestionResult.errors`
    and the run continues with the remaining files.

    Parameters
    ----------
    directory
        Path to the kb directory. Confined to ``KB_INGEST_ROOT``
        by :func:`_resolve_within_ingest_root` (raises
        :class:`KbIngestRootError` on escape, #101). Must exist and
        be a directory; otherwise :class:`NotADirectoryError` /
        :class:`FileNotFoundError` propagates.
    tenant_id
        The tenant on whose behalf the corpus is being ingested.
        Bound by the caller from the operator's JWT.
    dry_run
        When ``True``, classify every file (insert / update / skip /
        conflict / error) without writing to the DB. Useful for
        ``meho kb ingest --dry-run`` before a real run.
    """
    log = structlog.get_logger()
    # Confine to KB_INGEST_ROOT before any read; dry-run too (#101).
    resolved = _resolve_within_ingest_root(directory)
    counts: Counter[_IngestAction] = Counter()
    errors: list[str] = []
    ingested_at = datetime.now(UTC)

    # Pre-walk the whole tree before any write so slug conflicts
    # are known up front (the walker yields in sorted order).
    records = list(walk_kb_directory(resolved, errors=errors))
    conflicts = _find_slug_conflicts(records)
    conflicted_slugs = {conflict.slug for conflict in conflicts}
    conflict_count = sum(len(conflict.relative_paths) for conflict in conflicts)

    for record in records:
        if record.slug in conflicted_slugs:
            continue
        try:
            action = await _ingest_one(
                tenant_id=tenant_id,
                record=record,
                ingested_at=ingested_at,
                dry_run=dry_run,
            )
        except Exception as exc:
            # Database / embedding failures in the index path. The
            # walker already collected read / parse / slug errors;
            # this is best-effort bulk, so count and continue.
            log.exception(
                "kb_ingest_file_failed",
                path=record.relative_path,
                tenant_id=str(tenant_id),
            )
            errors.append(f"{record.relative_path}: {type(exc).__name__}: {exc}")
            continue
        counts[action] += 1

    inserted, updated, skipped = counts["inserted"], counts["updated"], counts["skipped"]
    log.info(
        "kb_ingest_completed",
        tenant_id=str(tenant_id),
        directory=str(directory),
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        errors=len(errors),
        conflicts=conflict_count,
        dry_run=dry_run,
    )
    return KbIngestionResult(
        inserted_count=inserted,
        updated_count=updated,
        skipped_count=skipped,
        error_count=len(errors),
        conflict_count=conflict_count,
        errors=errors,
        conflicts=conflicts,
    )


async def _ingest_one(
    *,
    tenant_id: uuid.UUID,
    record: KbFileRecord,
    ingested_at: datetime,
    dry_run: bool,
) -> _IngestAction:
    """Classify the action for one file, then perform it unless *dry_run*.

    Returns ``"inserted"`` / ``"updated"`` / ``"skipped"`` per
    the comparison between the incoming body's SHA-256 hash and
    any existing row's. Stamps the provenance block (relative
    path, ``source_kind="directory"``, *ingested_at*) into the
    metadata before delegating to
    :func:`~meho_backplane.retrieval.indexer.index_document`.

    The classification SELECT is one extra round-trip per file
    compared to calling ``index_document`` directly; the cost is
    the price of the precise (inserted / updated / skipped)
    breakdown the acceptance criteria require. v0.2.next can
    collapse this into a single round-trip if the helper
    exposes the action in its return shape.
    """
    sessionmaker = get_sessionmaker()
    slug = record.slug
    new_hash = compute_body_hash(record.body)
    async with sessionmaker() as session:
        existing = await session.execute(
            select(Document.id, Document.body_hash).where(
                Document.tenant_id == tenant_id,
                Document.source == KB_SOURCE,
                Document.source_id == slug,
            )
        )
        row = existing.one_or_none()

    if row is None:
        action: _IngestAction = "inserted"
    elif row.body_hash == new_hash:
        action = "skipped"
    else:
        action = "updated"

    if dry_run:
        return action

    enriched_metadata = stamp_provenance(
        record.metadata,
        source_kind="directory",
        relative_path=record.relative_path,
        ingested_at=ingested_at,
    )

    await index_document(
        tenant_id=tenant_id,
        source=KB_SOURCE,
        source_id=slug,
        kind=KB_KIND_ENTRY,
        body=record.body,
        metadata=enriched_metadata,
    )
    return action


def _find_slug_conflicts(records: Sequence[KbFileRecord]) -> list[KbIngestConflict]:
    """Return one :class:`KbIngestConflict` per slug claimed by 2+ files, sorted by slug."""
    paths_by_slug: dict[str, list[str]] = defaultdict(list)
    for record in records:
        paths_by_slug[record.slug].append(record.relative_path)
    return [
        KbIngestConflict(slug=slug, relative_paths=sorted(paths))
        for slug, paths in sorted(paths_by_slug.items())
        if len(paths) > 1
    ]

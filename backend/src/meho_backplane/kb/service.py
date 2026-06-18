# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant-scoped :class:`KbService` over the G0.4 ``documents`` substrate.

Initiative #331 (G4.1) T1 surface. Every later wave of G4.1 -- the
REST routes (T2), the MCP meta-tools (T3), the CLI verbs (T4) --
calls into this service rather than into the substrate directly so
the kb-shaped vocabulary (slug, snippet, ``KbEntry``) stays
canonical and the substrate's natural-key contract
(``(tenant_id, source, source_id)``) is enforced in one place.

Concurrency model
-----------------

:class:`KbService` is stateless and method-scoped: each public
method opens its own :class:`AsyncSession` via
:func:`~meho_backplane.db.engine.get_sessionmaker` and commits
synchronously before returning. Per-file commits during
:meth:`ingest_directory` are the load-bearing reason -- a failing
file in the middle of a 44-entry corpus must not roll back the
successful files preceding it. The dominant ingest cost is the
embedding compute, not the SQL round-trip, so the per-file commit
overhead is invisible in real corpus sizes.

Tenant scoping
--------------

Every public method takes ``tenant_id`` as the first parameter --
no contextvar resolution. The route / CLI layers are responsible
for binding the value from the operator's JWT (the same shape
G0.4-T5's ``/api/v1/retrieve`` uses); the service is trivially
testable in isolation and the tenant boundary is auditable at the
call site rather than buried in a per-call lookup.

RBAC
----

This service does **not** enforce roles. ``delete_entry`` requires
the caller to have already validated tenant_admin role; the API
route (T2) and the CLI verb (T4) own the
:func:`~meho_backplane.auth.rbac.require_role` gate. Splitting RBAC
out of the service keeps the service callable from contexts where
the role discipline is different (a future ``meho admin reindex``
job that runs unattended at tenant-admin equivalence, for instance).

Search adaptation
-----------------

:meth:`search_entries` wraps
:func:`~meho_backplane.retrieval.retriever.retrieve` with
``source=KB_SOURCE`` pinned. The :class:`RetrievalHit` shape is
adapted to :class:`KbEntrySearchHit` (source_id renamed to slug,
body truncated to a snippet). The full body is recoverable through
:meth:`get_entry` keyed on the returned slug; the snippet exists so
agent flows can ``search → decide → fetch`` without round-tripping
every full body on every search.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Literal

import structlog
from sqlalchemy import delete, select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.kb.attribution import merge_attribution
from meho_backplane.kb.file_walker import walk_kb_directory
from meho_backplane.kb.schemas import (
    KB_KIND_ENTRY,
    KB_SOURCE,
    KbEntry,
    KbEntrySearchHit,
    KbIngestionResult,
    validate_slug,
)
from meho_backplane.retrieval.indexer import compute_body_hash, index_document
from meho_backplane.retrieval.retriever import retrieve

__all__ = ["KbService"]


#: First-N characters of a body returned in :class:`KbEntrySearchHit.snippet`
#: and in the preview field of :meth:`KbService.list_entries`. 200 is wide
#: enough for the consumer's typical kb entry first sentence + start of
#: second; narrow enough to keep the search response size bounded when an
#: agent paginates through many hits.
_SNIPPET_CHARS: int = 200

#: Default per-call paging cap for :meth:`KbService.list_entries`. Mirrors
#: the v0.1-spec L391 cap on retrieval hits; over the consumer's 44-entry
#: corpus the default returns every row in one shot, but the cap stays
#: in place so a corpus that grows past ~1k rows doesn't accidentally
#: stream the whole table back to a casual ``meho kb list``.
DEFAULT_LIST_LIMIT: int = 100


_IngestAction = Literal["inserted", "updated", "skipped"]


class KbService:
    """Tenant-scoped CRUD + ingest + search over kb entries.

    Stateless and async; instantiate once per request (or once per
    long-running CLI session) and call freely. Each public method
    opens its own DB session, commits, and closes -- no shared
    transaction state across calls.

    The class deliberately ships with no constructor parameters: the
    session-per-method shape rules out a caller-owned session, and
    every dependency (the engine, the embedding service) is bound
    via module-level singletons that the existing G0.4 substrate
    set up. v0.2.next can add a constructor knob if a real use case
    needs request-level transaction batching across multiple service
    calls.
    """

    async def ingest_directory(
        self,
        directory: Path,
        tenant_id: uuid.UUID,
        *,
        dry_run: bool = False,
    ) -> KbIngestionResult:
        """Walk *directory*, ingest every ``.md`` file under it.

        Idempotent: a second call against an unchanged corpus
        produces ``skipped_count == <total files>`` because the body-
        hash short-circuit from G0.4-T3 skips re-embedding. A
        single-file body change between calls produces
        ``updated_count == 1, skipped_count == <total - 1>``.

        Per-file errors (binary file, unreadable bytes, invalid
        slug, malformed front-matter) are caught, counted, and
        appended to :attr:`KbIngestionResult.errors`. The ingestion
        run continues with the remaining files -- one bad file does
        not abort a 44-entry corpus.

        Parameters
        ----------
        directory
            Path to the kb directory. Must exist and be a directory;
            otherwise :class:`NotADirectoryError` /
            :class:`FileNotFoundError` propagates.
        tenant_id
            The tenant on whose behalf the corpus is being ingested.
            Bound by the caller from the operator's JWT.
        dry_run
            When ``True``, walk the directory and classify every
            file by the action it would take (insert / update /
            skip) but do not write to the DB. The returned counters
            reflect what would happen; ``errors`` still surfaces any
            walk-time failures (read error, parse error, slug error).
            Useful for ``meho kb ingest --dry-run`` (T4) before a
            destructive run.
        """
        log = structlog.get_logger()
        inserted = 0
        updated = 0
        skipped = 0
        errors: list[str] = []

        for record in walk_kb_directory(directory, errors=errors):
            try:
                action = await self._ingest_one(
                    tenant_id=tenant_id,
                    slug=record.slug,
                    body=record.body,
                    metadata=record.metadata,
                    source_path=record.path,
                    dry_run=dry_run,
                )
            except Exception as exc:
                # Database / embedding failures land here. The
                # ingestion run is a best-effort bulk operation;
                # per-file failures get counted and the next file
                # proceeds. The walker's own per-file errors
                # (read / parse / slug) are caught inside
                # ``walk_kb_directory`` and appended directly to
                # ``errors``; this catch covers post-walk failures
                # in the index path only.
                log.exception(
                    "kb_ingest_file_failed",
                    path=str(record.path),
                    tenant_id=str(tenant_id),
                )
                errors.append(f"{record.path}: {type(exc).__name__}: {exc}")
                continue

            if action == "inserted":
                inserted += 1
            elif action == "updated":
                updated += 1
            else:
                skipped += 1

        log.info(
            "kb_ingest_completed",
            tenant_id=str(tenant_id),
            directory=str(directory),
            inserted=inserted,
            updated=updated,
            skipped=skipped,
            errors=len(errors),
            dry_run=dry_run,
        )
        return KbIngestionResult(
            inserted_count=inserted,
            updated_count=updated,
            skipped_count=skipped,
            error_count=len(errors),
            errors=errors,
        )

    async def _ingest_one(
        self,
        *,
        tenant_id: uuid.UUID,
        slug: str,
        body: str,
        metadata: dict[str, object],
        source_path: Path,
        dry_run: bool,
    ) -> _IngestAction:
        """Classify the action for one file, then perform it unless *dry_run*.

        Returns ``"inserted"`` / ``"updated"`` / ``"skipped"`` per
        the comparison between the incoming body's SHA-256 hash and
        any existing row's. Enriches the metadata with the source
        path before delegating to
        :func:`~meho_backplane.retrieval.indexer.index_document` so
        the audit trail in ``documents.metadata`` records where the
        file came from.

        The classification SELECT is one extra round-trip per file
        compared to calling ``index_document`` directly; the cost is
        the price of the precise (inserted / updated / skipped)
        breakdown the acceptance criteria require. v0.2.next can
        collapse this into a single round-trip if the helper
        exposes the action in its return shape.
        """
        sessionmaker = get_sessionmaker()
        new_hash = compute_body_hash(body)
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

        enriched_metadata: dict[str, Any] = dict(metadata)
        enriched_metadata["source_path"] = str(source_path)

        await index_document(
            tenant_id=tenant_id,
            source=KB_SOURCE,
            source_id=slug,
            kind=KB_KIND_ENTRY,
            body=body,
            metadata=enriched_metadata,
        )
        return action

    async def list_entries(
        self,
        tenant_id: uuid.UUID,
        *,
        filter_pattern: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[KbEntry]:
        """Return up to *limit* kb entries for *tenant_id*, slug-sorted.

        Pure list, no retrieval -- this is the ``meho kb list`` (T4)
        and ``GET /api/v1/kb`` (T2) backend. Sorted by slug so a
        casual ``meho kb list`` output is predictable across runs.

        ``filter_pattern`` is a SQL ``LIKE`` pattern matched against
        the slug; pass ``"vcenter-%"`` to narrow to vcenter entries.
        The pattern is forwarded to SQLAlchemy unescaped -- callers
        that need literal ``%`` / ``_`` characters must escape them
        themselves. The caller is the trust boundary (route layer
        validates pattern shape); the service forwards.

        Parameters
        ----------
        tenant_id
            The owning tenant.
        filter_pattern
            Optional SQL ``LIKE`` pattern. ``None`` (default) returns
            every kb entry up to *limit*.
        limit
            Maximum rows returned. Default :data:`DEFAULT_LIST_LIMIT`
            (100); negative values raise :class:`ValueError` so a
            misconfigured caller surfaces at the boundary rather
            than silently truncating.
        offset
            Pagination offset. ``0`` (default) returns from the top.
            Negative offset raises :class:`ValueError`.
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0; got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be >= 0; got {offset}")
        if limit == 0:
            return []

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = (
                select(Document)
                .where(
                    Document.tenant_id == tenant_id,
                    Document.source == KB_SOURCE,
                )
                .order_by(Document.source_id)
                .limit(limit)
                .offset(offset)
            )
            if filter_pattern is not None:
                stmt = stmt.where(Document.source_id.like(filter_pattern))

            result = await session.execute(stmt)
            rows = result.scalars().all()

        return [_doc_to_entry(row) for row in rows]

    async def get_entry(self, tenant_id: uuid.UUID, slug: str) -> KbEntry | None:
        """Return the full :class:`KbEntry` for *(tenant_id, slug)*; ``None`` if absent.

        Backs ``meho kb show`` (T4) and ``GET /api/v1/kb/{slug}`` (T2).
        Slug is NOT re-validated here -- a caller passing an
        out-of-shape slug just gets ``None`` (no row will match the
        natural key) rather than a 422. The route / CLI layer does the
        validation before invoking the service; this method is the
        terminal read.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(Document).where(
                    Document.tenant_id == tenant_id,
                    Document.source == KB_SOURCE,
                    Document.source_id == slug,
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return _doc_to_entry(row)

    async def create_entry(
        self,
        tenant_id: uuid.UUID,
        slug: str,
        body: str,
        metadata: dict[str, object] | None = None,
        *,
        actor_sub: str | None = None,
    ) -> tuple[KbEntry, bool]:
        """Insert (or re-index) one kb entry; return ``(entry, created)``.

        Validates *slug* before touching the substrate. Delegates to
        :func:`~meho_backplane.retrieval.indexer.index_document` so
        the body-hash short-circuit applies -- callers writing an
        already-present entry with the same body pay zero embedding
        cost, just an ``updated_at`` bump.

        Used by ``POST /api/v1/kb`` (T2) and the MCP
        ``add_to_knowledge`` meta-tool (T3). RBAC (``tenant_admin``)
        enforced by the route layer.

        Cross-principal writes are wiki-like (any ``tenant_admin`` may
        overwrite any other principal's slug in-tenant -- intended, no
        ownership gate); this method only adds attribution via
        :func:`~meho_backplane.kb.attribution.merge_attribution`:
        ``created_by_sub`` set once and preserved across overwrites,
        ``last_updated_by_sub`` rewritten each write, both in
        ``doc_metadata`` and un-forgeable from caller ``metadata``.

        Returns ``(entry, created)`` where ``created`` is ``True`` only
        when no row existed for ``(tenant_id, slug)`` -- the route maps
        it to HTTP ``201`` vs ``200`` (#1845 ask 1). *actor_sub* is the
        writing principal's OIDC ``sub`` (``Operator.sub``); ``None``
        leaves the row unattributed. *metadata* ``None`` keeps the
        existing caller-metadata, ``{}`` clears it; *slug* is validated
        (raises :class:`~meho_backplane.kb.schemas.InvalidKbSlugError`).
        """
        validate_slug(slug)

        # One natural-key SELECT, ahead of the substrate upsert. It
        # serves two purposes: (1) the created-vs-overwrite signal for
        # the route's 201/200 decision, and (2) the prior
        # ``created_by_sub`` to preserve across an overwrite. The
        # substrate's own SELECT inside ``index_document`` is a second
        # round-trip; collapsing the two is a v0.2.next optimisation
        # (index_document would have to return the action + prior
        # metadata), not worth the wider blast radius here.
        existing = await self.get_entry(tenant_id=tenant_id, slug=slug)
        created = existing is None

        merged = merge_attribution(
            caller_metadata=metadata,
            existing_metadata=existing.metadata if existing is not None else None,
            actor_sub=actor_sub,
            created=created,
        )

        doc = await index_document(
            tenant_id=tenant_id,
            source=KB_SOURCE,
            source_id=slug,
            kind=KB_KIND_ENTRY,
            body=body,
            metadata=merged,
        )
        return _doc_to_entry(doc), created

    async def delete_entry(self, tenant_id: uuid.UUID, slug: str) -> bool:
        """Delete the kb entry matching *(tenant_id, slug)*. Return whether it existed.

        Backs ``DELETE /api/v1/kb/{slug}`` (T2) and a future
        ``remove_from_knowledge`` MCP tool. RBAC (``tenant_admin``)
        enforced by the route layer -- this method assumes the
        caller is already authorised.

        Returns
        -------
        bool
            ``True`` when a row was deleted; ``False`` when no row
            matched the natural key. The route layer translates
            ``False`` to 404.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = delete(Document).where(
                Document.tenant_id == tenant_id,
                Document.source == KB_SOURCE,
                Document.source_id == slug,
            )
            result = await session.execute(stmt)
            await session.commit()
        # SQLAlchemy 2.x types DML execute() as ``Result[Any]`` whose
        # ``rowcount`` is only typed on the concrete ``CursorResult``
        # subclass. Casting through ``CursorResult`` is heavier than
        # the simple ``getattr`` probe; the DML execute path always
        # returns a cursor-shaped result on every supported driver
        # (asyncpg + aiosqlite both honour the contract) so a bare
        # access is correct at runtime, the cast just silences mypy.
        rowcount: int = result.rowcount  # type: ignore[attr-defined]
        return rowcount > 0

    async def search_entries(
        self,
        tenant_id: uuid.UUID,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> list[KbEntrySearchHit]:
        """Hybrid BM25 + cosine retrieval scoped to *tenant_id*'s kb corpus.

        Wraps :func:`~meho_backplane.retrieval.retriever.retrieve`
        with ``source=KB_SOURCE`` pinned. The
        :class:`~meho_backplane.retrieval.retriever.RetrievalHit`
        shape is adapted to :class:`KbEntrySearchHit` (slug instead
        of source_id, snippet instead of full body, kb-shaped
        vocabulary).

        Parameters
        ----------
        tenant_id
            Querying tenant. The substrate's tenant scoping plus
            this method's pinned ``source`` means cross-tenant
            retrieval is structurally impossible.
        query
            Free-form query string. Substrate consumes via
            ``plainto_tsquery`` for BM25 and the embedding service
            for cosine.
        filters
            Optional :class:`dict`. The ``"kind"`` key narrows within
            the kb namespace (e.g. ``kind="kb-entry"`` to exclude
            future ``kb-index`` rows); every other key is forwarded
            to the substrate as a ``metadata_filters`` containment
            predicate against ``documents.metadata`` (G4.4-T1 /
            #1177). Pass e.g. ``{"kind": "kb-entry", "source_kind":
            "evoila-distilled"}`` to scope hits to the curated
            slice. Values must be JSON scalars -- the substrate's
            ``@>`` containment is shape-flat-only by contract.
        limit
            Maximum hits to return. Default 10; capped at 50 by the
            substrate (and the API surface).
        """
        kind_filter: str | None = None
        metadata_filters: dict[str, Any] | None = None
        if filters is not None:
            kind = filters.get("kind")
            if isinstance(kind, str):
                kind_filter = kind
            # Everything other than ``kind`` flows to the substrate's
            # ``metadata_filters`` -- the v0.2 "currently ignored"
            # disclaimer in the MCP tool description goes away once
            # this path lights up the substrate primitive (G4.4-T1 /
            # #1177). An empty residual dict normalises to ``None``
            # so the substrate skips the predicate.
            residual = {k: v for k, v in filters.items() if k != "kind"}
            metadata_filters = residual or None

        hits = await retrieve(
            tenant_id=tenant_id,
            query=query,
            source=KB_SOURCE,
            kind=kind_filter,
            limit=limit,
            metadata_filters=metadata_filters,
        )
        return [
            KbEntrySearchHit(
                slug=hit.source_id,
                snippet=_make_snippet(hit.body),
                metadata=hit.doc_metadata,
                fused_score=hit.fused_score,
                bm25_score=hit.bm25_score,
                cosine_score=hit.cosine_score,
                bm25_rank=hit.bm25_rank,
                cosine_rank=hit.cosine_rank,
            )
            for hit in hits
        ]


def _doc_to_entry(doc: Document) -> KbEntry:
    """Adapt a :class:`Document` ORM row to a :class:`KbEntry`.

    The kb-shaped vocabulary renames ``source_id`` to ``slug`` and
    drops the ``source`` / ``kind`` / ``body_hash`` / ``tokens`` /
    ``embedding`` columns the substrate carries (callers don't need
    them; the API + MCP surfaces deliberately don't expose them).
    """
    return KbEntry(
        id=doc.id,
        tenant_id=doc.tenant_id,
        slug=doc.source_id,
        body=doc.body,
        metadata=doc.doc_metadata,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def _make_snippet(body: str) -> str:
    """Return the first :data:`_SNIPPET_CHARS` of *body* with an ellipsis if truncated.

    Uses character-count truncation rather than word-boundary cleavage
    because the consumer's kb is technical content (commands, code
    fences) where mid-token cuts are acceptable and the alternative
    (sentence-tokenising every search hit) is expensive enough to
    show up under load. The ellipsis (``…``, U+2026) gives the
    operator a visual cue that the hit has more content available
    via :meth:`KbService.get_entry`.
    """
    if len(body) <= _SNIPPET_CHARS:
        return body
    return body[:_SNIPPET_CHARS] + "…"

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

Write policies
--------------

Two write paths with two different rules:

* **Directory ingest** (:meth:`ingest_directory`) reads every file
  first, in sorted relative-path order, and finds every slug claimed
  by more than one file *before* it writes anything. Those files are
  reported as conflicts and not written, so an existing entry with a
  conflicting slug stays unchanged. Filesystem listing order can no
  longer decide which file wins.
* **Direct create** (:meth:`create_entry`) is an explicit replace:
  the caller names one slug and the newest write wins. The route
  records ``audit_created`` so a replace is visible in the audit log.
  There is no expected-revision check; that waits for a real
  multi-writer workflow.

Both paths stamp a provenance block (source kind, relative path,
write time) through :mod:`meho_backplane.kb.provenance`. No absolute
host path is stored.

Both paths write through :func:`meho_backplane.kb.revisions.write_entry`:
the ``documents`` row stays the latest head, and each changed body is
also kept as an immutable ``kb_revision`` row in the same transaction.
:meth:`KbService.get_revision` reads one exact revision;
:meth:`KbService.delete_entry` removes the head and its revisions
together.

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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.kb.attribution import merge_attribution
from meho_backplane.kb.directory_ingest import KbIngestRootError
from meho_backplane.kb.directory_ingest import ingest_directory as _ingest_directory
from meho_backplane.kb.provenance import read_provenance, stamp_provenance
from meho_backplane.kb.revisions import (
    current_revision_id,
    delete_entry_rows,
    get_revision,
    write_entry,
)
from meho_backplane.kb.schemas import (
    KB_SOURCE,
    KbEntry,
    KbEntryRevision,
    KbEntrySearchHit,
    KbIngestionResult,
    validate_slug,
)
from meho_backplane.retrieval.retriever import retrieve

__all__ = ["KbIngestRootError", "KbService"]


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
        """Ingest every ``.md`` file under *directory* (see :mod:`.directory_ingest`).

        Sorted pre-walk; slugs claimed by two or more files are reported
        as conflicts and not written; per-file errors are counted and
        the run continues. Raises :class:`KbIngestRootError` when
        *directory* resolves outside ``KB_INGEST_ROOT`` (#101).
        """
        return await _ingest_directory(directory, tenant_id, dry_run=dry_run)

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
                select(Document, current_revision_id())
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
            rows = result.all()

        return [_doc_to_entry(doc, revision_id) for doc, revision_id in rows]

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
                select(Document, current_revision_id()).where(
                    Document.tenant_id == tenant_id,
                    Document.source == KB_SOURCE,
                    Document.source_id == slug,
                )
            )
            row = result.one_or_none()
        if row is None:
            return None
        doc, revision_id = row
        return _doc_to_entry(doc, revision_id)

    async def get_revision(
        self,
        tenant_id: uuid.UUID,
        entry_id: uuid.UUID,
        revision_id: uuid.UUID,
    ) -> KbEntryRevision | None:
        """Return one retained revision of an entry; ``None`` when not visible.

        Backs ``meho://kb/id/{entry}/revision/{revision}``. The revision
        must belong to *entry_id* and both must belong to *tenant_id*.
        A foreign, deleted, mismatched or unknown id gives ``None`` --
        the caller cannot tell these cases apart. Role checks stay with
        the caller, as for every other method here.
        """
        return await get_revision(tenant_id, entry_id, revision_id)

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

        This is the explicit replace path: last write wins. A second
        create for the same slug replaces body and metadata with no
        expected-revision check; the route maps ``created=False`` to
        HTTP 200 and records ``audit_created`` so the replace is
        auditable. Directory ingest follows a different rule (see
        :meth:`ingest_directory`). Every write stamps a provenance
        block with ``source_kind="direct"`` and no relative path,
        replacing any ``provenance`` key the caller sent.

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
        if merged is None:
            # No caller metadata and no actor: keep the existing keys,
            # as ``index_document(metadata=None)`` would, but still
            # record this write's provenance.
            merged = dict(existing.metadata) if existing is not None else {}
        merged = stamp_provenance(
            merged,
            source_kind="direct",
            relative_path=None,
            ingested_at=datetime.now(UTC),
        )

        doc, revision_id = await write_entry(
            tenant_id=tenant_id,
            slug=slug,
            body=body,
            metadata=merged,
        )
        return _doc_to_entry(doc, revision_id), created

    async def delete_entry(self, tenant_id: uuid.UUID, slug: str) -> bool:
        """Delete the kb entry matching *(tenant_id, slug)*. Return whether it existed.

        Backs ``DELETE /api/v1/kb/{slug}`` (T2) and a future
        ``remove_from_knowledge`` MCP tool. RBAC (``tenant_admin``)
        enforced by the route layer -- this method assumes the
        caller is already authorised.

        The entry's retained revisions are deleted in the same
        transaction as the head row, so the body stops being searchable
        and every ``meho://kb/id/...`` read of it stops working at the
        same moment. If the delete fails, nothing is removed and the
        error propagates; the caller can retry.

        Returns
        -------
        bool
            ``True`` when a row was deleted; ``False`` when no row
            matched the natural key. The route layer translates
            ``False`` to 404.
        """
        return await delete_entry_rows(tenant_id, slug)

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


def _doc_to_entry(doc: Document, revision_id: uuid.UUID | None) -> KbEntry:
    """Adapt a :class:`Document` ORM row and its current revision id to a :class:`KbEntry`.

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
        provenance=read_provenance(doc.doc_metadata),
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        revision_id=revision_id,
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

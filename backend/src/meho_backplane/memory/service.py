# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``MemoryService`` -- tenant-scoped wrapper over the documents table.

G5.1-T1 (#421). Exposes the five memory verbs the consumer-needs
spec names (``remember`` / ``recall`` / ``list`` / ``forget`` /
``search``) over the G0.4 retrieval substrate. Every method takes an
:class:`~meho_backplane.auth.operator.Operator` and routes both the
tenant boundary (via ``documents.tenant_id``) and the per-scope RBAC
matrix through the same code path -- the API surface (T2 #422), MCP
meta-tools (T3 #423), and CLI verbs (T4 #424) consume this class as
a thin shell and never re-derive the matrix.

Pure helpers live in :mod:`meho_backplane.memory._internal` (encoding,
serialisation, metadata extraction); this module is the class wiring.

Expiry handling
---------------

``expires_at`` is stored in ``doc_metadata`` rather than as a
dedicated column -- the substrate is shared with G4 kb (no expiry
concept), and adding a NULL-able expiry column to the table for one
consumer would pollute the schema. The read paths in :meth:`recall` /
:meth:`list_memories` / :meth:`search_memories` filter out rows whose
stored ``expires_at`` lies in the past unless the caller opts in via
``include_expired=True``. G5.2 #374's daily cleanup task is what
*deletes* expired rows; G5.1-T1's contract is only the read-side
filter.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import delete, select

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.memory._internal import (
    EPOCH,
    MEMORY_SOURCE,
    auto_slug,
    build_metadata,
    document_to_entry,
    encode_source_id,
    has_tag,
    is_expired,
    metadata_datetime,
    metadata_str,
    slug_from_source_id,
)
from meho_backplane.memory.rbac import MemoryRbacResolver, PermissionDeniedError
from meho_backplane.memory.schemas import (
    TARGET_SCOPED,
    MemoryEntry,
    MemoryEntrySearchHit,
    MemoryScope,
    kind_for_scope,
    scope_for_kind,
    validate_slug,
)
from meho_backplane.retrieval.indexer import index_document
from meho_backplane.retrieval.retriever import RetrievalHit, retrieve

__all__ = ["MemoryService"]


class MemoryService:
    """Tenant-scoped memory service over the ``documents`` table.

    Constructor takes an optional :class:`MemoryRbacResolver` so tests
    can inject a fake; production callers leave it ``None`` and the
    service builds one internally. The service does not hold a DB
    session -- every method opens its own via
    :func:`~meho_backplane.db.engine.get_sessionmaker`.
    """

    def __init__(self, rbac: MemoryRbacResolver | None = None) -> None:
        self._rbac = rbac if rbac is not None else MemoryRbacResolver()
        self._log = structlog.get_logger()

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    async def remember(
        self,
        operator: Operator,
        scope: MemoryScope,
        body: str,
        slug: str | None = None,
        metadata: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
        target_name: str | None = None,
    ) -> MemoryEntry:
        """Persist a memory in *scope* for *operator*.

        ``slug`` is auto-generated (12-char UUID hex prefix) when
        ``None``. ``metadata`` is merged with the bookkeeping fields
        the service owns (``user_sub``, ``target_name``,
        ``expires_at``); a value the caller already supplies for one
        of those keys is overwritten by the service-derived value to
        keep the row's encoding canonical.

        Raises
        ------
        PermissionDeniedError
            When the RBAC matrix denies the write (e.g. an ``operator``
            role attempting to write a ``TENANT``-scoped memory).
        ValueError
            When ``target_name`` is missing for a target-scoped write.
        """
        self._require_target_name(scope, target_name)
        if not self._rbac.can_write(operator, scope, target_name):
            raise PermissionDeniedError(
                scope,
                f"role={operator.tenant_role.value} cannot write scope={scope.value}",
            )

        # validate_slug runs in addition to MemoryEntryCreate's Field
        # pattern so direct callers (not going through Pydantic) cannot
        # smuggle a slug containing ``:`` past the safe-character gate;
        # the ``source_id`` encoding scheme is asymmetric on colons and
        # would silently truncate the returned MemoryEntry.slug on read.
        resolved_slug = validate_slug(slug) if slug is not None else auto_slug()
        source_id = encode_source_id(
            scope=scope,
            user_sub=operator.sub,
            target_name=target_name,
            slug=resolved_slug,
        )
        merged_metadata = build_metadata(
            caller_metadata=metadata,
            scope=scope,
            user_sub=operator.sub,
            target_name=target_name,
            expires_at=expires_at,
        )

        doc = await index_document(
            tenant_id=operator.tenant_id,
            source=MEMORY_SOURCE,
            source_id=source_id,
            kind=kind_for_scope(scope),
            body=body,
            metadata=merged_metadata,
        )
        self._log.info(
            "memory_remember",
            tenant_id=str(operator.tenant_id),
            operator_sub=operator.sub,
            scope=scope.value,
            slug=resolved_slug,
            target_name=target_name,
        )
        return document_to_entry(doc)

    async def forget(
        self,
        operator: Operator,
        scope: MemoryScope,
        slug: str,
        target_name: str | None = None,
    ) -> bool:
        """Delete one memory by ``(scope, slug)``. RBAC mirrors write.

        Returns ``True`` when a row was deleted, ``False`` when no
        row matched the natural key (idempotent delete). Same RBAC as
        :meth:`remember`; user-scoped forgets are gated by the
        natural-key encoding (different users yield different
        ``source_id`` so an operator deleting their own memory cannot
        reach another operator's row in the same scope).

        Raises
        ------
        PermissionDeniedError
            When the RBAC matrix denies the action.
        ValueError
            When ``target_name`` is missing for a target-scoped delete.
        """
        self._require_target_name(scope, target_name)
        if not self._rbac.can_write(operator, scope, target_name):
            raise PermissionDeniedError(
                scope,
                f"role={operator.tenant_role.value} cannot forget scope={scope.value}",
            )
        source_id = encode_source_id(
            scope=scope,
            user_sub=operator.sub,
            target_name=target_name,
            slug=slug,
        )
        deleted = await self._delete_by_natural_key(operator, source_id)
        self._log.info(
            "memory_forget",
            tenant_id=str(operator.tenant_id),
            operator_sub=operator.sub,
            scope=scope.value,
            slug=slug,
            deleted=deleted,
        )
        return deleted

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    async def recall(
        self,
        operator: Operator,
        scope: MemoryScope,
        slug: str,
        target_name: str | None = None,
    ) -> MemoryEntry | None:
        """Fetch one memory by ``(scope, slug)``. ``None`` on not-found OR RBAC deny.

        The 404-vs-403 collapse is the load-bearing info-leak
        avoidance from the issue AC: a caller cannot distinguish "no
        such memory" from "you don't have access" by the response
        shape. The HTTP route (T2) renders both as 404.

        Expired entries are filtered out (G5.2's executor physically
        removes them on its daily cadence; the read-side filter is
        what gates visibility between expiry and reap).
        """
        if scope in TARGET_SCOPED and target_name is None:
            return None
        source_id = encode_source_id(
            scope=scope,
            user_sub=operator.sub,
            target_name=target_name,
            slug=slug,
        )
        doc = await self._fetch_by_natural_key(operator, source_id)
        if doc is None:
            # For user-flavoured scopes the user_sub is in the
            # source_id encoding, so an operator trying to recall
            # someone else's user-scoped memory gets None here -- the
            # info-leak avoidance hits at the natural-key layer
            # before RBAC is consulted. The RBAC check below still
            # runs on tenant/target rows so the matrix is consulted
            # on every read path.
            return None

        stored_user_sub = metadata_str(doc.doc_metadata, "user_sub")
        stored_target = metadata_str(doc.doc_metadata, "target_name")
        if not self._rbac.can_read(
            operator,
            scope,
            user_sub=stored_user_sub,
            target_name=stored_target,
        ):
            return None
        entry = document_to_entry(doc)
        if entry.expires_at is not None and is_expired(entry.expires_at):
            return None
        return entry

    async def list_memories(
        self,
        operator: Operator,
        *,
        scope: MemoryScope | None = None,
        slug_pattern: str | None = None,
        tag: str | None = None,
        include_expired: bool = False,
        limit: int = 100,
    ) -> list[MemoryEntry]:
        """List memories the operator can read in this tenant.

        SQL-side filtering on ``tenant_id`` + ``source='memory'`` +
        (optional) ``kind`` for the scope filter; in-process filtering
        on ``user_sub`` (for user-scoped rows), ``slug_pattern``
        (substring), ``tag`` (membership in ``metadata.tags``), and
        ``expires_at`` (lifted from ``doc_metadata``). The in-process
        layer is acceptable for v0.2 -- memory corpora per tenant are
        small (consumer-needs.md L131 names ~15 files on Damir's
        laptop); SQL-side promotion is the v0.2.next escape hatch.

        ``limit`` caps the *returned* list, not the candidate set;
        the server-side query pulls up to ``limit * 4`` rows before
        in-process filtering so common cases (some rows expired, some
        belong to other operators in the tenant) still return a full
        page when filters trim the candidate pool.
        """
        if limit < 1:
            return []
        candidate_kinds = (
            [kind_for_scope(scope)] if scope is not None else self._rbac.visible_kinds(operator)
        )
        # Pull a wider candidate set so in-process filters don't
        # under-fill the page for the common "some expired / cross-user"
        # case. The 4x multiplier is empirical: tenant-shared corpora
        # rarely have >25% expired/cross-user rows.
        candidate_pull = max(limit * 4, 200)
        docs = await self._list_documents(operator, candidate_kinds, candidate_pull)

        entries: list[MemoryEntry] = []
        for doc in docs:
            entry = document_to_entry(doc)
            if not self._entry_passes_filters(
                entry,
                operator=operator,
                include_expired=include_expired,
                slug_pattern=slug_pattern,
                tag=tag,
            ):
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
        return entries

    async def search_memories(
        self,
        operator: Operator,
        query: str,
        *,
        scope: MemoryScope | None = None,
        limit: int = 10,
    ) -> list[MemoryEntrySearchHit]:
        """Hybrid BM25 + cosine search over the operator's visible memories.

        Wraps :func:`~meho_backplane.retrieval.retriever.retrieve`
        with ``source='memory'`` and (optional) ``kind=<scope>``;
        post-filters the ranked hits through the RBAC matrix on
        ``user_sub`` so an operator's search never surfaces another
        operator's user-scoped row even when retrieval ranked it
        highly. Expired entries are filtered out (same contract as
        :meth:`list_memories` with ``include_expired=False``).
        """
        if limit < 1:
            return []
        kind_filter = kind_for_scope(scope) if scope is not None else None
        retrieval_limit = max(limit * 4, 50)
        hits = await retrieve(
            tenant_id=operator.tenant_id,
            query=query,
            source=MEMORY_SOURCE,
            kind=kind_filter,
            limit=retrieval_limit,
        )
        results: list[MemoryEntrySearchHit] = []
        for hit in hits:
            converted = self._hit_to_search_result(operator, hit)
            if converted is None:
                continue
            results.append(converted)
            if len(results) >= limit:
                break
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _require_target_name(scope: MemoryScope, target_name: str | None) -> None:
        """Raise :class:`ValueError` when target-scoped writes lack a target name.

        Service-level surface for the AC "target_name required when
        scope ∈ {USER_TARGET, TARGET}". Surfaces *before* RBAC so the
        API layer (T2 #422) can map this to 422 Unprocessable Entity
        and keep the matrix mismatch (403) distinct.
        """
        if scope in TARGET_SCOPED and target_name is None:
            raise ValueError(f"target_name is required for scope={scope.value}")

    async def _fetch_by_natural_key(self, operator: Operator, source_id: str) -> Document | None:
        """Single-row fetch by the ``(tenant_id, source, source_id)`` key."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(Document).where(
                    Document.tenant_id == operator.tenant_id,
                    Document.source == MEMORY_SOURCE,
                    Document.source_id == source_id,
                )
            )
            return result.scalar_one_or_none()

    async def _delete_by_natural_key(self, operator: Operator, source_id: str) -> bool:
        """Existence-probe + delete; returns whether a row was removed.

        Two round-trips (select-then-delete) keep the return-value
        contract type-safe under ``mypy --strict``: SQLAlchemy 2.x's
        :class:`Result` does not statically expose ``rowcount`` (only
        the runtime :class:`CursorResult` subclass does). A single-row
        natural-key existence probe is cheap enough that the extra
        trip is invisible at memory's request rates.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            existing = await session.execute(
                select(Document.id).where(
                    Document.tenant_id == operator.tenant_id,
                    Document.source == MEMORY_SOURCE,
                    Document.source_id == source_id,
                )
            )
            doc_id = existing.scalar_one_or_none()
            if doc_id is None:
                return False
            await session.execute(delete(Document).where(Document.id == doc_id))
            await session.commit()
            return True

    async def _list_documents(
        self,
        operator: Operator,
        candidate_kinds: list[str],
        candidate_pull: int,
    ) -> list[Document]:
        """Pull the candidate ``Document`` rows for an in-process filter pass.

        Ordered by ``updated_at desc`` so the most recently-written
        memories surface first; matches the operator-intuition contract
        ("show me what I just wrote") for the list verb.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(Document)
                .where(
                    Document.tenant_id == operator.tenant_id,
                    Document.source == MEMORY_SOURCE,
                    Document.kind.in_(candidate_kinds),
                )
                .order_by(Document.updated_at.desc())
                .limit(candidate_pull)
            )
            return list(result.scalars().all())

    def _entry_passes_filters(
        self,
        entry: MemoryEntry,
        *,
        operator: Operator,
        include_expired: bool,
        slug_pattern: str | None,
        tag: str | None,
    ) -> bool:
        """Apply the in-process filter chain shared by ``list_memories``.

        Returns ``True`` when the entry should be included in the
        result. Centralised so the matrix is consulted exactly once
        per row and the filter ordering (RBAC -> expiry -> slug -> tag)
        is the same shape ``search_memories`` uses below.
        """
        if not self._rbac.can_read(
            operator,
            entry.scope,
            user_sub=entry.user_sub,
            target_name=entry.target_name,
        ):
            return False
        if not include_expired and entry.expires_at is not None and is_expired(entry.expires_at):
            return False
        if slug_pattern is not None and slug_pattern not in entry.slug:
            return False
        return not (tag is not None and not has_tag(entry.metadata, tag))

    def _hit_to_search_result(
        self, operator: Operator, hit: RetrievalHit
    ) -> MemoryEntrySearchHit | None:
        """Convert one retrieval hit to a :class:`MemoryEntrySearchHit` or drop it.

        ``None`` is returned when:

        * the hit's ``kind`` is not a recognised memory kind (defensive
          drop with a structured warn log -- the substrate's kind
          filter should have prevented this),
        * the RBAC matrix denies the operator the read,
        * the stored ``expires_at`` is in the past.

        Otherwise returns the ranked search hit with the retrieval
        substrate's per-signal scores attached. The retrieval
        substrate does not expose ``created_at`` / ``updated_at``
        through :class:`RetrievalHit`; v0.2 surfaces :data:`EPOCH`
        as the placeholder and the API layer (T2 #422) renders these
        as ``null``.
        """
        try:
            hit_scope = scope_for_kind(hit.kind)
        except ValueError:
            self._log.warning(
                "memory_search_unknown_kind",
                tenant_id=str(operator.tenant_id),
                kind=hit.kind,
            )
            return None
        user_sub = metadata_str(hit.doc_metadata, "user_sub")
        target_name = metadata_str(hit.doc_metadata, "target_name")
        if not self._rbac.can_read(
            operator,
            hit_scope,
            user_sub=user_sub,
            target_name=target_name,
        ):
            return None
        expires_at = metadata_datetime(hit.doc_metadata, "expires_at")
        if expires_at is not None and is_expired(expires_at):
            return None
        entry = MemoryEntry(
            id=hit.document_id,
            tenant_id=hit.tenant_id,
            scope=hit_scope,
            slug=slug_from_source_id(hit.source_id),
            body=hit.body,
            metadata=dict(hit.doc_metadata),
            expires_at=expires_at,
            user_sub=user_sub,
            target_name=target_name,
            created_at=EPOCH,
            updated_at=EPOCH,
        )
        return MemoryEntrySearchHit(
            entry=entry,
            fused_score=hit.fused_score,
            bm25_score=hit.bm25_score,
            cosine_score=hit.cosine_score,
            bm25_rank=hit.bm25_rank,
            cosine_rank=hit.cosine_rank,
        )

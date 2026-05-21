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

import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.memory._internal import (
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
from meho_backplane.memory.rbac import (
    MemoryRbacResolver,
    PermissionDeniedError,
    assert_can_promote,
)
from meho_backplane.memory.schemas import (
    TARGET_SCOPED,
    USER_SCOPED,
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

    async def promote(
        self,
        operator: Operator,
        *,
        source_scope: MemoryScope,
        source_slug: str,
        target_scope: MemoryScope,
        move: bool = False,
        target_name: str | None = None,
    ) -> MemoryEntry | None:
        """Promote one memory from *source_scope* to a strictly broader *target_scope*.

        G5.2-T4 (#626). Five-phase pipeline: load source under read-RBAC ->
        authorize via T3's :func:`assert_can_promote` -> build target metadata
        with ``promoted_from`` marker and cleared ``expires_at`` -> one-
        transaction probe-insert(+delete-on-move) -> return target row.

        * Returns ``None`` when the source is absent OR not visible to
          *operator* (the 404 collapse the route renders; preserves the
          tenant boundary against cross-tenant existence probes).
        * Raises :class:`InvalidPromotionStepError` (route: 400),
          :class:`PermissionDeniedError` (route: 403,
          ``insufficient_promotion_authority``),
          :class:`NotImplementedError` (route: 501; per-target ACL gap
          from G0.3 #224), or :class:`ValueError`
          ``promote_target_conflict`` (route: 409; target slug owned by
          a different provenance). Errors propagate unchanged; the
          service layer is FastAPI-free.
        * Idempotency: re-running the same promotion returns the
          existing target row (not a 409 and not a duplicate insert).
          Same-source detection is by ``metadata.promoted_from`` match.
        * Atomicity: target insert + optional source delete share one
          :class:`AsyncSession`; a failed target insert rolls back any
          in-flight delete (the "failed target insert leaves source
          intact" AC).
        * TTL clearing: the target row's ``metadata.expires_at`` is
          always ``None``. Broader-scope memories are intentional and
          long-lived per Initiative #374.

        ``target_name`` is inherited from the source row when the
        target scope is target-flavoured and the caller didn't supply
        one (the ``user-target -> target`` ladder step).
        """
        source_doc = await self._load_source_for_promotion(
            operator=operator,
            source_scope=source_scope,
            source_slug=source_slug,
        )
        if source_doc is None:
            return None

        # target_name inheritance for the ``user-target -> target``
        # ladder step (source row carries the name; route body may
        # omit it).
        resolved_target_name = target_name
        if resolved_target_name is None and target_scope in TARGET_SCOPED:
            resolved_target_name = metadata_str(source_doc.doc_metadata, "target_name")

        # T3 helper owns the ladder + role matrix; errors propagate.
        assert_can_promote(
            operator,
            source_scope,
            target_scope,
            target_id=resolved_target_name,
        )

        target_source_id = encode_source_id(
            scope=target_scope,
            user_sub=operator.sub,
            target_name=resolved_target_name,
            slug=source_slug,
        )
        source_marker = f"{source_scope.value}/{source_slug}"
        target_metadata = self._build_promotion_metadata(
            source_doc=source_doc,
            target_scope=target_scope,
            target_name=resolved_target_name,
            source_marker=source_marker,
        )

        target_doc = await self._commit_promotion(
            operator=operator,
            source_doc=source_doc,
            source_scope=source_scope,
            source_slug=source_slug,
            target_scope=target_scope,
            target_source_id=target_source_id,
            target_metadata=target_metadata,
            source_marker=source_marker,
            move=move,
        )
        return document_to_entry(target_doc)

    async def _commit_promotion(
        self,
        *,
        operator: Operator,
        source_doc: Document,
        source_scope: MemoryScope,
        source_slug: str,
        target_scope: MemoryScope,
        target_source_id: str,
        target_metadata: dict[str, Any],
        source_marker: str,
        move: bool,
    ) -> Document:
        """One-transaction commit for :meth:`promote`. Idempotency + atomicity.

        Pulled out of :meth:`promote` so the caller stays focused on the
        load + authorize phases. Same single-session lifecycle the
        inline original used; the split is purely for readability and
        per-function-size compliance.

        Returns the target :class:`Document` -- either freshly inserted
        or the existing-from-idempotent-rerun row. Raises
        :class:`ValueError` ``promote_target_conflict`` when the target
        slug is occupied by a row with a different provenance.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            existing = await self._fetch_doc_in_session(
                session=session,
                tenant_id=operator.tenant_id,
                source_id=target_source_id,
            )
            if existing is not None:
                # Idempotent same-source re-run: return the existing
                # row, no insert, no source delete (move=True on a
                # re-run would otherwise delete an already-moved
                # source the operator can't see).
                existing_marker = metadata_str(existing.doc_metadata, "promoted_from")
                if existing_marker == source_marker:
                    self._log.info(
                        "memory_promote_idempotent",
                        tenant_id=str(operator.tenant_id),
                        operator_sub=operator.sub,
                        source_scope=source_scope.value,
                        target_scope=target_scope.value,
                        slug=source_slug,
                    )
                    return existing
                # An unrelated row already occupies the target slug --
                # 409 territory. Don't silently overwrite.
                raise ValueError(
                    f"promote_target_conflict: target slug {source_slug!r} at scope "
                    f"{target_scope.value!r} already exists with a different provenance"
                )

            target_doc = await index_document(
                tenant_id=operator.tenant_id,
                source=MEMORY_SOURCE,
                source_id=target_source_id,
                kind=kind_for_scope(target_scope),
                body=source_doc.body,
                metadata=target_metadata,
                session=session,
            )

            if move:
                await session.delete(source_doc)

            await session.commit()

        self._log.info(
            "memory_promote",
            tenant_id=str(operator.tenant_id),
            operator_sub=operator.sub,
            source_scope=source_scope.value,
            target_scope=target_scope.value,
            slug=source_slug,
            move=move,
        )
        return target_doc

    async def _load_source_for_promotion(
        self,
        *,
        operator: Operator,
        source_scope: MemoryScope,
        source_slug: str,
    ) -> Document | None:
        """Load + RBAC-check the source row for :meth:`promote`.

        Returns ``None`` when the row is absent OR not visible to
        *operator*. The natural-key encoding for user-flavoured scopes
        already binds ``operator.sub`` so an operator probing for
        another operator's user-scoped memory gets ``None`` at the SQL
        layer; the ``can_read`` check below adds the target-/tenant-
        flavoured visibility post-filter.

        Pulling the load + visibility check into a helper keeps
        :meth:`promote`'s control flow flat (one early-return for the
        invisible-source branch) and gives the route a single
        ``None``-means-404 contract.
        """
        # For user-flavoured source scopes the natural-key encoding
        # uses operator.sub, which is correct (an operator promoting
        # their own memory). For target-flavoured source scopes the
        # natural key needs target_name; we cannot resolve it until
        # we've loaded the row, so we use a wider SQL query that
        # bypasses the natural-key encoding.
        if source_scope in USER_SCOPED:
            source_id = encode_source_id(
                scope=source_scope,
                user_sub=operator.sub,
                # USER_TARGET requires target_name in the encoding;
                # promotion of a USER_TARGET source needs the caller
                # to supply target_name -- which v0.2 doesn't expose
                # on the route body. Returning None for this branch
                # is the conservative behaviour until G5.2-T5 wires
                # the CLI verb with the explicit target_name kwarg.
                target_name=None if source_scope is not MemoryScope.USER_TARGET else "",
                slug=source_slug,
            )
            # For USER_TARGET the empty target_name yields a non-
            # matching source_id, returning None -- which the route
            # surfaces as 404. T5's CLI verb will widen this path.
            doc = await self._fetch_by_natural_key(operator, source_id)
        else:
            # Tenant / target source scope: natural key has no per-
            # operator component, so a single column-filter query is
            # enough. Tenant boundary is in tenant_id.
            doc = await self._fetch_first_for_scope(
                operator=operator,
                source_scope=source_scope,
                source_slug=source_slug,
            )

        if doc is None:
            return None

        stored_user_sub = metadata_str(doc.doc_metadata, "user_sub")
        stored_target_name = metadata_str(doc.doc_metadata, "target_name")
        if not self._rbac.can_read(
            operator,
            source_scope,
            user_sub=stored_user_sub,
            target_name=stored_target_name,
        ):
            return None
        return doc

    async def _fetch_first_for_scope(
        self,
        *,
        operator: Operator,
        source_scope: MemoryScope,
        source_slug: str,
    ) -> Document | None:
        """Tenant- / target-scoped source-row fetch by ``(kind, slug)``.

        For ``TENANT`` and ``TARGET`` source scopes the natural-key
        encoding is enough to identify a unique row (``tenant:<slug>``
        for TENANT; the TARGET branch needs a target_name we don't yet
        have, so this method's caller invokes it via a SQL ``LIKE``
        match on the slug suffix). Used only by :meth:`promote`.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            if source_scope is MemoryScope.TENANT:
                source_id = encode_source_id(
                    scope=source_scope,
                    user_sub=operator.sub,
                    target_name=None,
                    slug=source_slug,
                )
                result = await session.execute(
                    select(Document).where(
                        Document.tenant_id == operator.tenant_id,
                        Document.source == MEMORY_SOURCE,
                        Document.source_id == source_id,
                    )
                )
                return result.scalar_one_or_none()
            # TARGET scope: natural key is ``target:<target_name>:<slug>``;
            # we don't yet have the caller-supplied target_name on this
            # path (T4's body shape is ``{to, move}``). v0.2 returns
            # None for this branch -- T5's CLI verb supplies the
            # target_name explicitly via a parallel arg, at which point
            # this method can take it as a kwarg.
            return None

    @staticmethod
    async def _fetch_doc_in_session(
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        source_id: str,
    ) -> Document | None:
        """Natural-key fetch within a caller-owned :class:`AsyncSession`.

        Distinct from :meth:`_fetch_by_natural_key` because that helper
        opens its own session; :meth:`promote` needs the fetch and the
        subsequent insert/delete to share one transaction.
        """
        result = await session.execute(
            select(Document).where(
                Document.tenant_id == tenant_id,
                Document.source == MEMORY_SOURCE,
                Document.source_id == source_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _build_promotion_metadata(
        *,
        source_doc: Document,
        target_scope: MemoryScope,
        target_name: str | None,
        source_marker: str,
    ) -> dict[str, Any]:
        """Build the target row's ``doc_metadata`` for a promotion insert.

        Three load-bearing fields beyond what :func:`build_metadata`
        produces:

        * ``promoted_from = "<source_scope>/<source_slug>"`` -- the
          idempotency marker. A re-run with the same source produces
          the same marker; an unrelated promotion lands a different
          one and doesn't trip the idempotency branch.
        * ``expires_at = None`` -- promotion intentionally clears the
          source's TTL. Broader-scope memories are long-lived per the
          Initiative body.
        * ``user_sub`` -- ``None`` for tenant- / target-flavoured
          targets (tenant-shared rows have no per-user owner);
          inherited from the source for user-flavoured targets. This
          mirrors :func:`build_metadata`'s scope-aware semantics.
        """
        caller_metadata = dict(source_doc.doc_metadata) if source_doc.doc_metadata else {}
        # Drop the source's bookkeeping fields -- the target's are
        # different and ``build_metadata``-like layer below overwrites
        # them, but explicit deletion makes the intent clear and
        # protects against a future ``build_metadata`` change that
        # respected caller keys for these names.
        for key in ("scope", "user_sub", "target_name", "expires_at"):
            caller_metadata.pop(key, None)

        merged: dict[str, Any] = caller_metadata
        merged["scope"] = target_scope.value
        merged["user_sub"] = (
            metadata_str(source_doc.doc_metadata, "user_sub")
            if target_scope in USER_SCOPED
            else None
        )
        merged["target_name"] = target_name
        # Promotion clears the default TTL -- broader-scope memories
        # are intentional, long-lived per the Initiative body.
        merged["expires_at"] = None
        # The idempotency marker -- the re-run path uses this to
        # decide between "same promotion, return existing" and "409
        # conflict, different provenance".
        merged["promoted_from"] = source_marker
        return merged

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
        substrate's per-signal scores attached. ``created_at`` /
        ``updated_at`` are passed through from :class:`RetrievalHit`,
        which mirrors the persisted :class:`Document` columns, so
        ``search_memory`` returns the same timestamps as a fresh
        write / direct read of the row (G0.9.1-T4 #776 fixed this
        path after consumer feedback observed epoch zero strings).
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
            created_at=hit.created_at,
            updated_at=hit.updated_at,
        )
        return MemoryEntrySearchHit(
            entry=entry,
            fused_score=hit.fused_score,
            bm25_score=hit.bm25_score,
            cosine_score=hit.cosine_score,
            bm25_rank=hit.bm25_rank,
            cosine_rank=hit.cosine_rank,
        )

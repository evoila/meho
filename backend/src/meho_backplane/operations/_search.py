# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hybrid BM25 + cosine RRF search internals for the operation meta-tools.

G0.6-T8 (#399) internals. Split out of
:mod:`meho_backplane.operations.meta_tools` to keep the public surface
focused on handler shape; the SQL + RRF math lives here.

The algorithm is the same Reciprocal Rank Fusion shape G0.4 ships for
the ``documents`` table -- pull the top
:data:`~meho_backplane.retrieval.retriever.CANDIDATE_LIMIT` rows from
each per-signal candidate query, fuse the two ranked lists via
RRF (``1 / (RRF_K + rank)``), and return the top ``limit``. The
divergence is the source table (``endpoint_descriptor``) and the
SQL (the expression-FTS index built on ``coalesce(summary, '') || ' '
|| coalesce(description, '')`` instead of the documents ``body`` column).

Dialect-aware
=============

PostgreSQL routes through the FTS GIN + pgvector IVFFlat operators
that migration ``0005`` installed; SQLite (used by unit tests) falls
back to a deterministic application-layer ranking (substring match
on the summary + description blob, plus Python cosine math against
each row's stored embedding). The fallback path is what the unit tests
exercise; production never hits it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.retrieval.embedding import get_embedding_service
from meho_backplane.retrieval.retriever import CANDIDATE_LIMIT, RRF_K

if TYPE_CHECKING:
    from meho_backplane.operations.meta_tools import OperationSearchHit

__all__ = [
    "hybrid_search",
    "resolve_group_id",
]


async def resolve_group_id(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
    group_key: str,
) -> uuid.UUID | None:
    """Look up an ``operation_group.id`` by natural key + group_key, tenant-scoped.

    Tenant-scoped row wins over built-in row when both exist with the
    same key (same precedence as
    :func:`~meho_backplane.operations._lookup.lookup_descriptor`). Returns
    ``None`` when neither bucket has a match.

    Visibility predicate (claude-rdc-hetzner-dc#1136): a group resolves
    when it is fully enabled (``review_status='enabled'``) **or** still
    ``staged``/``disabled`` at the group level yet holding ≥1 per-op-
    enabled descriptor. This mirrors the ``list_operation_groups``
    visibility branch (:func:`~meho_backplane.operations.meta_tools._build_operation_groups_query`)
    so a group-scoped ``search_operations(group=<key>)`` on a ``partial``
    group narrows to its live ops instead of short-circuiting to empty
    hits — :func:`hybrid_search` already filters per-op ``is_enabled``,
    so the resolved staged-group id then yields exactly its enabled ops.
    """
    has_enabled_op = (
        select(EndpointDescriptor.id)
        .where(
            EndpointDescriptor.group_id == OperationGroup.id,
            EndpointDescriptor.is_enabled.is_(True),
        )
        .exists()
    )
    visible = (OperationGroup.review_status == "enabled") | has_enabled_op
    # Tenant-scoped first.
    result = await session.execute(
        select(OperationGroup.id).where(
            OperationGroup.tenant_id == tenant_id,
            OperationGroup.product == product,
            OperationGroup.version == version,
            OperationGroup.impl_id == impl_id,
            OperationGroup.group_key == group_key,
            visible,
        )
    )
    row = result.scalar_one_or_none()
    if row is not None:
        return row
    result = await session.execute(
        select(OperationGroup.id).where(
            OperationGroup.tenant_id.is_(None),
            OperationGroup.product == product,
            OperationGroup.version == version,
            OperationGroup.impl_id == impl_id,
            OperationGroup.group_key == group_key,
            visible,
        )
    )
    return result.scalar_one_or_none()


async def hybrid_search(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
    group_id: uuid.UUID | None,
    query: str,
    limit: int,
) -> list[OperationSearchHit]:
    """BM25 + cosine RRF over ``endpoint_descriptor`` rows.

    Dialect-aware: PostgreSQL routes through the FTS + pgvector
    operators the migration installed indexes for; SQLite falls back to
    a deterministic application-layer ranking. The PG path is what
    production hits; the SQLite path keeps the unit tests deterministic
    without a Postgres container.
    """
    conn = await session.connection()
    if conn.dialect.name == "postgresql":
        return await _hybrid_search_pg(
            session,
            tenant_id=tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
            group_id=group_id,
            query=query,
            limit=limit,
        )
    return await _hybrid_search_fallback(
        session,
        tenant_id=tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
        group_id=group_id,
        query=query,
        limit=limit,
    )


async def _hybrid_search_pg(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
    group_id: uuid.UUID | None,
    query: str,
    limit: int,
) -> list[OperationSearchHit]:
    """PostgreSQL path -- FTS GIN + pgvector IVFFlat + RRF fusion.

    The BM25 candidate query matches against the same
    ``coalesce(summary, '') || ' ' || coalesce(description, '')``
    expression the ``endpoint_descriptor_bm25_idx`` is built on.
    """
    query_embedding = await get_embedding_service().encode_one(query)
    embedding_literal = "[" + ", ".join(f"{x:.7f}" for x in query_embedding) + "]"

    bm25_sql = text(
        """
        SELECT id, ts_rank_cd(
            to_tsvector('english', coalesce(summary, '') || ' ' || coalesce(description, '')),
            plainto_tsquery('english', :query)
        ) AS score
        FROM endpoint_descriptor
        WHERE (tenant_id IS NULL OR tenant_id = :tenant_id)
          AND product = :product
          AND version = :version
          AND impl_id = :impl_id
          AND is_enabled = TRUE
          AND (CAST(:group_id AS uuid) IS NULL OR group_id = :group_id)
          AND to_tsvector('english', coalesce(summary, '') || ' ' || coalesce(description, ''))
              @@ plainto_tsquery('english', :query)
        ORDER BY score DESC
        LIMIT :limit
        """
    )
    bm25_result = await session.execute(
        bm25_sql,
        {
            "query": query,
            "tenant_id": str(tenant_id),
            "product": product,
            "version": version,
            "impl_id": impl_id,
            "group_id": str(group_id) if group_id is not None else None,
            "limit": CANDIDATE_LIMIT,
        },
    )
    bm25_rows = bm25_result.all()

    cosine_sql = text(
        """
        SELECT id, 1 - (embedding <=> CAST(:emb AS vector)) AS score
        FROM endpoint_descriptor
        WHERE (tenant_id IS NULL OR tenant_id = :tenant_id)
          AND product = :product
          AND version = :version
          AND impl_id = :impl_id
          AND is_enabled = TRUE
          AND embedding IS NOT NULL
          AND (CAST(:group_id AS uuid) IS NULL OR group_id = :group_id)
        ORDER BY embedding <=> CAST(:emb AS vector)
        LIMIT :limit
        """
    )
    cosine_result = await session.execute(
        cosine_sql,
        {
            "emb": embedding_literal,
            "tenant_id": str(tenant_id),
            "product": product,
            "version": version,
            "impl_id": impl_id,
            "group_id": str(group_id) if group_id is not None else None,
            "limit": CANDIDATE_LIMIT,
        },
    )
    cosine_rows = cosine_result.all()

    fused = _rrf_fuse(bm25_rows, cosine_rows, limit=limit)
    if not fused:
        return []
    return await _hydrate_hits(session, fused)


async def _hybrid_search_fallback(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
    group_id: uuid.UUID | None,
    query: str,
    limit: int,
) -> list[OperationSearchHit]:
    """SQLite (test-only) path -- substring + Python cosine ranking.

    Deterministic, doesn't require pgvector or PG FTS, and tracks the
    PG path's behaviour close enough that the unit test asserting
    "search returns the typed op whose summary matches the query"
    holds across both dialects. Production never hits this branch.
    """
    stmt = (
        select(EndpointDescriptor)
        .where(
            (EndpointDescriptor.tenant_id.is_(None)) | (EndpointDescriptor.tenant_id == tenant_id),
            EndpointDescriptor.product == product,
            EndpointDescriptor.version == version,
            EndpointDescriptor.impl_id == impl_id,
            EndpointDescriptor.is_enabled.is_(True),
        )
        .order_by(EndpointDescriptor.op_id)
        .limit(CANDIDATE_LIMIT)
    )
    if group_id is not None:
        stmt = stmt.where(EndpointDescriptor.group_id == group_id)
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return []
    try:
        query_embedding: list[float] | None = await get_embedding_service().encode_one(query)
    except Exception:
        # Embedding service unavailable in this test context. Fall back
        # to substring-only ranking; test fixtures supply summaries that
        # overlap the query so the candidate the test cares about ranks first.
        query_embedding = None
    query_lower = query.lower()
    bm25_rows: list[_FallbackRow] = []
    cosine_rows: list[_FallbackRow] = []
    for row in rows:
        text_blob = ((row.summary or "") + " " + (row.description or "")).lower()
        terms = [t for t in query_lower.split() if t]
        # Substring score: count of query terms present in the text blob.
        # Zero-score rows are dropped from the BM25 candidate list (mirrors
        # the PG `@@` filter behaviour).
        score = sum(1 for t in terms if t in text_blob)
        if score > 0:
            bm25_rows.append(_FallbackRow(row.id, float(score)))
        if query_embedding is not None and row.embedding is not None:
            cosine = _cosine_similarity(query_embedding, row.embedding)
            cosine_rows.append(_FallbackRow(row.id, cosine))
    bm25_rows.sort(key=lambda r: r.score, reverse=True)
    cosine_rows.sort(key=lambda r: r.score, reverse=True)
    fused = _rrf_fuse(bm25_rows, cosine_rows, limit=limit)
    if not fused:
        return []
    return await _hydrate_hits(session, fused)


class _FallbackRow:
    """SQLite-fallback candidate row -- duck-types the asyncpg row shape."""

    __slots__ = ("id", "score")

    def __init__(self, descriptor_id: uuid.UUID, score: float) -> None:
        self.id = descriptor_id
        self.score = score


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity for two equal-length float vectors. Python-only."""
    if len(a) != len(b):
        return 0.0
    dot: float = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a: float = sum(x * x for x in a) ** 0.5
    norm_b: float = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


class _FusedEntry:
    """Per-id fusion state across BM25 + cosine signals."""

    __slots__ = (
        "bm25_score",
        "cosine_score",
        "descriptor_id",
        "fused_score",
    )

    def __init__(self, descriptor_id: uuid.UUID) -> None:
        self.descriptor_id = descriptor_id
        self.bm25_score: float | None = None
        self.cosine_score: float | None = None
        self.fused_score: float = 0.0


def _rrf_fuse(
    bm25_rows: Sequence[Any],
    cosine_rows: Sequence[Any],
    *,
    limit: int,
) -> list[_FusedEntry]:
    """RRF fusion mirroring :func:`~meho_backplane.retrieval.retriever._rrf_fuse`.

    Same math (``1 / (RRF_K + rank)``, 1-based ranks); the only difference
    is the entry type carries a descriptor_id source for the hydration
    step. Replicated here rather than imported because the retriever's
    helper is keyed on document UUIDs and the entry class is private to
    that module.
    """
    if limit <= 0:
        return []
    entries: dict[uuid.UUID, _FusedEntry] = {}
    for rank0, row in enumerate(bm25_rows):
        descriptor_id = _coerce_uuid(row.id)
        rank = rank0 + 1
        entry = entries.setdefault(descriptor_id, _FusedEntry(descriptor_id))
        entry.bm25_score = float(row.score) if row.score is not None else None
        entry.fused_score += 1.0 / (RRF_K + rank)
    for rank0, row in enumerate(cosine_rows):
        descriptor_id = _coerce_uuid(row.id)
        rank = rank0 + 1
        entry = entries.setdefault(descriptor_id, _FusedEntry(descriptor_id))
        entry.cosine_score = float(row.score) if row.score is not None else None
        entry.fused_score += 1.0 / (RRF_K + rank)
    return sorted(entries.values(), key=lambda e: e.fused_score, reverse=True)[:limit]


def _coerce_uuid(value: Any) -> uuid.UUID:
    """Round-trip ``uuid.UUID`` -- accepts str or UUID and returns UUID."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


async def _hydrate_hits(
    session: AsyncSession,
    fused: list[_FusedEntry],
) -> list[OperationSearchHit]:
    """Bulk-fetch full descriptor rows + group_key for the fused top-N entries."""
    from meho_backplane.operations.meta_tools import OperationSearchHit

    descriptor_ids = [e.descriptor_id for e in fused]
    stmt = (
        select(EndpointDescriptor, OperationGroup.group_key)
        .outerjoin(OperationGroup, EndpointDescriptor.group_id == OperationGroup.id)
        .where(EndpointDescriptor.id.in_(descriptor_ids))
    )
    rows = (await session.execute(stmt)).all()
    by_id: dict[uuid.UUID, tuple[EndpointDescriptor, str | None]] = {
        descriptor.id: (descriptor, group_key) for descriptor, group_key in rows
    }
    hits: list[OperationSearchHit] = []
    for entry in fused:
        pair = by_id.get(entry.descriptor_id)
        if pair is None:
            # Defensive: row deleted between fusion and hydration. Skip
            # rather than fail; the caller's hit list shortens by one.
            continue
        descriptor, group_key = pair
        hits.append(
            OperationSearchHit(
                op_id=descriptor.op_id,
                summary=descriptor.summary,
                description=descriptor.description,
                group_key=group_key,
                safety_level=descriptor.safety_level,
                requires_approval=descriptor.requires_approval,
                fused_score=entry.fused_score,
                bm25_score=entry.bm25_score,
                cosine_score=entry.cosine_score,
            )
        )
    return hits

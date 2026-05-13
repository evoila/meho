# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``retrieve`` -- hybrid BM25 + cosine retrieval with Reciprocal Rank Fusion.

G0.4-T4 (#261) of Initiative #225. The shared read path G4
(:func:`meho kb search`), G5 (:func:`meho recall`), and future
agent-grounding flows all consume; correctness here is load-bearing
for every retrieval-quality assessment downstream.

Algorithm (per v0.1-spec L391)
------------------------------

Two retrieval signals, fused by RRF (Reciprocal Rank Fusion,
Microsoft 2009 paper -- ``k=60`` default):

1. **BM25** via Postgres FTS. ``ts_rank_cd`` against the
   ``documents_body_fts_idx`` GIN expression index migration
   ``0003`` (G0.4-T1) installed. Lexical match: high score for
   documents containing the literal query terms.
2. **Cosine similarity** via pgvector. ``embedding <=> query_embedding``
   against the ``documents_embedding_idx`` IVFFlat index. Semantic
   match: high score for documents whose meaning aligns with the
   query even without literal term overlap.

Each signal returns its top 50 candidates; the two ranked lists are
merged via RRF:

    fused_score(d) = sum over signals s where d appears:
        1.0 / (k + rank(d, s))

with ``k = 60``. Documents in only one signal's top-50 still
contribute one term; documents in both contribute two. Sorting by
fused_score and taking the top ``limit`` yields the final hit list.

**Why RRF and not weighted-sum or rerank.** Per the Initiative body:
RRF needs no per-signal score normalisation (``ts_rank_cd`` is
unbounded, cosine is 0--1), no per-query weight tuning, and no
training data -- ranks are scale-invariant. A reranker model
(BAAI/bge-reranker, ColBERT) is the v0.2.next escape hatch if G4
corpus-recall numbers show RRF under-performs.

**Pull 50 candidates per signal, fuse to ``limit``.** 50 is wide
enough to catch semantically-related documents that BM25 misses
(and vice-versa), narrow enough to keep the in-process fusion cheap
on the v0.2 corpus size. Tune upward when corpora grow past ~10k
documents per tenant; the in-process Python loop is O(N) over
candidates, not O(N) over the corpus.

Tenant scoping
--------------

Every query filters by ``tenant_id`` in both SQL statements. There
is no API surface that omits tenant scoping -- the caller passes
``tenant_id`` as a required parameter, the helper threads it into
every query, and the unique composite index on
``(tenant_id, source, source_id)`` guarantees no cross-tenant
collision in the indexed corpus.

Optional filters
----------------

``source`` (e.g. ``"kb"`` vs ``"memory"``) and ``kind``
(e.g. ``"kb-entry"`` vs ``"memory-user"``) narrow within a tenant.
G4's ``meho kb search`` calls ``retrieve(source="kb")``; G5's
``meho recall`` calls ``retrieve(source="memory", kind="memory-user")``
to scope to per-operator memories.

Out of scope (deferred per Initiative body)
-------------------------------------------

* Reranking (BAAI/bge-reranker, ColBERT) -- v0.2 ships RRF only.
* Cross-tenant retrieval / global search -- explicitly disallowed.
* Streaming hits / SSE -- single batch response.
* Per-query embedding cache -- every retrieval re-embeds the query.
  v0.2.next can add an LRU once cache-hit ratios are measured.
* Filtering by metadata JSONB fields -- v0.2 supports source/kind only.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.retrieval.embedding import get_embedding_service

__all__ = ["CANDIDATE_LIMIT", "RRF_K", "RetrievalHit", "retrieve"]


#: RRF "k" constant. ``k = 60`` is the Microsoft paper default; the
#: literature shows the choice is not load-bearing within the
#: 10--100 range (RRF is robust to k variations because rank-based
#: fusion is scale-invariant). Locked here as a module-level constant
#: so callers can read it for assertions (T6 integration test
#: references it explicitly).
RRF_K: int = 60

#: Per-signal candidate pull. Both BM25 and cosine return their top
#: ``CANDIDATE_LIMIT`` documents; the in-process fusion merges them
#: down to the caller's ``limit``. 50 is the v0.2 default per the
#: Initiative body -- wide enough to catch the documents one signal
#: missed, narrow enough to keep the fusion loop O(N) where N <= 100.
CANDIDATE_LIMIT: int = 50


class RetrievalHit(BaseModel):
    """One document in a ranked retrieval response.

    Frozen pydantic v2 model: the retrieve helper builds these once
    and the API surface (T5) returns them unchanged. The per-signal
    scores + ranks are carried for observability -- callers tuning
    embedding model choice want to see whether the top hit had
    contributions from both signals or just one.

    ``fused_score`` is the RRF score the documents are sorted by;
    ``bm25_score`` and ``cosine_score`` are the raw per-signal
    scores (None if the document didn't appear in that signal's
    top-:data:`CANDIDATE_LIMIT`). Likewise ``bm25_rank`` /
    ``cosine_rank`` are the 1-based ranks (1 = best) the document
    held in each signal's list, None when absent.
    """

    model_config = ConfigDict(frozen=True)

    document_id: uuid.UUID
    tenant_id: uuid.UUID
    source: str
    source_id: str
    kind: str
    body: str
    doc_metadata: dict[str, Any]
    fused_score: float
    bm25_score: float | None
    cosine_score: float | None
    bm25_rank: int | None
    cosine_rank: int | None


async def retrieve(
    tenant_id: uuid.UUID,
    query: str,
    source: str | None = None,
    kind: str | None = None,
    limit: int = 10,
    session: AsyncSession | None = None,
) -> list[RetrievalHit]:
    """Hybrid BM25 + cosine retrieval with RRF fusion. Tenant-scoped.

    Parameters
    ----------
    tenant_id
        The querying tenant's UUID. Required -- no cross-tenant
        retrieval surface exists.
    query
        Free-form query string. Both signals consume it: BM25 via
        ``plainto_tsquery('english', query)``, cosine via
        :func:`~meho_backplane.retrieval.embedding.EmbeddingService.encode_one`.
    source
        Optional source namespace filter (``"kb"``, ``"memory"``, …).
        ``None`` retrieves across every source.
    kind
        Optional kind filter (``"kb-entry"``, ``"memory-user"``, …).
        Independently applicable from ``source``.
    limit
        Maximum number of hits to return. Default 10; the API
        surface (T5) caps at 50.
    session
        Optional caller-owned :class:`AsyncSession`. When ``None``
        the helper opens its own and closes on exit (no commit
        needed -- retrieve is read-only).

    Returns
    -------
    list[RetrievalHit]
        Ranked list (best first by ``fused_score``), length ``<=
        limit``. Empty list when no document matches either signal.

    Behavioural contract
    --------------------

    * The query is embedded once per call (no embedding cache in
      v0.2). The cost is one fastembed forward-pass plus two SQL
      round-trips against the indexed table.
    * BM25 filter uses ``@@`` against ``to_tsvector('english',
      body)`` -- documents without ANY query term match are
      excluded from the BM25 candidate set entirely. The cosine
      side has no such filter; it always returns up to
      :data:`CANDIDATE_LIMIT` rows ranked by distance.
    * A document appearing in only one signal's candidate set still
      contributes its single RRF term -- the fused list is the
      union, not the intersection.
    * Empty corpus / no-match query -> empty list, not an error.
    * ``limit < 0`` -> :class:`ValueError`. Python's slice semantics
      would silently truncate the result with a negative bound,
      producing a partial list with no operator-facing signal that
      the request was malformed. Failing fast at the helper boundary
      keeps the API surface (T5) honest -- a Pydantic ``ge=1``
      guard there is the first line, but this defensive check
      prevents in-process callers (T3's index pipeline future work,
      G4 / G5 ingestion paths) from re-deriving the same mistake.
    * ``limit == 0`` -> short-circuit ``[]``. Skips the embedding
      compute + both SQL round-trips; the result is the same as
      slicing the fused list with ``[:0]`` but avoids the wasted
      work.

    SQLite path
    -----------

    The PG-only ``to_tsvector`` / ``plainto_tsquery`` / ``<=>``
    operators do not have SQLite analogues. The helper's behaviour
    against SQLite is undefined; unit tests should mock the
    embedding service and either pre-stage hits in the
    PG-real path (T6's integration test) or assert the RRF fusion
    math against a synthetic per-signal candidate set (this
    module's unit tests below).
    """
    if limit < 0:
        raise ValueError(f"limit must be >= 0; got {limit}")
    if limit == 0:
        return []
    if session is not None:
        return await _retrieve_in_session(session, tenant_id, query, source, kind, limit)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as owned_session:
        return await _retrieve_in_session(owned_session, tenant_id, query, source, kind, limit)


async def _retrieve_in_session(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: str,
    source: str | None,
    kind: str | None,
    limit: int,
) -> list[RetrievalHit]:
    """Inner implementation -- runs the two candidate queries + RRF fusion.

    Split out so :func:`retrieve` can branch on caller-owned-vs-
    helper-owned session without duplicating the algorithm. Read-only
    by construction; no commit / rollback paths.

    Raw SQL is used here (rather than the ORM ``select(Document)``
    + ``func.ts_rank_cd(...)`` style) because the BM25 + cosine
    operators (``@@``, ``<=>``) are pgvector / PG-FTS extensions
    SQLAlchemy doesn't model natively. Parameterised bind variables
    keep the query injection-safe; the embedding cast
    (``CAST(:emb AS vector)``) is the documented pgvector pattern
    for binding a Python ``list[float]`` to a ``vector(384)`` column.
    """
    log = structlog.get_logger()
    query_embedding = await get_embedding_service().encode_one(query)
    # pgvector's wire format for a vector literal is a bracketed list
    # of floats: ``[0.1, 0.2, ...]``. Python's ``str(list)`` produces
    # exactly that shape, which lets the bound ``$1`` work through
    # asyncpg's text codec (asyncpg has no native ``vector`` type
    # registered against the raw ``text()`` statement, so the bind
    # variable must arrive as a string for ``CAST($1 AS vector)`` to
    # parse). Without this serialisation asyncpg raises
    # ``TypeError: expected str, got list`` deep inside the text
    # codec because Python list -> wire format conversion is gated
    # on a typed column adapter we don't have here.
    embedding_literal = "[" + ", ".join(f"{x:.7f}" for x in query_embedding) + "]"

    # BM25 candidates -- top :data:`CANDIDATE_LIMIT` rows whose body
    # contains at least one query term (the ``@@`` filter), ranked by
    # ``ts_rank_cd`` descending. The ``:source IS NULL OR ...``
    # pattern lets us bind a single SQL string for every filter
    # combination; the asyncpg driver short-circuits the OR cleanly.
    bm25_sql = text(
        """
        SELECT id, ts_rank_cd(
            to_tsvector('english', body),
            plainto_tsquery('english', :query)
        ) AS score
        FROM documents
        WHERE tenant_id = :tenant_id
          AND (CAST(:source AS text) IS NULL OR source = :source)
          AND (CAST(:kind AS text) IS NULL OR kind = :kind)
          AND to_tsvector('english', body) @@ plainto_tsquery('english', :query)
        ORDER BY score DESC
        LIMIT :limit
        """
    )
    bm25_result = await session.execute(
        bm25_sql,
        {
            "query": query,
            "tenant_id": str(tenant_id),
            "source": source,
            "kind": kind,
            "limit": CANDIDATE_LIMIT,
        },
    )
    bm25_rows = bm25_result.all()

    # Cosine candidates -- top :data:`CANDIDATE_LIMIT` rows by
    # ``embedding <=> query_embedding`` distance. ``1 - distance``
    # converts pgvector's cosine *distance* (0 = identical) to a
    # similarity *score* (1 = identical) so callers can rank on a
    # higher-is-better signal that aligns with BM25's ``ts_rank_cd``.
    # No content filter on the cosine side -- the embedding is the
    # query, and the IVFFlat index returns ranked candidates whether
    # the body shares query terms or not.
    cosine_sql = text(
        """
        SELECT id, 1 - (embedding <=> CAST(:emb AS vector)) AS score
        FROM documents
        WHERE tenant_id = :tenant_id
          AND (CAST(:source AS text) IS NULL OR source = :source)
          AND (CAST(:kind AS text) IS NULL OR kind = :kind)
        ORDER BY embedding <=> CAST(:emb AS vector)
        LIMIT :limit
        """
    )
    cosine_result = await session.execute(
        cosine_sql,
        {
            "emb": embedding_literal,
            "tenant_id": str(tenant_id),
            "source": source,
            "kind": kind,
            "limit": CANDIDATE_LIMIT,
        },
    )
    cosine_rows = cosine_result.all()

    fused = _rrf_fuse(bm25_rows, cosine_rows, limit=limit)
    if not fused:
        log.info(
            "retrieve_empty",
            tenant_id=str(tenant_id),
            source=source,
            kind=kind,
        )
        return []

    # Fetch full Document rows for the top-`limit` ids in one query;
    # avoids dragging the full body / metadata through the two
    # candidate queries (which only need id + score for the fusion
    # decision).
    top_ids = [entry.document_id for entry in fused]
    doc_result = await session.execute(select(Document).where(Document.id.in_(top_ids)))
    docs_by_id = {doc.id: doc for doc in doc_result.scalars().all()}

    hits: list[RetrievalHit] = []
    for entry in fused:
        doc = docs_by_id.get(entry.document_id)
        if doc is None:
            # Shouldn't happen -- the candidate queries pulled ids
            # from `documents` -- but guard against the SQLAlchemy
            # cache-staleness edge case where a concurrent delete
            # removed the row between the candidate query and the
            # full fetch.
            continue
        hits.append(
            RetrievalHit(
                document_id=doc.id,
                tenant_id=doc.tenant_id,
                source=doc.source,
                source_id=doc.source_id,
                kind=doc.kind,
                body=doc.body,
                doc_metadata=doc.doc_metadata,
                fused_score=entry.fused_score,
                bm25_score=entry.bm25_score,
                cosine_score=entry.cosine_score,
                bm25_rank=entry.bm25_rank,
                cosine_rank=entry.cosine_rank,
            )
        )

    log.info(
        "retrieve_hits",
        tenant_id=str(tenant_id),
        source=source,
        kind=kind,
        hit_count=len(hits),
    )
    return hits


class _FusedEntry:
    """Intermediate fusion result -- the per-id state across both signals."""

    __slots__ = (
        "bm25_rank",
        "bm25_score",
        "cosine_rank",
        "cosine_score",
        "document_id",
        "fused_score",
    )

    def __init__(self, document_id: uuid.UUID) -> None:
        self.document_id: uuid.UUID = document_id
        self.bm25_score: float | None = None
        self.cosine_score: float | None = None
        self.bm25_rank: int | None = None
        self.cosine_rank: int | None = None
        self.fused_score: float = 0.0


def _rrf_fuse(
    bm25_rows: Sequence[Any],
    cosine_rows: Sequence[Any],
    *,
    limit: int,
) -> list[_FusedEntry]:
    """Merge two per-signal ranked lists via Reciprocal Rank Fusion.

    Returns the top ``limit`` :class:`_FusedEntry` records sorted by
    ``fused_score`` descending. Pure function -- no DB access, no
    embedding calls -- so unit tests can exercise the fusion math
    directly against synthetic candidate lists without spinning up
    PG or fastembed.

    The fusion math:

        fused_score(d) = sum over signals s where d appears in top-50:
            1.0 / (RRF_K + rank(d, s))

    A document in both signals' top-50 receives two contributions; a
    document in only one receives one. Ranks are 1-based (best = 1)
    because the RRF paper convention uses 1-indexed ranks, which
    keeps the score positive and bounded above by ``2/(RRF_K + 1)``.

    Parameters
    ----------
    bm25_rows
        Ranked rows from the BM25 candidate query. Each row exposes
        ``.id`` (UUID) and ``.score`` (float). Order matters --
        position 0 is rank 1.
    cosine_rows
        Ranked rows from the cosine candidate query. Same shape.
    limit
        How many top-fused entries to return. Must be ``>= 0``;
        the public :func:`retrieve` wrapper guards before calling
        here, but unit tests exercise this helper directly so the
        same guard is repeated to keep the contract local to the
        function. ``limit == 0`` short-circuits to an empty list
        without sorting.
    """
    if limit < 0:
        raise ValueError(f"limit must be >= 0; got {limit}")
    if limit == 0:
        return []
    entries: dict[uuid.UUID, _FusedEntry] = {}

    for rank0, row in enumerate(bm25_rows):
        doc_id = _coerce_uuid(row.id)
        rank = rank0 + 1
        entry = entries.setdefault(doc_id, _FusedEntry(doc_id))
        entry.bm25_score = float(row.score) if row.score is not None else None
        entry.bm25_rank = rank
        entry.fused_score += 1.0 / (RRF_K + rank)

    for rank0, row in enumerate(cosine_rows):
        doc_id = _coerce_uuid(row.id)
        rank = rank0 + 1
        entry = entries.setdefault(doc_id, _FusedEntry(doc_id))
        entry.cosine_score = float(row.score) if row.score is not None else None
        entry.cosine_rank = rank
        entry.fused_score += 1.0 / (RRF_K + rank)

    return sorted(entries.values(), key=lambda e: e.fused_score, reverse=True)[:limit]


def _coerce_uuid(value: Any) -> uuid.UUID:
    """Accept ``UUID`` or hex-string; return ``UUID``.

    asyncpg's PG driver returns ``uuid.UUID`` directly from a
    ``Uuid()`` column; psycopg2 / SQLAlchemy SQLite paths may surface
    the value as ``str``. The fusion math is type-uniform; the
    coercion keeps the dict-key behaviour consistent regardless of
    driver. The :func:`uuid.UUID` constructor is idempotent on a
    ``UUID`` instance only via the ``hex=`` kwarg; the bare
    constructor expects a string. The two-branch dispatch reads
    cleaner than the ``hex=`` form.
    """
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))

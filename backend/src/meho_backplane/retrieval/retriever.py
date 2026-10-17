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
Memory search calls ``retrieve(source="memory", memory_eligibility=...)``
so Memory eligibility and expiry run inside the candidate SQL (#3741).

``metadata_filters`` (G4.4-T1 / #1177) narrows further by
``documents.doc_metadata`` JSONB containment. A flat ``{key: scalar}``
dict translates to ``doc_metadata @> :filters_jsonb`` on the PG side,
which excludes any row whose top-level metadata does not contain
every supplied key/value pair (PG ``@>`` semantics). The substrate
accepts arbitrary keys -- the taxonomy (``source_kind``,
``sidecar_kind``, ``product``, etc.) is a consumer-side convention.
A GIN index on ``documents.metadata`` (migration ``0032``, default
``jsonb_ops`` opclass) keeps containment lookups index-backed as
corpora grow.

Out of scope (deferred per Initiative body)
-------------------------------------------

* Reranking (BAAI/bge-reranker, ColBERT) -- v0.2 ships RRF only.
* Cross-tenant retrieval / global search -- explicitly disallowed.
* Streaming hits / SSE -- single batch response.
* Per-query embedding cache -- every retrieval re-embeds the query.
  v0.2.next can add an LRU once cache-hit ratios are measured.
* Per-source RRF weighting (Proposal B from #1177). Filters give a
  binary "include / exclude" answer that is deterministic by
  construction; weights add a per-request judgment call that has
  to be specified, tested for determinism, and documented. Deferred
  until a Tier 2 corpus surfaces a measured ranking-quality
  regression that filters alone cannot resolve.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.retrieval.embedding import get_embedding_service

__all__ = [
    "CANDIDATE_LIMIT",
    "RRF_K",
    "MemoryEligibility",
    "RetrievalFacets",
    "RetrievalHit",
    "list_retrieval_facets",
    "retrieve",
]


#: The ``documents.source`` value memory rows carry. Mirrors
#: :data:`meho_backplane.memory._internal.MEMORY_SOURCE`; duplicated here
#: as a literal rather than imported so the shared retrieval substrate
#: does not invert its dependency direction onto the memory consumer
#: package. The per-principal predicate below only ever fires for this
#: source, so a drift between the two literals would surface immediately
#: as a memory-isolation test failure.
_MEMORY_SOURCE: str = "memory"

#: The ``documents.kind`` values whose visibility is gated by the
#: stored ``user_sub`` -- the principal that wrote the row is the only
#: one who may read it back. Mirrors
#: :data:`meho_backplane.memory.schemas.USER_SCOPED` projected through
#: ``kind_for_scope`` (``memory-<scope>``). The tenant-broadcast kinds
#: (``memory-tenant`` / ``memory-target``) are deliberately absent: they
#: carry ``user_sub = null`` and are visible to every principal in the
#: tenant. Frozen so the boundary predicate cannot be mutated at runtime.
_USER_SCOPED_MEMORY_KINDS: frozenset[str] = frozenset(
    {"memory-user", "memory-user-tenant", "memory-user-target"}
)


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

    ``created_at`` / ``updated_at`` mirror the persisted
    :class:`~meho_backplane.db.models.Document` columns so downstream
    consumers (memory ``search_memory``, kb search projections) can
    surface real write-time / mtime values instead of substituting a
    placeholder. The retriever already issues a full ``SELECT * FROM
    documents WHERE id IN (...)`` for each top-fused row, so carrying
    the columns through is a free pass-through, not an extra query.
    """

    model_config = ConfigDict(frozen=True)

    document_id: uuid.UUID
    tenant_id: uuid.UUID
    source: str
    source_id: str
    kind: str
    body: str
    doc_metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    fused_score: float
    bm25_score: float | None
    cosine_score: float | None
    bm25_rank: int | None
    cosine_rank: int | None


class MemoryEligibility(BaseModel):
    """Which Memory rows one caller may rank, applied inside the candidate SQL.

    The Memory service builds this (see
    :meth:`meho_backplane.memory.rbac.MemoryRbacResolver.eligibility`); the
    substrate only reads it. It is frozen, so nobody can widen it after it
    is built. When :func:`retrieve` gets one, a ``source='memory'`` row is a
    candidate only if all of these hold, before any ``LIMIT``:

    * the row is in ``tenant_id`` (the substrate's tenant predicate);
    * its ``kind`` is in ``kinds`` (the visible kinds, narrowed to one kind
      when the caller picked a scope);
    * a user-scoped kind carries ``user_sub == principal_sub``; tenant and
      target kinds are shared with every principal in the tenant;
    * its expiry is unset or a valid timestamp later than the database
      server's ``now()`` (see :data:`_MEMORY_EXPIRY_PREDICATE_SQL`).

    The expiry rule runs for every Memory candidate, with or without an
    eligibility value, so no retrieve caller can rank an expired memory.
    Rows from other sources are not affected.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    principal_sub: str = Field(min_length=1)
    kinds: tuple[str, ...] = Field(min_length=1)


# `retrieve` was already over the 100-line block limit on main: its length is
# the parameter/contract docstring, not branching (executable body ~15 lines,
# McCabe trivial). #1797 adds one parameter + a concise doc note, not
# complexity. code-quality-allow: function-size — docstring-dominated public API.
async def retrieve(
    tenant_id: uuid.UUID,
    query: str,
    source: str | None = None,
    kind: str | None = None,
    limit: int = 10,
    session: AsyncSession | None = None,
    metadata_filters: dict[str, Any] | None = None,
    principal_sub: str | None = None,
    memory_eligibility: MemoryEligibility | None = None,
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
    metadata_filters
        Optional flat ``{key: scalar}`` dict. When set, rows are
        narrowed by ``documents.doc_metadata @> :filters_jsonb``
        containment in both candidate SQL statements. Missing keys
        exclude the row (PG ``@>`` semantics); multi-key dicts behave
        as an intersection. ``None`` (default) preserves the
        pre-G4.4-T1 byte-for-byte behaviour. Empty dict ``{}`` is
        treated as ``None`` -- ``@> '{}'`` matches every row, but
        emitting the predicate adds DB-side parse cost for zero
        filtering benefit. Values are expected to be JSON scalars
        (``str`` / ``int`` / ``float`` / ``bool`` / ``None``); the
        substrate does not validate beyond JSON-encodability -- the
        API surface (#1177's :class:`RetrieveRequest`) is the gate
        that enforces scalar-only at request time.
    principal_sub
        The authenticated caller's OIDC ``sub``. When set, the substrate
        enforces a **mandatory, non-overridable** per-principal predicate
        (#1797, :data:`_PRINCIPAL_PREDICATE_SQL`): a ``source='memory'``
        user-scoped row (:data:`_USER_SCOPED_MEMORY_KINDS`) is returned
        only when its stored ``user_sub`` equals *principal_sub*. This
        mirrors :meth:`MemoryRbacResolver.can_read` but is enforced
        centrally so every caller (HTTP route, MCP resource, future
        consumers) is protected without trusting client ``metadata_filters``
        -- the predicate is ANDed in unconditionally, so a client value
        can only narrow, never widen, the visible set. Tenant-broadcast
        kinds and non-memory sources are unaffected. ``None`` (default)
        disables it for callers that scope by ``user_sub`` themselves or
        query non-principal sources.
    memory_eligibility
        The Memory service's typed eligibility for this caller (#3741).
        When set, ``source='memory'`` candidates are limited to its
        ``kinds`` and its ``principal_sub`` drives the per-principal
        predicate, both inside the candidate SQL, so an ineligible row can
        never take a candidate slot. Its ``tenant_id`` must equal
        *tenant_id*, and *principal_sub* must be ``None`` or the same
        value; a mismatch raises :class:`ValueError`. Memory expiry is
        always enforced in SQL, even when this is ``None``.

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

    There is no supported fallback: the Memory eligibility and expiry
    predicates exist only in the PostgreSQL candidate SQL. The PG-only
    ``to_tsvector`` / ``plainto_tsquery`` / ``<=>`` operators do not
    have SQLite analogues. The helper's behaviour against SQLite is
    undefined; unit tests should mock the embedding service and either pre-stage hits in the
    PG-real path (T6's integration test) or assert the RRF fusion
    math against a synthetic per-signal candidate set (this
    module's unit tests below).
    """
    if limit < 0:
        raise ValueError(f"limit must be >= 0; got {limit}")
    if limit == 0:
        return []
    # Empty-dict early-normalise: ``@> '{}'::jsonb`` matches every row,
    # so emitting the predicate is pure DB-side parse cost with zero
    # filtering benefit. Treat ``{}`` as ``None`` at the boundary.
    if metadata_filters is not None and not metadata_filters:
        metadata_filters = None
    memory_kinds = _memory_kinds_for(tenant_id, principal_sub, memory_eligibility)
    if memory_eligibility is not None:
        principal_sub = memory_eligibility.principal_sub
    if session is not None:
        return await _retrieve_in_session(
            session,
            tenant_id,
            query,
            source,
            kind,
            limit,
            metadata_filters,
            principal_sub,
            memory_kinds,
        )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as owned_session:
        return await _retrieve_in_session(
            owned_session,
            tenant_id,
            query,
            source,
            kind,
            limit,
            metadata_filters,
            principal_sub,
            memory_kinds,
        )


def _memory_kinds_for(
    tenant_id: uuid.UUID,
    principal_sub: str | None,
    memory_eligibility: MemoryEligibility | None,
) -> list[str] | None:
    """Check *memory_eligibility* against the call and return its kinds bind.

    ``None`` means "no Memory kind limit" (the caller passed no
    eligibility). A tenant or principal that does not match the call is a
    programming error, so it fails loudly instead of quietly picking one.
    """
    if memory_eligibility is None:
        return None
    if memory_eligibility.tenant_id != tenant_id:
        raise ValueError("memory_eligibility.tenant_id does not match tenant_id")
    if principal_sub is not None and principal_sub != memory_eligibility.principal_sub:
        raise ValueError("principal_sub does not match memory_eligibility.principal_sub")
    return list(memory_eligibility.kinds)


async def _retrieve_in_session(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: str,
    source: str | None,
    kind: str | None,
    limit: int,
    metadata_filters: dict[str, Any] | None,
    principal_sub: str | None,
    memory_kinds: list[str] | None,
) -> list[RetrievalHit]:
    """Inner implementation -- runs the two candidate queries + RRF fusion.

    Split out so :func:`retrieve` can branch on caller-owned-vs-
    helper-owned session without duplicating the algorithm. Read-only
    by construction; no commit / rollback paths.

    Raw SQL is used inside the candidate helpers (rather than the ORM
    ``select(Document)`` + ``func.ts_rank_cd(...)`` style) because the
    BM25 + cosine operators (``@@``, ``<=>``) are pgvector / PG-FTS
    extensions SQLAlchemy doesn't model natively.
    """
    log = structlog.get_logger()
    query_embedding = await get_embedding_service().encode_one(query)
    embedding_literal = _vector_literal(query_embedding)

    # Pre-serialise the metadata-filter dict to a JSON string here so
    # both candidate SQL statements bind the identical text payload.
    # ``json.dumps`` with sort_keys is not strictly required for
    # correctness (PG's ``@>`` is key-order-independent) but keeps the
    # bind value reproducible across calls, which is what the audit-
    # payload hash on the API surface relies on for stable digests.
    metadata_filters_json = (
        json.dumps(metadata_filters, sort_keys=True) if metadata_filters is not None else None
    )

    bm25_rows = await _bm25_candidates(
        session, tenant_id, query, source, kind, metadata_filters_json, principal_sub, memory_kinds
    )
    cosine_rows = await _cosine_candidates(
        session,
        tenant_id,
        embedding_literal,
        source,
        kind,
        metadata_filters_json,
        principal_sub,
        memory_kinds,
    )
    if memory_kinds is not None and source in (None, _MEMORY_SOURCE):
        await _log_unreadable_memory_expiry(session, tenant_id, principal_sub, memory_kinds)

    # Log keys (not values) so structlog never carries tenant-shaped
    # metadata into the application log. The audit payload uses the
    # same key-only discipline (see api/v1/retrieve.py). ``principal_scoped``
    # records *whether* the per-principal predicate was enforced (a bool,
    # never the ``sub`` value) so a security analyst can confirm at a
    # glance that a memory retrieval ran under the #1797 isolation gate.
    metadata_filter_keys = sorted(metadata_filters.keys()) if metadata_filters else None
    principal_scoped = principal_sub is not None

    fused = _rrf_fuse(bm25_rows, cosine_rows, limit=limit)
    if not fused:
        log.info(
            "retrieve_empty",
            tenant_id=str(tenant_id),
            source=source,
            kind=kind,
            metadata_filter_keys=metadata_filter_keys,
            principal_scoped=principal_scoped,
        )
        return []

    hits = await _hydrate_hits(session, fused)
    log.info(
        "retrieve_hits",
        tenant_id=str(tenant_id),
        source=source,
        kind=kind,
        metadata_filter_keys=metadata_filter_keys,
        principal_scoped=principal_scoped,
        hit_count=len(hits),
    )
    return hits


def _vector_literal(query_embedding: Sequence[float]) -> str:
    """Serialize a Python ``list[float]`` to the pgvector wire literal.

    pgvector's wire format for a vector literal is a bracketed list of
    floats: ``[0.1, 0.2, ...]``. Python's ``str(list)`` produces almost
    that shape but uses ``repr`` for each float; manually formatting
    with a fixed precision keeps the bind variable stable across
    Python releases and round-tripping through asyncpg's text codec
    (asyncpg has no native ``vector`` type registered against the raw
    ``text()`` statement, so the bind variable must arrive as a string
    for ``CAST($1 AS vector)`` to parse). Without this serialisation
    asyncpg raises ``TypeError: expected str, got list`` deep inside
    the text codec because Python list → wire format conversion is
    gated on a typed column adapter we don't have here.
    """
    return "[" + ", ".join(f"{x:.7f}" for x in query_embedding) + "]"


#: Mandatory per-principal visibility predicate for ``source='memory'``
#: user-scoped kinds (#1797). Shared verbatim between the BM25 and cosine
#: candidate queries so neither signal can drift into leaking the other's
#: rows. The predicate is inert when ``:principal_sub`` is NULL (the
#: in-process callers that opt out) and for every non-memory source /
#: tenant-broadcast kind; when active it admits a user-scoped memory row
#: only if its stored ``metadata->>'user_sub'`` equals the caller's
#: ``sub``. The three user-scoped kinds are spelled as SQL literals (not
#: a bound IN-list) because the set is fixed by the memory scope model
#: and PG plans a constant ``IN`` list more predictably than an
#: expanding bind. A corrupt user-scoped row carrying ``user_sub = null``
#: yields ``NULL = :principal_sub`` -> NULL -> excluded, matching
#: ``MemoryRbacResolver.can_read``'s deny-on-missing-``user_sub`` posture.
#:
#: This constant is the canonical text of the predicate and the assertion
#: target for :mod:`tests.test_retrieve_isolation`. It is **not**
#: interpolated into the candidate queries: the two ``text(...)``
#: statements below spell the predicate out verbatim instead. Building a
#: statement with ``text(f"...{_PRINCIPAL_PREDICATE_SQL}...")`` trips
#: Semgrep's ``avoid-sqlalchemy-text`` rule (any non-literal ``text()``
#: argument reads as a SQL-injection sink, even a trusted module
#: constant), and CI's registry-pack Semgrep does not honour an inline
#: ``# nosemgrep`` for that rule -- so the repo's convention
#: (``topology/query.py``, ``events/outbox.py``) is to keep the
#: ``text()`` argument a plain string literal. ``_assert_predicate_inlined``
#: guards against the resulting duplication drifting.
_PRINCIPAL_PREDICATE_SQL: str = """
          AND (
            CAST(:principal_sub AS text) IS NULL
            OR source <> 'memory'
            OR kind NOT IN ('memory-user', 'memory-user-tenant', 'memory-user-target')
            OR metadata ->> 'user_sub' = :principal_sub
          )"""

#: Memory eligibility predicate (#3741), inlined verbatim into both
#: candidate queries so it runs **before** each signal's ``LIMIT``. An
#: expired or ineligible Memory row can therefore never take a candidate
#: slot from an eligible one. Rows from other sources pass untouched.
#:
#: * ``:memory_kinds`` -- the :class:`MemoryEligibility` kinds, or NULL
#:   when the caller passed no eligibility (no kind limit).
#: * Expiry uses the database server's ``now()`` (transaction start), never
#:   a client clock, and is enforced for every Memory row:
#:
#:   - missing key or JSON ``null`` -> not expired (legacy rows and
#:     persistent memories);
#:   - the value must look like an ISO 8601 timestamp (the shape regex) and
#:     be valid ``timestamptz`` input (``pg_input_is_valid``, PostgreSQL 16+,
#:     never raises). This rejects ``now``, ``today``, ``epoch``,
#:     ``infinity`` and garbage, which are then treated as **expired**;
#:   - a value with an offset (``Z`` / ``+00:00`` / ``+05:30``) is compared
#:     as the instant it names;
#:   - a value without an offset is read as UTC, the same rule
#:     :func:`meho_backplane.memory._internal.metadata_datetime` uses. It
#:     is cast to ``timestamp`` (not ``timestamptz``) so the session
#:     ``TimeZone`` cannot change the answer.
#:
#: ``CASE`` evaluates its arms in order, so the casts only ever see input
#: the guards accepted -- a malformed value cannot abort the query.
#: Same literal-``text()`` discipline as :data:`_PRINCIPAL_PREDICATE_SQL`;
#: :func:`_assert_predicate_inlined` pins the copies together.
_MEMORY_ELIGIBILITY_PREDICATE_SQL: str = r"""
      AND (
        source <> 'memory'
        OR (
          (CAST(:memory_kinds AS text[]) IS NULL
           OR kind = ANY(CAST(:memory_kinds AS text[])))
          AND CASE
            WHEN metadata ->> 'expires_at' IS NULL THEN true
            WHEN metadata ->> 'expires_at'
                 !~ '^\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d([.]\d{1,6})?)?(Z|[+-]\d\d(:?\d\d)?)?$'
              THEN false
            WHEN NOT pg_input_is_valid(metadata ->> 'expires_at', 'timestamptz') THEN false
            WHEN metadata ->> 'expires_at' ~ '(Z|[+-]\d\d(:?\d\d)?)$'
              THEN CAST(metadata ->> 'expires_at' AS timestamptz) > now()
            ELSE CAST(metadata ->> 'expires_at' AS timestamp) > timezone('UTC', now())
          END
        )
      )"""

#: The ISO 8601 shape check, shared by the eligibility predicate and the
#: unreadable-expiry diagnostic query. Pinned by :func:`_assert_predicate_inlined`.
_MEMORY_EXPIRY_SHAPE_SQL: str = (
    r"'^\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d([.]\d{1,6})?)?(Z|[+-]\d\d(:?\d\d)?)?$'"
)

# BM25 candidate query. Fully-literal ``text("...")`` (no f-string) so the
# ``avoid-sqlalchemy-text`` SAST rule stays clean; the per-principal
# predicate is inlined verbatim from :data:`_PRINCIPAL_PREDICATE_SQL` and
# pinned to it at module load by :func:`_assert_predicate_inlined`.
_BM25_CANDIDATES_SQL = text(
    r"""
    SELECT id, ts_rank_cd(
        to_tsvector('english', body),
        plainto_tsquery('english', :query)
    ) AS score
    FROM documents
    WHERE tenant_id = :tenant_id
      AND (CAST(:source AS text) IS NULL OR source = :source)
      AND (CAST(:kind AS text) IS NULL OR kind = :kind)
      AND (CAST(:metadata_filters AS text) IS NULL
           OR metadata @> CAST(:metadata_filters AS jsonb))
      AND (
        CAST(:principal_sub AS text) IS NULL
        OR source <> 'memory'
        OR kind NOT IN ('memory-user', 'memory-user-tenant', 'memory-user-target')
        OR metadata ->> 'user_sub' = :principal_sub
      )
      AND (
        source <> 'memory'
        OR (
          (CAST(:memory_kinds AS text[]) IS NULL
           OR kind = ANY(CAST(:memory_kinds AS text[])))
          AND CASE
            WHEN metadata ->> 'expires_at' IS NULL THEN true
            WHEN metadata ->> 'expires_at'
                 !~ '^\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d([.]\d{1,6})?)?(Z|[+-]\d\d(:?\d\d)?)?$'
              THEN false
            WHEN NOT pg_input_is_valid(metadata ->> 'expires_at', 'timestamptz') THEN false
            WHEN metadata ->> 'expires_at' ~ '(Z|[+-]\d\d(:?\d\d)?)$'
              THEN CAST(metadata ->> 'expires_at' AS timestamptz) > now()
            ELSE CAST(metadata ->> 'expires_at' AS timestamp) > timezone('UTC', now())
          END
        )
      )
      AND to_tsvector('english', body) @@ plainto_tsquery('english', :query)
    ORDER BY score DESC
    LIMIT :limit
    """
)

# Cosine candidate query -- same literal-``text`` convention and the same
# inlined per-principal predicate as the BM25 statement above.
_COSINE_CANDIDATES_SQL = text(
    r"""
    SELECT id, 1 - (embedding <=> CAST(:emb AS vector)) AS score
    FROM documents
    WHERE tenant_id = :tenant_id
      AND (CAST(:source AS text) IS NULL OR source = :source)
      AND (CAST(:kind AS text) IS NULL OR kind = :kind)
      AND (CAST(:metadata_filters AS text) IS NULL
           OR metadata @> CAST(:metadata_filters AS jsonb))
      AND (
        CAST(:principal_sub AS text) IS NULL
        OR source <> 'memory'
        OR kind NOT IN ('memory-user', 'memory-user-tenant', 'memory-user-target')
        OR metadata ->> 'user_sub' = :principal_sub
      )
      AND (
        source <> 'memory'
        OR (
          (CAST(:memory_kinds AS text[]) IS NULL
           OR kind = ANY(CAST(:memory_kinds AS text[])))
          AND CASE
            WHEN metadata ->> 'expires_at' IS NULL THEN true
            WHEN metadata ->> 'expires_at'
                 !~ '^\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d([.]\d{1,6})?)?(Z|[+-]\d\d(:?\d\d)?)?$'
              THEN false
            WHEN NOT pg_input_is_valid(metadata ->> 'expires_at', 'timestamptz') THEN false
            WHEN metadata ->> 'expires_at' ~ '(Z|[+-]\d\d(:?\d\d)?)$'
              THEN CAST(metadata ->> 'expires_at' AS timestamptz) > now()
            ELSE CAST(metadata ->> 'expires_at' AS timestamp) > timezone('UTC', now())
          END
        )
      )
    ORDER BY embedding <=> CAST(:emb AS vector)
    LIMIT :limit
    """
)


#: Upper bound for the unreadable-expiry diagnostic count. The log line
#: says "at least this many" past the cap, so the query stays cheap.
_UNREADABLE_EXPIRY_LOG_CAP: int = 100

# Diagnostic count of Memory rows the caller could otherwise see but whose
# ``expires_at`` the eligibility predicate rejects as unreadable. Same
# tenant / kind / principal limits as the candidate queries, so the number
# never covers rows the caller is not allowed to know about. Bounded by
# ``LIMIT :cap``. Literal ``text()`` like the statements above.
_UNREADABLE_EXPIRY_COUNT_SQL = text(
    r"""
    SELECT count(*) AS unreadable
    FROM (
      SELECT 1
      FROM documents
      WHERE tenant_id = :tenant_id
        AND source = 'memory'
        AND kind = ANY(CAST(:memory_kinds AS text[]))
        AND (
          kind NOT IN ('memory-user', 'memory-user-tenant', 'memory-user-target')
          OR metadata ->> 'user_sub' = :principal_sub
        )
        AND metadata ->> 'expires_at' IS NOT NULL
        AND NOT (
          metadata ->> 'expires_at'
            ~ '^\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d([.]\d{1,6})?)?(Z|[+-]\d\d(:?\d\d)?)?$'
          AND pg_input_is_valid(metadata ->> 'expires_at', 'timestamptz')
        )
      LIMIT :cap
    ) AS unreadable_rows
    """
)


def _assert_predicate_inlined() -> None:
    """Fail at import if a candidate query drifts from the predicate constants.

    The per-principal predicate lives once in
    :data:`_PRINCIPAL_PREDICATE_SQL` (the value the isolation test asserts
    on) but is spelled out a second time inside each ``text()`` statement
    to keep the ``avoid-sqlalchemy-text`` rule clean. This guard makes the
    duplication safe: it normalises whitespace and checks the predicate is
    embedded verbatim in both statements, so an edit to one copy that is
    not mirrored to the other is a loud ``ImportError`` rather than a
    silent isolation regression.
    """
    principal = " ".join(_PRINCIPAL_PREDICATE_SQL.split())
    eligibility = " ".join(_MEMORY_ELIGIBILITY_PREDICATE_SQL.split())
    for name, statement in (
        ("_BM25_CANDIDATES_SQL", _BM25_CANDIDATES_SQL),
        ("_COSINE_CANDIDATES_SQL", _COSINE_CANDIDATES_SQL),
    ):
        haystack = " ".join(str(statement.text).split())
        if principal not in haystack or eligibility not in haystack:
            raise AssertionError(
                f"{name} no longer contains the per-principal and Memory "
                "eligibility predicates verbatim; they must stay "
                "byte-equivalent to _PRINCIPAL_PREDICATE_SQL and "
                "_MEMORY_ELIGIBILITY_PREDICATE_SQL (see retriever.py)."
            )
    for name, sql in (
        ("_MEMORY_ELIGIBILITY_PREDICATE_SQL", _MEMORY_ELIGIBILITY_PREDICATE_SQL),
        ("_UNREADABLE_EXPIRY_COUNT_SQL", str(_UNREADABLE_EXPIRY_COUNT_SQL.text)),
    ):
        if _MEMORY_EXPIRY_SHAPE_SQL not in sql:
            raise AssertionError(f"{name} no longer contains _MEMORY_EXPIRY_SHAPE_SQL verbatim.")


_assert_predicate_inlined()


async def _bm25_candidates(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: str,
    source: str | None,
    kind: str | None,
    metadata_filters_json: str | None,
    principal_sub: str | None,
    memory_kinds: list[str] | None = None,
) -> Sequence[Any]:
    """Top :data:`CANDIDATE_LIMIT` BM25 candidates by ``ts_rank_cd``.

    Returns rows whose body contains at least one query term (the
    ``@@`` filter), ranked descending. The ``CAST(:source AS text) IS
    NULL OR ...`` pattern lets us bind a single SQL string for every
    filter combination; the asyncpg driver short-circuits the OR
    cleanly. The metadata-filter predicate uses the same null-or-match
    shape (``CAST(:metadata_filters AS text) IS NULL OR metadata @>
    CAST(:metadata_filters AS jsonb)``); the GIN index on
    ``documents.metadata`` (migration ``0032``) backs the containment
    operator so the predicate stays index-backed at corpus scale. The
    mandatory per-principal predicate (:data:`_PRINCIPAL_PREDICATE_SQL`,
    inlined verbatim into :data:`_BM25_CANDIDATES_SQL`) is the last
    ``WHERE`` arm so ``source='memory'`` user-scoped rows the caller does
    not own are excluded regardless of ``metadata_filters``. The Memory
    eligibility predicate (:data:`_MEMORY_ELIGIBILITY_PREDICATE_SQL`) follows
    it, so expired or out-of-kind Memory rows are gone before ``LIMIT``.
    """
    result = await session.execute(
        _BM25_CANDIDATES_SQL,
        {
            "query": query,
            "tenant_id": str(tenant_id),
            "source": source,
            "kind": kind,
            "metadata_filters": metadata_filters_json,
            "principal_sub": principal_sub,
            "memory_kinds": memory_kinds,
            "limit": CANDIDATE_LIMIT,
        },
    )
    return result.all()


async def _cosine_candidates(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    embedding_literal: str,
    source: str | None,
    kind: str | None,
    metadata_filters_json: str | None,
    principal_sub: str | None,
    memory_kinds: list[str] | None = None,
) -> Sequence[Any]:
    """Top :data:`CANDIDATE_LIMIT` cosine candidates by pgvector distance.

    ``1 - (embedding <=> query)`` converts pgvector's cosine *distance*
    (0 = identical) to a similarity *score* (1 = identical) so callers
    can rank on a higher-is-better signal that aligns with BM25's
    ``ts_rank_cd``. No content filter on the cosine side -- the
    embedding is the query, and the IVFFlat index returns ranked
    candidates whether the body shares query terms or not. The
    metadata-filter predicate mirrors :func:`_bm25_candidates` so a
    multi-key filter narrows both signals symmetrically; the mandatory
    per-principal predicate (:data:`_PRINCIPAL_PREDICATE_SQL`) is inlined
    verbatim into :data:`_COSINE_CANDIDATES_SQL` too, so neither signal
    can surface a user-scoped memory row the caller does not own. The
    Memory eligibility predicate is inlined the same way.
    """
    result = await session.execute(
        _COSINE_CANDIDATES_SQL,
        {
            "emb": embedding_literal,
            "tenant_id": str(tenant_id),
            "source": source,
            "kind": kind,
            "metadata_filters": metadata_filters_json,
            "principal_sub": principal_sub,
            "memory_kinds": memory_kinds,
            "limit": CANDIDATE_LIMIT,
        },
    )
    return result.all()


async def _log_unreadable_memory_expiry(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    principal_sub: str | None,
    memory_kinds: list[str],
) -> None:
    """Log how many eligible-looking Memory rows had an unreadable expiry.

    Those rows are already excluded by the candidate SQL. This only tells
    an operator that some stored ``expires_at`` values need repair. The log
    carries a capped count and the tenant id -- never a raw value, row id,
    or principal.
    """
    result = await session.execute(
        _UNREADABLE_EXPIRY_COUNT_SQL,
        {
            "tenant_id": str(tenant_id),
            "memory_kinds": memory_kinds,
            "principal_sub": principal_sub,
            "cap": _UNREADABLE_EXPIRY_LOG_CAP,
        },
    )
    unreadable = int(result.scalar_one())
    if unreadable:
        structlog.get_logger().warning(
            "memory_expiry_unreadable_excluded",
            tenant_id=str(tenant_id),
            excluded_count=unreadable,
            count_capped=unreadable >= _UNREADABLE_EXPIRY_LOG_CAP,
        )


async def _hydrate_hits(
    session: AsyncSession,
    fused: Sequence[_FusedEntry],
) -> list[RetrievalHit]:
    """Project fused entries to :class:`RetrievalHit` via a single ``IN`` fetch.

    Fetches full :class:`Document` rows for the top-fused ids in one
    query; avoids dragging the full body / metadata through the two
    candidate queries (which only need id + score for the fusion
    decision). A concurrent delete between candidate scan and hydrate
    can leave a fused id without a row -- we skip those defensively
    rather than raising, because retrieval is a read-only contract and
    the caller would rather see N-1 hits than an error.
    """
    top_ids = [entry.document_id for entry in fused]
    doc_result = await session.execute(select(Document).where(Document.id.in_(top_ids)))
    docs_by_id = {doc.id: doc for doc in doc_result.scalars().all()}

    hits: list[RetrievalHit] = []
    for entry in fused:
        doc = docs_by_id.get(entry.document_id)
        if doc is None:
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
                created_at=doc.created_at,
                updated_at=doc.updated_at,
                fused_score=entry.fused_score,
                bm25_score=entry.bm25_score,
                cosine_score=entry.cosine_score,
                bm25_rank=entry.bm25_rank,
                cosine_rank=entry.cosine_rank,
            )
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


class RetrievalFacets(BaseModel):
    """The distinct ``source`` / ``kind`` values retrievable in a tenant.

    Populates the ``/ui/retrieval`` diagnostics Source / Kind datalists (#2458)
    so an operator selects a filter value the substrate actually holds instead
    of guessing one that fails silently. The values are enumerated from the
    same ``documents`` rows :func:`retrieve` reads, gated by the same
    per-principal visibility predicate -- so a docs-corpus collection name (a
    different substrate that ``retrieve`` never queries) can never appear, and a
    user-scoped ``source='memory'`` ``kind`` another principal owns is excluded.

    Both lists are sorted and de-duplicated. Frozen pydantic v2 model -- the
    caller renders the values verbatim into ``<datalist>`` options.
    """

    model_config = ConfigDict(frozen=True)

    sources: tuple[str, ...]
    kinds: tuple[str, ...]


async def list_retrieval_facets(
    tenant_id: uuid.UUID,
    *,
    principal_sub: str | None = None,
    session: AsyncSession | None = None,
) -> RetrievalFacets:
    """Enumerate the distinct retrieval-visible ``source`` / ``kind`` values.

    Runs one ``SELECT DISTINCT source, kind FROM documents`` scoped to
    *tenant_id* -- no cross-tenant row can contribute, mirroring the mandatory
    tenant predicate every candidate query carries. When *principal_sub* is set
    the same per-principal predicate the candidate queries enforce
    (:data:`_PRINCIPAL_PREDICATE_SQL`) is applied, so a user-scoped
    ``source='memory'`` row contributes its ``kind`` only to the principal that
    wrote it; passing ``None`` (the opt-out the in-process callers use) admits
    every row in the tenant.

    Parameters
    ----------
    tenant_id
        The querying tenant's UUID. Required -- there is no cross-tenant
        enumeration surface, exactly as with :func:`retrieve`.
    principal_sub
        The authenticated caller's OIDC ``sub``. When set, user-scoped memory
        kinds owned by a different principal are excluded from the result.
    session
        Optional caller-owned :class:`AsyncSession`. When ``None`` the helper
        opens its own and closes on exit (the query is read-only, no commit).

    Returns
    -------
    RetrievalFacets
        Sorted, de-duplicated ``sources`` and ``kinds`` tuples. Empty tuples
        when the tenant has no visible documents.
    """
    if session is None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as owned_session:
            return await _select_facets(owned_session, tenant_id, principal_sub)
    return await _select_facets(session, tenant_id, principal_sub)


async def _select_facets(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    principal_sub: str | None,
) -> RetrievalFacets:
    """Execute the tenant-scoped, per-principal-gated DISTINCT facet query.

    The per-principal ``OR`` chain is the ``:principal_sub IS NOT NULL`` arm of
    :data:`_PRINCIPAL_PREDICATE_SQL` expressed through the ORM so the query
    stays portable across the PG production engine and the SQLite test engine
    (the raw ``metadata ->> 'user_sub'`` text extraction the candidate queries
    inline is PG-only). ``doc_metadata['user_sub'].as_string()`` yields
    ``JSON_EXTRACT`` on SQLite and ``->>`` on PostgreSQL; a missing ``user_sub``
    resolves to NULL, so ``NULL == :principal_sub`` excludes the row -- matching
    the candidate queries' deny-on-missing-``user_sub`` posture.
    """
    stmt = select(Document.source, Document.kind).where(Document.tenant_id == tenant_id).distinct()
    if principal_sub is not None:
        stmt = stmt.where(
            or_(
                Document.source != _MEMORY_SOURCE,
                Document.kind.notin_(_USER_SCOPED_MEMORY_KINDS),
                Document.doc_metadata["user_sub"].as_string() == principal_sub,
            )
        )
    rows = (await session.execute(stmt)).all()
    sources = sorted({source for source, _ in rows})
    kinds = sorted({kind for _, kind in rows})
    return RetrievalFacets(sources=tuple(sources), kinds=tuple(kinds))

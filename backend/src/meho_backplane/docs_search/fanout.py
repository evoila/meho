# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cross-collection fan-out for ``search_docs`` with RRF merge (G4.6-T5 #1554).

The opt-in cross-collection path of ``search_docs`` (and ``search_docs``
only — ``ask_docs`` stays single-collection by design, #1548 decision 2).
A request that names **more than one** collection — an explicit
``collections=[a, b]`` list or the ``all`` sentinel (every entitled,
ready collection) — fans the query out across each collection's own
backend independently, then merges the per-collection ranked lists with
**Reciprocal Rank Fusion** (RRF). Three pieces:

1. :func:`parse_collection_scope` — turn the request's ``collection`` /
   ``collections`` arguments into a typed :class:`CollectionScope`: either
   the single-collection scope (the T3 path, handled by the caller) or a
   fan-out scope (an explicit key set, or the ``all`` sentinel). The two
   request shapes are **mutually exclusive**; naming neither is the
   mandatory-scope 422 the binary-scope posture already enforces.

2. :func:`search_docs_fanout` — query each resolved collection on its own
   backend concurrently (bounded by :data:`_MAX_CONCURRENT_BACKENDS` so a
   wide ``all`` fan-out cannot open an unbounded number of simultaneous
   backend round-trips), tagging every returned chunk with its source
   ``collection`` for provenance.

3. :func:`rrf_merge` — fuse the per-collection ranked lists into one
   ranked list by RRF. Raw backend scores are **not** comparable across
   collections (different embedding models / backends), so the merge is
   strictly **rank-based**: a chunk's fused score is
   ``sum over the collections it ranked in: 1 / (RRF_K + rank)`` with
   1-based ranks — the same math (and the same :data:`RRF_K`) the hybrid
   retriever and the operation search already use, never a raw-score sort
   (#1177 / #1178: determinism over a tunable). A chunk is keyed by
   ``(collection, chunk_id)`` so the same ``chunk_id`` surfacing in two
   collections stays two distinct, separately-attributed hits.

Shared-embedding-model caveat (guidance, not enforced): RRF is rank-based,
so it *tolerates* divergent embedding spaces across collections — but
collections that share an embedding model fuse more meaningfully (their
ranks reflect the same notion of similarity). The fan-out does not require
a shared model; this is recorded as routing guidance for operators, not a
gate.

Entitlement + readiness are enforced by
:func:`~meho_backplane.docs_search.collection_access.resolve_entitled_ready_collections`
*before* this module runs: by the time :func:`search_docs_fanout` is
called the collection set is already entitled, ready, and resolved to
backends — non-entitled / not-ready collections were dropped-and-logged
there, never queried here.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

import structlog
from pydantic import BaseModel, ConfigDict

from meho_backplane.docs_search.service import (
    DocsChunk,
    DocsSearchResult,
    _project_chunk,
)
from meho_backplane.retrieval.retriever import RRF_K

if TYPE_CHECKING:
    from meho_backplane.auth.corpus import CorpusSearchResponse
    from meho_backplane.auth.operator import Operator
    from meho_backplane.docs_collections import DocCollection

__all__ = [
    "CollectionScope",
    "ConflictingCollectionScopeError",
    "parse_collection_scope",
    "rrf_merge",
    "search_docs_fanout",
]

_log = structlog.get_logger(__name__)

#: The sentinel ``collection`` value that selects every entitled, ready
#: collection for fan-out. Matches the ``"all"`` no-filter sentinel the
#: approvals / memory-view surfaces already use, rather than inventing a
#: new spelling.
ALL_COLLECTIONS_SENTINEL: Final[str] = "all"

#: Cap on simultaneous backend round-trips during a fan-out. A wide
#: ``all`` fan-out across many collections must not open an unbounded
#: number of concurrent backend connections; the semaphore bounds the
#: in-flight set while still overlapping the latency of independent
#: backends (the win over a serial loop). Tuned conservatively — the
#: federation backends are remote and per-collection ``limit``-bounded.
_MAX_CONCURRENT_BACKENDS: Final[int] = 5


class ConflictingCollectionScopeError(ValueError):
    """Raised when a request names both ``collection`` and ``collections``.

    The single-collection scope (``collection``) and the fan-out scope
    (``collections`` / the ``all`` sentinel) are **mutually exclusive** —
    a request that sets both is ambiguous about whether it wants one
    collection or a fused set. Surfaces map this to a 422 (REST) /
    ``-32602`` (MCP): invalid params, not a server fault.
    """

    def __init__(self) -> None:
        super().__init__(
            "search_docs accepts either 'collection' (single) or 'collections' "
            "(fan-out) / collection='all', not both"
        )


class CollectionScope(BaseModel):
    """The parsed collection scope of a ``search_docs`` request.

    Three possible shapes, distinguished so the caller can route each
    correctly:

    * **Single** — ``single`` set, not a fan-out. The T3 single-collection
      path the caller handles directly.
    * **Fan-out** — ``is_all`` ``True`` (the ``all`` sentinel → resolve every
      entitled ready collection) or ``fanout_keys`` a non-empty sorted key
      list (an explicit ``collections=[…]`` request).
    * **Empty** — neither single nor fan-out (``single`` ``None``,
      ``is_all`` ``False``, ``fanout_keys`` ``None``): no collection scope at
      all. The caller's single path runs
      :func:`~meho_backplane.docs_search.build_docs_scope`, which raises the
      mandatory-scope 422 — so the missing-scope rejection stays in one
      place rather than being duplicated here.

    Frozen value object built by :func:`parse_collection_scope`; the caller
    branches on :meth:`is_fanout`.
    """

    model_config = ConfigDict(frozen=True)

    single: str | None = None
    fanout_keys: tuple[str, ...] | None = None
    is_all: bool = False

    def is_fanout(self) -> bool:
        """Whether this scope fans out across multiple collections.

        Only ``True`` for a genuine fan-out (the ``all`` sentinel or an
        explicit key list) — **not** for the empty scope (``single`` ``None``
        with no fan-out), which must fall through to the single path so
        ``build_docs_scope`` raises the mandatory-scope 422.
        """
        return self.is_all or self.fanout_keys is not None

    def requested_keys(self) -> list[str] | None:
        """The explicit fan-out keys, or ``None`` for the ``all`` sentinel.

        Only meaningful on the fan-out arm; ``None`` under ``all`` (resolve
        every entitled ready collection) and under the single arm.
        """
        if self.fanout_keys is None:
            return None
        return list(self.fanout_keys)


def parse_collection_scope(
    collection: str | None,
    collections: list[str] | None,
) -> CollectionScope:
    """Parse the mutually-exclusive ``collection`` / ``collections`` arguments.

    Resolution rules:

    * Both ``collection`` and a non-empty ``collections`` set →
      :class:`ConflictingCollectionScopeError` (mutually exclusive).
    * ``collection == "all"`` (the sentinel) → fan-out across every entitled
      ready collection (``is_all=True``).
    * a non-empty ``collections`` list → fan-out across exactly those keys
      (deduplicated, blank-stripped, sorted for determinism).
    * a single non-sentinel ``collection`` → the single-collection scope.
    * neither → an *empty* single scope (``single=None``, not a fan-out);
      the caller's :func:`~meho_backplane.docs_search.build_docs_scope`
      raises the mandatory-scope 422, so the missing-scope rejection stays
      in one place.

    Args:
        collection: The single ``collection`` argument, the ``all``
            sentinel, or ``None``.
        collections: The explicit fan-out list, or ``None``.

    Returns:
        The parsed :class:`CollectionScope`.

    Raises:
        ConflictingCollectionScopeError: both scopes were supplied.
    """
    norm_collection = collection.strip() if collection and collection.strip() else None
    norm_collections = _normalise_collections(collections)

    if norm_collection is not None and norm_collections:
        raise ConflictingCollectionScopeError()

    if norm_collection == ALL_COLLECTIONS_SENTINEL:
        return CollectionScope(is_all=True)

    if norm_collections:
        return CollectionScope(fanout_keys=tuple(norm_collections))

    # No fan-out scope: the single arm (possibly empty → the caller's
    # build_docs_scope raises the mandatory-scope 422).
    return CollectionScope(single=norm_collection)


def _normalise_collections(collections: list[str] | None) -> list[str]:
    """Strip-blank, deduplicate, and sort an explicit ``collections`` list.

    Sorting makes the fan-out order — and the derived ``audit_collection``
    set — deterministic regardless of the order the client listed the keys.
    """
    if not collections:
        return []
    return sorted({c.strip() for c in collections if c and c.strip()})


async def _search_one(
    operator: Operator,
    query: str,
    *,
    collection: DocCollection,
    limit: int,
    semaphore: asyncio.Semaphore,
) -> list[DocsChunk]:
    """Search one collection's backend, tagging hits with its provenance.

    Holds *semaphore* across the backend round-trip so the number of
    simultaneous backend calls stays bounded. Resolves the collection's
    backend and projects the corpus chunks into MEHO's surface tagged with
    the source ``collection`` key.
    """
    # Imported lazily to keep the module-import graph identical to the
    # single-collection path (service.py already owns the backend resolve).
    from meho_backplane.docs_search.backends import resolve_backend

    async with semaphore:
        resolved = resolve_backend(collection)
        response: CorpusSearchResponse = await resolved.backend.search(
            operator,
            query,
            backend_ref=resolved.ref,
            metadata_filters=None,
            limit=limit,
        )
    return [_project_chunk(c, collection=collection.collection_key) for c in response.chunks]


async def search_docs_fanout(
    operator: Operator,
    query: str,
    *,
    collections: list[DocCollection],
    limit: int = 10,
) -> DocsSearchResult:
    """Fan *query* out across *collections* and RRF-merge the ranked lists.

    Each collection is queried **independently** on its own backend (T2's
    :func:`~meho_backplane.docs_search.backends.resolve_backend`),
    concurrently but bounded by :data:`_MAX_CONCURRENT_BACKENDS`. Every
    chunk is tagged with its source ``collection``. The per-collection
    ranked lists are then fused by :func:`rrf_merge` — rank-based, never a
    raw-score sort — and truncated to *limit*.

    *collections* is the already-entitled, already-ready set
    :func:`~meho_backplane.docs_search.collection_access.resolve_entitled_ready_collections`
    returned, so this function does no entitlement / readiness work — it
    only federates and fuses.

    Args:
        operator: The verified operator whose JWT each backend adapter
            forwards.
        query: The free-text query, sent verbatim to every collection.
        collections: The entitled, ready collections to fan out across
            (non-empty; the resolver raises when the set is empty).
        limit: The per-collection request cap **and** the merged-result
            cap. Each backend is asked for up to *limit* chunks and the
            fused list is truncated to *limit*.

    Returns:
        A :class:`DocsSearchResult` whose chunks are the RRF-fused hits
        (best first), each carrying its source ``collection``.

    Raises:
        CorpusUnavailable: propagated from any one collection's backend —
            a fan-out is fail-closed: if one collection's backend is
            unconfigured / unreachable, the whole query is 503 rather than
            silently returning a partial fused list that omits a
            collection the operator asked for. The surface maps it the same
            way the single-collection path does.
    """
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_BACKENDS)
    per_collection: list[list[DocsChunk]] = await asyncio.gather(
        *(
            _search_one(operator, query, collection=c, limit=limit, semaphore=semaphore)
            for c in collections
        )
    )
    merged = rrf_merge(per_collection, limit=limit)
    _log.info(
        "docs_search_fanout_completed",
        operator_sub=operator.sub,
        collections=[c.collection_key for c in collections],
        hit_count=len(merged),
    )
    return DocsSearchResult(chunks=merged)


def rrf_merge(
    ranked_lists: list[list[DocsChunk]],
    *,
    limit: int,
) -> list[DocsChunk]:
    """Fuse per-collection ranked chunk lists by Reciprocal Rank Fusion.

    The cross-collection analogue of the hybrid retriever's ``_rrf_fuse``
    (:mod:`meho_backplane.retrieval.retriever`): same math, same
    :data:`RRF_K`, but keyed on ``(collection, chunk_id)`` chunks instead of
    document UUIDs. Replicated rather than imported because the retriever's
    helper is private and keyed on document UUIDs — the same
    deliberate-replication the operation search already does — so the one
    shared thing is the :data:`RRF_K` constant.

    A chunk's fused score is the sum over every collection list it appears
    in of ``1 / (RRF_K + rank)``, with 1-based ranks (position 0 in a list
    is rank 1). Raw backend scores are **never** consulted — they are not
    comparable across collections — so the merge is purely rank-based and
    deterministic. Ties (equal fused score) break on the
    ``(collection, chunk_id)`` key so the order is stable across runs.

    Args:
        ranked_lists: One ranked :class:`DocsChunk` list per collection, in
            backend rank order (best first). Each chunk carries its source
            ``collection``.
        limit: How many top-fused chunks to return. ``<= 0`` → empty list.

    Returns:
        The top-*limit* fused chunks, best fused score first.
    """
    if limit <= 0:
        return []

    fused_score: dict[tuple[str | None, str], float] = {}
    chunk_by_key: dict[tuple[str | None, str], DocsChunk] = {}

    for ranked in ranked_lists:
        for rank0, chunk in enumerate(ranked):
            key = (chunk.collection, chunk.chunk_id)
            fused_score[key] = fused_score.get(key, 0.0) + 1.0 / (RRF_K + rank0 + 1)
            # Keep the first projection seen for a key; identical chunks
            # across a collection's own list are deduplicated to one hit.
            chunk_by_key.setdefault(key, chunk)

    # Sort by fused score desc, breaking ties on the stable key so the
    # order is deterministic across runs (no reliance on dict insertion
    # order for equal-score chunks).
    ordered_keys = sorted(
        fused_score,
        key=lambda k: (-fused_score[k], k[0] or "", k[1]),
    )
    return [chunk_by_key[k] for k in ordered_keys[:limit]]

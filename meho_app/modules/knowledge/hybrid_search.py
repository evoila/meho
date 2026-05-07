# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Hybrid retrieval over the knowledge base.

Combines two retrieval signals:

1. **BM25** - Redis-cached lexical search via :class:`BM25Service` (rank_bm25 +
   Porter stemmer). Excels at exact-term queries (model numbers, error codes).
2. **Vector** - pgvector cosine similarity over the chunk embeddings. Excels
   at paraphrased / semantic queries.

The two ranked candidate lists are fused with **reciprocal rank fusion**
(``score = sum(weight / (k + rank))``, ``k=60``).

The cross-encoder reranker is intentionally absent from this preview path -
it returns when MEHO.Knowledge takes over remote retrieval. The
:meth:`search_with_rerank` and :meth:`adaptive_search` methods are retained
as thin wrappers around :meth:`search` so callers in ``service.py`` and
``ask_mode.py`` stay backwards-compatible.

The class name :class:`PostgresFTSHybridService` is preserved for callers
that import it from earlier revisions.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger

from .bm25_service import BM25Service
from .retrieval_context import build_retrieval_text_from_metadata

if TYPE_CHECKING:
    from uuid import UUID

    from redis.asyncio import Redis

    from meho_app.core.auth_context import UserContext

    from .embeddings import EmbeddingProvider
    from .repository import KnowledgeRepository

logger = get_logger(__name__)

RERANK_CANDIDATE_MULTIPLIER = 5
RERANK_CANDIDATE_LIMIT = 100
# Constant from the canonical RRF paper (Cormack et al., SIGIR 2009).
# Damps the effect of low-rank items so the top-of-list dominates.
_RRF_K = 60


class PostgresFTSHybridService:
    """Hybrid BM25 + pgvector retrieval (no cross-encoder reranker in this preview)."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        embeddings: EmbeddingProvider,
        bm25_service: BM25Service | None = None,
        redis: Redis | None = None,
    ) -> None:
        self.repository = repository
        self.embeddings = embeddings
        self.bm25_service = bm25_service or BM25Service(repository.session, redis=redis)
        # Backwards-compat attribute for callers that introspected this:
        # the reranker is gone in the preview, so it's always None.
        self.reranker: Any | None = None

        logger.info(
            "hybrid_search_initialized",
            search_type="bm25_vector_rrf",
            reranker_available=False,
        )

    async def search(
        self,
        query: str,
        user_context: UserContext,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
        score_threshold: float = 0.0,
        bm25_weight: float = 0.5,
        semantic_weight: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Run BM25 + vector retrieval in parallel and fuse with RRF."""
        if not user_context.tenant_id:
            raise ValueError("tenant_id is required for hybrid search")

        candidate_k = min(max(top_k * RERANK_CANDIDATE_MULTIPLIER, top_k), RERANK_CANDIDATE_LIMIT)

        vector_task = self._vector_search(
            query=query,
            user_context=user_context,
            filters=filters,
            top_k=candidate_k,
            score_threshold=score_threshold,
        )
        bm25_task = self._bm25_search(
            tenant_id=user_context.tenant_id,
            query=query,
            top_k=candidate_k,
            metadata_filters=filters,
        )

        vector_results, bm25_results = await asyncio.gather(vector_task, bm25_task)

        fused = _reciprocal_rank_fusion(
            vector_results=vector_results,
            bm25_results=bm25_results,
            bm25_weight=bm25_weight,
            semantic_weight=semantic_weight,
        )

        logger.info(
            "hybrid_search_completed",
            query=query,
            tenant_id=str(user_context.tenant_id),
            vector_hits=len(vector_results),
            bm25_hits=len(bm25_results),
            fused_hits=len(fused),
            top_k=top_k,
        )

        return fused[:top_k]

    async def search_with_rerank(
        self,
        query: str,
        user_context: UserContext,
        top_k: int = 10,
        rerank_candidates: int = 50,  # noqa: ARG002 -- preserved for API stability
        filters: dict[str, Any] | None = None,
        score_threshold: float = 0.0,
        bm25_weight: float = 0.5,
        semantic_weight: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Backwards-compat wrapper around :meth:`search`.

        The cross-encoder reranker is absent in this preview, so this
        method delegates to plain hybrid retrieval.
        """
        return await self.search(
            query=query,
            user_context=user_context,
            filters=filters,
            top_k=top_k,
            score_threshold=score_threshold,
            bm25_weight=bm25_weight,
            semantic_weight=semantic_weight,
        )

    async def adaptive_search(
        self,
        query: str,
        user_context: UserContext,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
        score_threshold: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Hybrid retrieval sized for typical UI use; no reranker in the preview."""
        return await self.search(
            query=query,
            user_context=user_context,
            filters=filters,
            top_k=top_k,
            score_threshold=score_threshold,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _vector_search(
        self,
        query: str,
        user_context: UserContext,
        filters: dict[str, Any] | None,
        top_k: int,
        score_threshold: float,
    ) -> list[dict[str, Any]]:
        query_vector = await self.embeddings.embed_text(query)
        rows = await self.repository.search_by_embedding(
            query_embedding=query_vector,
            user_context=user_context,
            top_k=top_k,
            score_threshold=score_threshold,
            metadata_filters=filters,
        )

        out: list[dict[str, Any]] = []
        for chunk, similarity in rows:
            raw_metadata: Any = chunk.search_metadata or {}
            metadata = (
                raw_metadata.model_dump()
                if hasattr(raw_metadata, "model_dump")
                else dict(raw_metadata)
                if isinstance(raw_metadata, dict)
                else {}
            )
            out.append(
                {
                    "id": str(chunk.id),
                    "text": chunk.text,
                    "metadata": metadata,
                    "source_uri": chunk.source_uri,
                    "tags": chunk.tags or [],
                    "similarity": similarity,
                    "semantic_score": similarity,
                    "score": similarity,
                    "retrieval_text": build_retrieval_text_from_metadata(
                        text=chunk.text,
                        source_uri=chunk.source_uri,
                        metadata=metadata,
                    ),
                }
            )
        return out

    async def _bm25_search(
        self,
        tenant_id: UUID | str,
        query: str,
        top_k: int,
        metadata_filters: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        try:
            return await self.bm25_service.search(
                tenant_id=tenant_id,
                query=query,
                top_k=top_k,
                metadata_filters=metadata_filters,
            )
        except Exception as exc:  # noqa: BLE001 -- BM25 errors degrade gracefully to vector-only
            logger.warning("bm25_search_failed", error=str(exc), error_type=type(exc).__name__)
            return []


def _reciprocal_rank_fusion(
    vector_results: list[dict[str, Any]],
    bm25_results: list[dict[str, Any]],
    bm25_weight: float,
    semantic_weight: float,
) -> list[dict[str, Any]]:
    """Fuse two ranked lists using reciprocal rank fusion (k=60).

    The contribution of each list is weighted by ``semantic_weight`` /
    ``bm25_weight``. Equal weights (0.5/0.5) recover plain RRF.
    """
    by_id: dict[str, dict[str, Any]] = {}

    for rank, hit in enumerate(vector_results):
        chunk_id = str(hit["id"])
        contribution = semantic_weight / (_RRF_K + rank + 1)
        entry = by_id.setdefault(chunk_id, dict(hit))
        entry["rrf_score"] = entry.get("rrf_score", 0.0) + contribution
        entry["semantic_score"] = hit.get("semantic_score", hit.get("similarity", 0.0))

    for rank, hit in enumerate(bm25_results):
        chunk_id = str(hit["id"])
        contribution = bm25_weight / (_RRF_K + rank + 1)
        if chunk_id in by_id:
            entry = by_id[chunk_id]
            entry["rrf_score"] = entry.get("rrf_score", 0.0) + contribution
            entry["bm25_score"] = hit.get("bm25_score", 0.0)
        else:
            metadata = hit.get("metadata") or {}
            entry = {
                "id": chunk_id,
                "text": hit.get("text", ""),
                "metadata": metadata,
                "source_uri": hit.get("source_uri"),
                "tags": hit.get("tags", []),
                "similarity": 0.0,
                "semantic_score": 0.0,
                "bm25_score": hit.get("bm25_score", 0.0),
                "rrf_score": contribution,
                "retrieval_text": build_retrieval_text_from_metadata(
                    text=hit.get("text", ""),
                    source_uri=hit.get("source_uri"),
                    metadata=metadata,
                ),
            }
            by_id[chunk_id] = entry

    fused = list(by_id.values())
    for entry in fused:
        entry["score"] = entry["rrf_score"]

    fused.sort(key=lambda e: e["rrf_score"], reverse=True)
    return fused

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Compatibility search service for MEHO knowledge retrieval.

The legacy `PostgresFTSHybridService` name is retained for compatibility, but the
implementation now follows the Farseer-style retrieval path:

1. semantic vector ranking via pgvector
2. optional Voyage/TEI reranking on retrieval text

The older PostgreSQL FTS + RRF path has been removed to avoid ranking drift.
"""

from typing import Any

from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger

from .embeddings import EmbeddingProvider
from .repository import KnowledgeRepository
from .reranker import RerankerProvider
from .retrieval_context import build_retrieval_text_from_metadata

logger = get_logger(__name__)
RERANK_CANDIDATE_MULTIPLIER = 5
RERANK_CANDIDATE_LIMIT = 100


class PostgresFTSHybridService:
    """
    Legacy compatibility wrapper for the current semantic ranker + reranker flow.

    The class name remains stable to avoid widespread call-site churn, but it no
    longer performs PostgreSQL full-text search or reciprocal rank fusion.
    """

    def __init__(
        self,
        repository: KnowledgeRepository,
        embeddings: EmbeddingProvider,
        reranker: RerankerProvider | None = None,
    ) -> None:
        """
        Initialize compatibility search service.

        Args:
            repository: Knowledge repository (provides DB session)
            embeddings: Embedding provider for semantic search
            reranker: Optional reranker for post-retrieval precision boost
        """
        self.repository = repository
        self.embeddings = embeddings
        self.reranker = reranker

        logger.info(
            "postgres_fts_hybrid_search_service_initialized",
            search_type="semantic_rank_rerank",
            reranker_available=reranker is not None,
            legacy_class_name=True,
        )

    async def search(
        self,
        query: str,
        user_context: UserContext,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
        score_threshold: float = 0.7,
        bm25_weight: float = 0.5,
        semantic_weight: float = 0.5,
    ) -> list[dict[str, Any]]:
        """
        Rank candidates semantically without reranking.

        Args:
            query: Search query
            user_context: User context for ACL
            filters: Metadata filters (applied to semantic search)
            top_k: Number of final results
            score_threshold: Minimum similarity score for semantic search
            bm25_weight: Ignored legacy parameter retained for compatibility
            semantic_weight: Ignored legacy parameter retained for compatibility

        Returns:
            Semantically ranked results
        """
        logger.info(
            "hybrid_search_started",
            query=query,
            tenant_id=user_context.tenant_id,
            top_k=top_k,
            bm25_weight=bm25_weight,
            semantic_weight=semantic_weight,
            search_mode="semantic_rank_only",
        )

        if not user_context.tenant_id:
            raise ValueError("tenant_id is required for hybrid search")

        query_vector = await self.embeddings.embed_text(query, input_type="query")
        semantic_results_tuples = await self.repository.search_by_embedding(
            query_embedding=query_vector,
            user_context=user_context,
            top_k=top_k,
            score_threshold=score_threshold,
            metadata_filters=filters,
        )

        results: list[dict[str, Any]] = []
        for chunk, similarity in semantic_results_tuples:
            raw_metadata: Any = chunk.search_metadata or {}
            metadata = (
                raw_metadata.model_dump()
                if hasattr(raw_metadata, "model_dump")
                else dict(raw_metadata)
                if isinstance(raw_metadata, dict)
                else {}
            )
            results.append(
                {
                    "id": chunk.id,
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

        logger.info(
            "hybrid_search_completed",
            query=query,
            num_results=len(results[:top_k]),
            top_score=results[0]["similarity"] if results else 0,
            search_mode="semantic_rank_only",
        )

        return results[:top_k]

    async def search_with_rerank(
        self,
        query: str,
        user_context: UserContext,
        top_k: int = 10,
        rerank_candidates: int = 50,
        filters: dict[str, Any] | None = None,
        score_threshold: float = 0.7,
    ) -> list[dict[str, Any]]:
        """
        Semantic ranking with post-retrieval reranking.

        Retrieves a wider set of candidates via semantic ranking, then uses
        Voyage AI rerank-2.5 cross-encoder to re-score and select the best top_k.
        Falls back to unreranked results when reranker is unavailable.

        Args:
            query: Search query
            user_context: User context for ACL
            top_k: Number of final results after reranking
            rerank_candidates: Number of candidates to retrieve before reranking (default 50)
            filters: Optional metadata filters
            score_threshold: Minimum similarity score for semantic search

        Returns:
            Reranked results (same dict format as search(), with added rerank_score field)
        """
        retrieval_threshold = 0.0 if self.reranker is not None else score_threshold
        candidates = await self.search(
            query=query,
            user_context=user_context,
            filters=filters,
            top_k=rerank_candidates,
            score_threshold=retrieval_threshold,
        )

        if not candidates:
            return []

        # Step 2: If no reranker or only 1 candidate, return as-is
        if self.reranker is None or len(candidates) <= 1:
            logger.debug(
                "search_with_rerank_skip",
                reason="no_reranker" if self.reranker is None else "single_candidate",
                num_candidates=len(candidates),
            )
            return candidates[:top_k]

        document_texts = [c["retrieval_text"] for c in candidates]
        rerank_results = await self.reranker.rerank(
            query=query,
            documents=document_texts,
            top_k=top_k,
        )

        reranked = []
        for rr in rerank_results:
            original_idx = rr["index"]
            if original_idx < len(candidates):
                candidate = candidates[original_idx].copy()
                candidate["rerank_score"] = rr["relevance_score"]
                candidate["score"] = rr["relevance_score"]
                reranked.append(candidate)

        logger.info(
            "search_with_rerank_completed",
            query=query,
            candidates_retrieved=len(candidates),
            reranked_results=len(reranked),
            top_rerank_score=reranked[0]["rerank_score"] if reranked else 0,
            search_mode="semantic_rank_rerank",
        )

        return reranked

    async def adaptive_search(
        self,
        query: str,
        user_context: UserContext,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
        score_threshold: float = 0.7,
    ) -> list[dict[str, Any]]:
        """
        Legacy compatibility entry point.

        The former adaptive BM25/semantic weighting behavior has been removed.
        This now delegates directly to the semantic ranker + reranker path.
        """
        rerank_candidates = min(
            max(top_k * RERANK_CANDIDATE_MULTIPLIER, top_k),
            RERANK_CANDIDATE_LIMIT,
        )
        return await self.search_with_rerank(
            query=query,
            user_context=user_context,
            filters=filters,
            top_k=top_k,
            rerank_candidates=rerank_candidates,
            score_threshold=score_threshold,
        )

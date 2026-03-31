# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Reranker providers for post-retrieval result reranking.

Reranking is a post-retrieval step that improves precision by
re-scoring top-N candidates from hybrid search using a cross-encoder model.
Typical improvement: 15-30% precision boost on top-10 results.

Providers:
- VoyageReranker: Enterprise mode (Voyage AI rerank-2.5, when VOYAGE_API_KEY is set)
- TEIReranker: Community mode (local TEI sidecar with bge-reranker-v2-m3)
"""

from typing import Any, Protocol, runtime_checkable

import httpx

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


@runtime_checkable
class RerankerProvider(Protocol):
    """Protocol for reranker providers."""

    async def rerank(
        self, query: str, documents: list[str], top_k: int = 10
    ) -> list[dict[str, Any]]: ...


class VoyageReranker:
    """
    Reranks search results using Voyage AI rerank-2.5 cross-encoder model.

    Usage:
        reranker = VoyageReranker(api_key="...")
        results = await reranker.rerank(query="how to ...", documents=["doc1", "doc2"], top_k=10)
    """

    def __init__(self, api_key: str, model: str = "rerank-2.5"):
        """
        Initialize Voyage AI reranker.

        Args:
            api_key: Voyage AI API key (same key used for embeddings)
            model: Reranking model name (default: rerank-2.5)
        """
        import voyageai  # Lazy import -- not needed in community mode

        self.client = voyageai.AsyncClient(api_key=api_key)
        self.model = model
        logger.info("voyage_reranker_initialized", model=model)

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Rerank documents by relevance to query using cross-encoder scoring.

        Args:
            query: Search query
            documents: List of document texts to rerank
            top_k: Number of top results to return

        Returns:
            List of dicts with keys: index, relevance_score, document
            Sorted by relevance_score descending.
            Falls back to original order on error.
        """
        if not documents:
            return []

        if len(documents) == 1:
            # Single document, no reranking needed
            return [{"index": 0, "relevance_score": 1.0, "document": documents[0]}]

        try:
            result = await self.client.rerank(
                query=query,
                documents=documents,
                model=self.model,
                top_k=top_k,
            )

            reranked = [
                {
                    "index": r.index,
                    "relevance_score": r.relevance_score,
                    "document": r.document,
                }
                for r in result.results
            ]

            logger.debug(
                "rerank_completed",
                query_len=len(query),
                input_docs=len(documents),
                output_docs=len(reranked),
                top_score=reranked[0]["relevance_score"] if reranked else 0,
                total_tokens=result.total_tokens,
            )

            return reranked

        except Exception as e:
            # Graceful degradation: if reranking fails, return unreranked results
            logger.warning(
                "rerank_failed_fallback_to_unreranked",
                error=str(e),
                error_type=type(e).__name__,
                query_len=len(query),
                num_documents=len(documents),
            )
            return [
                {"index": i, "relevance_score": 0.0, "document": doc}
                for i, doc in enumerate(documents[:top_k])
            ]


class TEIReranker:
    """Local TEI reranker using bge-reranker-v2-m3 via HTTP."""

    def __init__(self, base_url: str = "http://tei-reranker:80"):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        logger.info("tei_reranker_initialized", base_url=self.base_url)

    async def rerank(
        self, query: str, documents: list[str], top_k: int = 10
    ) -> list[dict[str, Any]]:
        """
        Rerank documents using TEI cross-encoder.

        Args:
            query: Search query
            documents: List of document texts to rerank
            top_k: Number of top results to return

        Returns:
            List of dicts with keys: index, relevance_score, document
            Sorted by relevance_score descending.
            Falls back to original order on error.
        """
        if not documents:
            return []
        if len(documents) == 1:
            return [{"index": 0, "relevance_score": 1.0, "document": documents[0]}]
        try:
            response = await self.client.post(
                "/rerank",
                json={"query": query, "texts": documents, "raw_scores": False},
            )
            response.raise_for_status()
            results = response.json()
            # TEI returns [{"index": N, "score": F}, ...] -- map to our format
            reranked = [
                {
                    "index": r["index"],
                    "relevance_score": r["score"],
                    "document": documents[r["index"]],
                }
                for r in sorted(results, key=lambda x: x["score"], reverse=True)[:top_k]
            ]
            logger.debug(
                "tei_rerank_completed",
                query_len=len(query),
                input_docs=len(documents),
                output_docs=len(reranked),
                top_score=reranked[0]["relevance_score"] if reranked else 0,
            )
            return reranked
        except Exception as e:
            logger.warning(
                "tei_rerank_failed_fallback",
                error=str(e),
                error_type=type(e).__name__,
            )
            return [
                {"index": i, "relevance_score": 0.0, "document": doc}
                for i, doc in enumerate(documents[:top_k])
            ]


# Singleton
_reranker: RerankerProvider | None = None


def get_reranker() -> RerankerProvider | None:
    """
    Get reranker singleton. TEI when no Voyage key, Voyage AI when key present.

    Returns None if reranker cannot be initialized (graceful degradation).
    """
    global _reranker

    if _reranker is None:
        try:
            from meho_app.core.config import get_config

            config = get_config()
            if config.voyage_api_key:
                _reranker = VoyageReranker(
                    api_key=config.voyage_api_key,
                    model="rerank-2.5",
                )
            else:
                _reranker = TEIReranker(base_url=config.tei_reranker_url)
        except Exception as e:
            logger.warning(
                "reranker_not_available",
                error=str(e),
                detail="Reranking disabled - search will use unreranked results",
            )
            return None

    return _reranker


def reset_reranker() -> None:
    """Reset reranker singleton (for testing)."""
    global _reranker
    _reranker = None

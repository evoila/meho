# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Knowledge module public service interface.
Other modules should ONLY import from this file.
"""

# Import protocols for type hints (import directly to avoid circular imports)
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from .embeddings import EmbeddingProvider, get_embedding_provider
from .hybrid_search import PostgresFTSHybridService
from .knowledge_store import KnowledgeStore
from .repository import KnowledgeRepository
from .reranker import get_reranker
from .schemas import (
    KnowledgeChunk,
    KnowledgeChunkFilter,
)

if TYPE_CHECKING:
    from meho_app.protocols.knowledge import (
        IHybridSearchService,
        IKnowledgeRepository,
        IKnowledgeStore,
    )


class KnowledgeService:
    """
    Public API for the knowledge module.

    This is the ONLY class other modules should use to interact with knowledge.

    Supports two construction patterns:

    1. Session-based (backward compatible):
        service = KnowledgeService(session)

    2. Protocol-based (for dependency injection):
        service = KnowledgeService.from_protocols(
            repository=mock_repo,
            embedding_provider=mock_embeddings,
            hybrid_search=mock_hybrid,
        )
    """

    def __init__(
        self,
        session: AsyncSession | None = None,
        *,
        repository: Optional["IKnowledgeRepository"] = None,
        embedding_provider: EmbeddingProvider | None = None,
        hybrid_search: Optional["IHybridSearchService"] = None,
        store: Optional["IKnowledgeStore"] = None,
    ) -> None:
        """
        Initialize KnowledgeService.

        Args:
            session: AsyncSession (creates concrete implementations)
            repository: Optional repository protocol implementation
            embedding_provider: Optional embedding provider
            hybrid_search: Optional hybrid search service
            store: Optional knowledge store (if provided, other params ignored)
        """
        if store is not None:
            # Direct store injection (for testing)
            self.store = store
            self.repository = repository
            self.embedding_provider = embedding_provider
            self.hybrid_search = hybrid_search
            self.session = session
        elif session is not None:
            # Backward compatible: build from session
            self.session = session
            self.repository = repository or KnowledgeRepository(session)  # type: ignore[assignment]
            self.embedding_provider = embedding_provider or get_embedding_provider()
            self.reranker = get_reranker()
            self.hybrid_search = hybrid_search or PostgresFTSHybridService(
                repository=self.repository,  # type: ignore[arg-type]
                embeddings=self.embedding_provider,
                reranker=self.reranker,
            )
            self.store = KnowledgeStore(  # type: ignore[assignment]
                repository=self.repository,  # type: ignore[arg-type]
                embedding_provider=self.embedding_provider,
                hybrid_search_service=self.hybrid_search,  # type: ignore[arg-type]
            )
        elif repository is not None and embedding_provider is not None:
            # Protocol-based construction (no session needed)
            self.session = None
            self.repository = repository
            self.embedding_provider = embedding_provider
            self.hybrid_search = hybrid_search
            self.store = KnowledgeStore(  # type: ignore[assignment]
                repository=self.repository,  # type: ignore[arg-type]
                embedding_provider=self.embedding_provider,
                hybrid_search_service=self.hybrid_search,  # type: ignore[arg-type]
            )
        else:
            raise ValueError(
                "KnowledgeService requires either 'session' or "
                "'repository' + 'embedding_provider' arguments"
            )

    @classmethod
    def from_protocols(
        cls,
        repository: "IKnowledgeRepository",
        embedding_provider: EmbeddingProvider,
        hybrid_search: Optional["IHybridSearchService"] = None,
    ) -> "KnowledgeService":
        """
        Create KnowledgeService from protocol implementations.

        This is the preferred constructor for testing and dependency injection.

        Args:
            repository: Knowledge repository implementation
            embedding_provider: Embedding provider implementation
            hybrid_search: Optional hybrid search service

        Returns:
            Configured KnowledgeService instance
        """
        return cls(
            session=None,
            repository=repository,
            embedding_provider=embedding_provider,
            hybrid_search=hybrid_search,
        )

    async def search(
        self,
        query: str,
        tenant_id: str,
        user_id: str | None = None,
        connector_id: str | None = None,
        roles: list[str] | None = None,
        groups: list[str] | None = None,
        top_k: int = 10,
        search_mode: str = "hybrid",
        metadata_filters: dict[str, Any] | None = None,
        system_id: str | None = None,  # Deprecated — kept for backward compat
    ) -> dict[str, Any]:
        """
        Search the knowledge base.

        Args:
            query: Search query string
            tenant_id: Tenant ID for access control
            user_id: Optional user ID for user-specific knowledge
            connector_id: Optional connector ID filter
            roles: User roles for RBAC
            groups: User groups for group-based access
            top_k: Number of results to return
            search_mode: "semantic", "bm25", or "hybrid"
            metadata_filters: Additional metadata filters

        Returns:
            Dict with chunks and scores
        """
        filters = KnowledgeChunkFilter(  # type: ignore[call-arg]
            tenant_id=tenant_id,
            user_id=user_id,
            connector_id=connector_id,
            roles=roles or [],
            groups=groups or [],
        )

        if search_mode == "hybrid":
            result = await self.store.hybrid_search(query, filters, top_k)  # type: ignore[attr-defined]
        elif search_mode == "bm25":
            result = await self.store.bm25_search(query, filters, top_k)  # type: ignore[attr-defined]
        else:
            result = await self.store.semantic_search(query, filters, top_k)  # type: ignore[attr-defined]

        return {
            "chunks": [
                chunk.model_dump() if hasattr(chunk, "model_dump") else chunk
                for chunk in result.chunks
            ],
            "scores": result.scores,
            "total": len(result.chunks),
        }

    async def search_with_rerank(
        self,
        query: str,
        tenant_id: str,
        user_id: str | None = None,
        connector_id: str | None = None,
        roles: list[str] | None = None,
        groups: list[str] | None = None,
        top_k: int = 10,
        rerank_candidates: int = 50,
        metadata_filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Search with Voyage AI reranking for improved precision.

        Retrieves rerank_candidates via hybrid search, then reranks to top_k
        using Voyage AI rerank-2.5 cross-encoder.

        Falls back to unreranked hybrid search if reranker is unavailable.

        Args:
            query: Search query string
            tenant_id: Tenant ID for access control
            user_id: Optional user ID for user-specific knowledge
            connector_id: Optional connector ID filter
            roles: User roles for RBAC
            groups: User groups for group-based access
            top_k: Number of final results after reranking
            rerank_candidates: Number of candidates for reranking (default 50)
            metadata_filters: Additional metadata filters

        Returns:
            Dict with chunks and scores (reranked)
        """
        from meho_app.core.auth_context import UserContext

        user_context = UserContext(
            tenant_id=tenant_id,
            user_id=user_id or "",
            roles=roles or [],
            groups=groups or [],
        )

        results = await self.hybrid_search.search_with_rerank(  # type: ignore[union-attr]
            query=query,
            user_context=user_context,
            top_k=top_k,
            rerank_candidates=rerank_candidates,
            filters=metadata_filters,
        )

        return {
            "chunks": results,
            "scores": [r.get("rerank_score", r.get("rrf_score", 0)) for r in results],
            "total": len(results),
        }

    async def ingest_text(
        self,
        text: str,
        tenant_id: str,
        user_id: str | None = None,
        connector_id: str | None = None,
        roles: list[str] | None = None,
        groups: list[str] | None = None,
        tags: list[str] | None = None,
        source_uri: str | None = None,
        knowledge_type: str = "documentation",
        priority: int = 0,
        system_id: str | None = None,  # Deprecated — kept for backward compat
    ) -> dict[str, Any]:
        """Ingest text content into the knowledge base."""
        result = await self.store.ingest_text(  # type: ignore[attr-defined]
            text=text,
            tenant_id=tenant_id,
            user_id=user_id,
            connector_id=connector_id,
            roles=roles or [],
            groups=groups or [],
            tags=tags or [],
            source_uri=source_uri,
            knowledge_type=knowledge_type,
            priority=priority,
        )
        return {
            "chunk_ids": result.chunk_ids if hasattr(result, "chunk_ids") else [],
            "status": getattr(result, "status", "completed"),
        }

    async def get_chunk(self, chunk_id: str) -> KnowledgeChunk | None:
        """Get a chunk by ID."""
        return await self.repository.get_by_id(chunk_id)  # type: ignore[union-attr, no-any-return]

    async def delete_chunk(self, chunk_id: str) -> bool:
        """Delete a chunk by ID."""
        return await self.repository.delete(chunk_id)  # type: ignore[union-attr, no-any-return]

    async def list_chunks(
        self,
        tenant_id: str | None = None,
        connector_id: str | None = None,
        knowledge_type: str | None = None,
        limit: int = 50,
    ) -> list[KnowledgeChunk]:
        """List chunks with filters."""
        filters = KnowledgeChunkFilter(
            tenant_id=tenant_id,
            connector_id=connector_id,
            knowledge_type=knowledge_type,  # type: ignore[arg-type]
        )
        return await self.repository.list_chunks(filters, limit=limit)  # type: ignore[union-attr, no-any-return]


def get_knowledge_service(session: AsyncSession) -> KnowledgeService:
    """Factory function for getting a KnowledgeService instance."""
    return KnowledgeService(session)

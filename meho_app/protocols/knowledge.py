# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Protocol definitions for the Knowledge module.

These protocols define the interfaces that knowledge components must implement.
"""

from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

# Avoid circular imports - use string annotations
if TYPE_CHECKING:
    from meho_app.core.auth_context import UserContext
    from meho_app.modules.knowledge.schemas import KnowledgeChunk, KnowledgeChunkCreate


@runtime_checkable
class IKnowledgeRepository(Protocol):
    """
    Protocol for knowledge chunk storage operations.

    Implementations:
        - KnowledgeRepository (PostgreSQL + pgvector)
    """

    async def create_chunk(
        self, chunk: "KnowledgeChunkCreate", embedding: list[float] | None = None
    ) -> "KnowledgeChunk":
        """Create a new knowledge chunk with optional embedding."""
        ...

    async def get_chunk(self, chunk_id: str) -> Optional["KnowledgeChunk"]:
        """Get a chunk by ID (no ACL check)."""
        ...

    async def get_chunks_with_acl(
        self, chunk_ids: list[str], user_context: "UserContext"
    ) -> list["KnowledgeChunk"]:
        """Get multiple chunks with ACL enforcement."""
        ...

    async def delete_chunk(self, chunk_id: str) -> bool:
        """Delete a chunk by ID."""
        ...

    async def search_semantic(
        self,
        embedding: list[float],
        user_context: "UserContext",
        top_k: int = 10,
        score_threshold: float = 0.7,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search chunks by semantic similarity using embedding vector."""
        ...

    async def search_keyword(
        self, query: str, user_context: "UserContext", top_k: int = 10
    ) -> list[dict[str, Any]]:
        """Search chunks by keyword using PostgreSQL FTS."""
        ...


@runtime_checkable
class IHybridSearchService(Protocol):
    """
    Protocol for hybrid search combining semantic and keyword search.

    Implementations:
        - PostgresFTSHybridService (PostgreSQL FTS + pgvector)
        - BM25HybridService (BM25 + pgvector)
    """

    async def search(
        self,
        query: str,
        user_context: "UserContext",
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
        score_threshold: float = 0.7,
        bm25_weight: float = 0.5,
        semantic_weight: float = 0.5,
    ) -> list[dict[str, Any]]:
        """
        Hybrid search combining keyword and semantic search.

        Uses Reciprocal Rank Fusion (RRF) to merge results.
        """
        ...


@runtime_checkable
class IKnowledgeStore(Protocol):
    """
    Protocol for the unified knowledge store interface.

    This is the high-level interface that other modules should use
    for knowledge operations.

    Implementations:
        - KnowledgeStore
    """

    async def add_chunk(self, chunk_create: "KnowledgeChunkCreate") -> "KnowledgeChunk":
        """Add a knowledge chunk with automatic embedding generation."""
        ...

    async def search_hybrid(
        self,
        query: str,
        user_context: "UserContext",
        top_k: int = 10,
        score_threshold: float = 0.7,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search using hybrid search (semantic + keyword)."""
        ...

    async def search_semantic(
        self,
        query: str,
        user_context: "UserContext",
        top_k: int = 10,
        score_threshold: float = 0.7,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search using semantic similarity only."""
        ...

    async def get_chunks(
        self, chunk_ids: list[str], user_context: "UserContext"
    ) -> list["KnowledgeChunk"]:
        """Get chunks by IDs with ACL enforcement."""
        ...

    async def delete_chunk(self, chunk_id: str) -> bool:
        """Delete a chunk by ID."""
        ...

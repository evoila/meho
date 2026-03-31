# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
PostgreSQL Full-Text Search Service

Replaces BM25IndexManager with PostgreSQL's built-in full-text search.
Eliminates external file storage and provides better integration.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.knowledge.models import KnowledgeChunkModel

logger = get_logger(__name__)


class PostgresFTSService:
    """
    PostgreSQL Full-Text Search service.

    Uses PostgreSQL's built-in FTS with GIN indexes for keyword-based search.
    Replaces the BM25IndexManager pickle file approach.

    Advantages over BM25 pickle files:
    - Everything in PostgreSQL (no external storage)
    - Automatic index maintenance
    - Transactional consistency
    - No manual index rebuilding needed
    - Better concurrent access
    - Handles updates automatically
    """

    def __init__(self, session: AsyncSession):
        """
        Initialize PostgreSQL FTS service.

        Args:
            session: SQLAlchemy async session
        """
        self.session = session
        logger.info("postgres_fts_service_initialized")

    async def search(
        self,
        tenant_id: UUID,
        query: str,
        top_k: int = 100,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search using PostgreSQL full-text search.

        Args:
            tenant_id: Tenant ID (for ACL)
            query: Search query
            top_k: Number of results to return
            metadata_filters: Optional metadata filters to apply

        Returns:
            List of documents with FTS scores
        """
        logger.debug(
            "postgres_fts_search_started", tenant_id=str(tenant_id), query=query, top_k=top_k
        )

        # Convert tenant UUID to string for comparison
        # (knowledge_chunk.tenant_id is stored as string, not UUID)
        tenant_id_str = str(tenant_id)

        # Use websearch_to_tsquery for natural language queries
        # This handles phrases, AND/OR operators, and stemming automatically
        # e.g., "list virtual machines" → automatically handles stemming and relevance

        # Build rank expression using ts_rank
        # ts_rank scores documents by term frequency and position
        rank_expr = func.ts_rank(
            func.to_tsvector("english", KnowledgeChunkModel.text),
            func.websearch_to_tsquery("english", query),
        )

        # Build base query with FTS filter
        stmt = (
            select(
                KnowledgeChunkModel.id,
                KnowledgeChunkModel.text,
                KnowledgeChunkModel.search_metadata,
                rank_expr.label("fts_score"),
            )
            .where(KnowledgeChunkModel.tenant_id == tenant_id_str)
            .where(
                func.to_tsvector("english", KnowledgeChunkModel.text).op("@@")(
                    func.websearch_to_tsquery("english", query)
                )
            )
        )

        # Apply metadata filters if provided
        if metadata_filters:
            conditions = []
            for key, value in metadata_filters.items():
                # JSONB metadata filtering
                conditions.append(KnowledgeChunkModel.search_metadata[key].astext == str(value))
            if conditions:
                stmt = stmt.where(and_(*conditions))

        # Order by relevance and limit
        stmt = stmt.order_by(rank_expr.desc()).limit(top_k)

        # Execute query
        result = await self.session.execute(stmt)
        rows = result.all()

        # Format results
        results = []
        for row in rows:
            results.append(
                {
                    "id": str(row.id),
                    "text": row.text,
                    "metadata": row.search_metadata or {},
                    "fts_score": float(row.fts_score),
                }
            )

        logger.debug(
            "postgres_fts_search_completed",
            tenant_id=tenant_id_str,
            query=query,
            num_results=len(results),
            top_score=results[0]["fts_score"] if results else 0,
        )

        return results

    async def get_index_stats(self, tenant_id: UUID) -> dict[str, Any]:
        """
        Get statistics about indexed documents.

        Args:
            tenant_id: Tenant ID

        Returns:
            Index statistics
        """
        tenant_id_str = str(tenant_id)

        # Count documents for this tenant
        count_stmt = select(func.count(KnowledgeChunkModel.id)).where(
            KnowledgeChunkModel.tenant_id == tenant_id_str
        )

        result = await self.session.execute(count_stmt)
        doc_count = result.scalar()

        return {
            "exists": (doc_count or 0) > 0,
            "num_documents": doc_count or 0,
            "index_type": "postgresql_fts",
            "note": "Automatically maintained by PostgreSQL",
        }

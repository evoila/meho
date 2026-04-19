# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Service layer for memory business logic: embedding, deduplication, merge, and search ranking.
"""

# mypy: disable-error-code="arg-type,assignment,attr-defined,no-any-return"
import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.memory.models import ConfidenceLevel, ConnectorMemoryModel, MemoryType
from meho_app.modules.memory.repository import MemoryRepository
from meho_app.modules.memory.schemas import (
    BulkCreateMemoriesResponse,
    MemoryCreate,
    MemoryFilter,
    MemoryResponse,
    MemorySearchResult,
    MemoryUpdate,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Per-type cosine similarity thresholds for near-duplicate detection.
# Higher = stricter matching = fewer false merges.
DEDUP_THRESHOLDS = {
    MemoryType.ENTITY.value: 0.92,  # Tight: "node-3" vs "node-4" are different
    MemoryType.CONFIG.value: 0.90,  # Tight: specific config values need precision
    MemoryType.PATTERN.value: 0.88,  # Looser: different phrasings of same pattern
    MemoryType.OUTCOME.value: 0.85,  # Loosest: resolution descriptions vary
}

# Override via environment variable (applies as baseline if set)
DEFAULT_THRESHOLD = float(os.getenv("MEMORY_DEDUP_THRESHOLD", "0.90"))

# Confidence ranking for merge decisions (higher = more authoritative)
CONFIDENCE_RANK = {
    ConfidenceLevel.OPERATOR.value: 3,
    ConfidenceLevel.CONFIRMED_OUTCOME.value: 2,
    ConfidenceLevel.AUTO_EXTRACTED.value: 1,
}

# Confidence boost for search ranking
CONFIDENCE_BOOST = {
    ConfidenceLevel.OPERATOR.value: 1.3,
    ConfidenceLevel.CONFIRMED_OUTCOME.value: 1.15,
    ConfidenceLevel.AUTO_EXTRACTED.value: 1.0,
}

# Auto-promotion threshold: after this many occurrences, auto_extracted -> confirmed_outcome
AUTO_PROMOTION_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MemoryService:
    """Orchestrates embedding generation, deduplication, merge, and search ranking."""

    def __init__(self, repository: MemoryRepository, embedding_provider: Any) -> None:
        self.repository = repository
        self.embedding_provider = embedding_provider

    # ------------------------------------------------------------------
    # Create with dedup
    # ------------------------------------------------------------------

    async def create_with_dedup(self, memory_create: MemoryCreate) -> MemoryResponse:
        """
        Create a memory with write-time deduplication.

        Generates an embedding, checks for near-duplicates within the same
        connector and memory type, and either merges into an existing memory
        or inserts a new one.

        Args:
            memory_create: Memory creation data

        Returns:
            MemoryResponse (merged=True if merged, False if new)
        """
        # Generate embedding — MUST use input_type="document" for storage
        embed_text = f"{memory_create.title}\n{memory_create.body}"
        embedding = await self.embedding_provider.embed_text(embed_text, input_type="document")

        # Look up per-type dedup threshold
        threshold = DEDUP_THRESHOLDS.get(memory_create.memory_type, DEFAULT_THRESHOLD)

        # Check for near-duplicates
        similar = await self.repository.find_similar(
            connector_id=memory_create.connector_id,
            embedding=embedding,
            memory_type=memory_create.memory_type
            if isinstance(memory_create.memory_type, str)
            else memory_create.memory_type.value,
            threshold=threshold,
            tenant_id=memory_create.tenant_id,
        )

        if similar:
            # Merge into the most similar existing memory
            existing_model, _similarity = similar[0]
            logger.info(
                "memory_dedup_merge",
                existing_id=str(existing_model.id),
                similarity=_similarity,
                memory_type=memory_create.memory_type,
            )
            return await self._merge_memory(existing_model, memory_create)

        # No duplicate — create new
        db_memory = await self.repository.create_memory(memory_create, embedding)
        return self._model_to_response(db_memory, merged=False)

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    async def _merge_memory(
        self, existing: ConnectorMemoryModel, incoming: MemoryCreate
    ) -> MemoryResponse:
        """
        Merge an incoming memory into an existing near-duplicate.

        - Increments occurrence_count
        - Appends to provenance_trail
        - Updates last_seen
        - If incoming confidence > existing confidence: overwrites title/body and re-embeds
        - Auto-promotes auto_extracted -> confirmed_outcome at threshold

        Args:
            existing: The existing memory model to merge into
            incoming: The incoming memory creation data

        Returns:
            MemoryResponse with merged=True
        """
        now = datetime.now(tz=UTC)

        # Increment occurrence count
        existing.occurrence_count = (existing.occurrence_count or 1) + 1

        # Update last_seen
        existing.last_seen = now

        # Append to provenance trail
        trail = list(existing.provenance_trail or [])
        trail.append(
            {
                "conversation_id": incoming.conversation_id,
                "timestamp": now.isoformat(),
                "source": incoming.source_type,
            }
        )
        existing.provenance_trail = trail

        # Confidence comparison: higher-confidence incoming overwrites content
        incoming_conf = incoming.confidence_level
        if isinstance(incoming_conf, ConfidenceLevel):
            incoming_conf_value = incoming_conf.value
        else:
            incoming_conf_value = incoming_conf

        existing_conf_value = str(existing.confidence_level)

        incoming_rank = CONFIDENCE_RANK.get(incoming_conf_value, 0)
        existing_rank = CONFIDENCE_RANK.get(existing_conf_value, 0)

        if incoming_rank > existing_rank:
            existing.title = incoming.title
            existing.body = incoming.body
            existing.confidence_level = incoming_conf_value

            # Re-embed with new content — input_type="document"
            new_text = f"{incoming.title}\n{incoming.body}"
            new_embedding = await self.embedding_provider.embed_text(
                new_text, input_type="document"
            )
            existing.embedding = new_embedding

            logger.info(
                "memory_confidence_upgrade",
                memory_id=str(existing.id),
                old_confidence=existing_conf_value,
                new_confidence=incoming_conf_value,
            )

        # Auto-promotion: auto_extracted -> confirmed_outcome at threshold
        if (
            existing.confidence_level == ConfidenceLevel.AUTO_EXTRACTED.value
            and existing.occurrence_count >= AUTO_PROMOTION_THRESHOLD
        ):
            existing.confidence_level = ConfidenceLevel.CONFIRMED_OUTCOME.value
            logger.info(
                "memory_auto_promoted",
                memory_id=str(existing.id),
                occurrence_count=existing.occurrence_count,
            )

        existing.updated_at = now

        await self.repository.session.flush()

        return self._model_to_response(existing, merged=True)

    # ------------------------------------------------------------------
    # Bulk create
    # ------------------------------------------------------------------

    async def bulk_create(self, memories: list[MemoryCreate]) -> BulkCreateMemoriesResponse:
        """
        Create multiple memories with dedup for each.

        Args:
            memories: List of memory creation data

        Returns:
            BulkCreateMemoriesResponse with created/merged counts
        """
        created_count = 0
        merged_count = 0
        results: list[MemoryResponse] = []

        for memory in memories:
            response = await self.create_with_dedup(memory)
            results.append(response)
            if response.merged:
                merged_count += 1
            else:
                created_count += 1

        return BulkCreateMemoriesResponse(
            created=created_count,
            merged=merged_count,
            memories=results,
        )

    # ------------------------------------------------------------------
    # Search with ranking
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        connector_id: str,
        tenant_id: str,
        top_k: int = 10,
        score_threshold: float = 0.7,
        memory_type: str | None = None,
        confidence_level: str | None = None,
    ) -> list[MemorySearchResult]:
        """
        Semantic search with confidence boost and staleness decay.

        Generates a query embedding, fetches 2x candidates for reranking
        headroom, then applies confidence boost and staleness decay.

        Args:
            query: Natural language search query
            connector_id: Connector scope
            tenant_id: Tenant scope
            top_k: Number of results to return
            score_threshold: Minimum similarity (0-1)
            memory_type: Optional type filter
            confidence_level: Optional confidence filter

        Returns:
            List of MemorySearchResult ordered by final_score descending
        """
        # Generate query embedding — MUST use input_type="query" for search
        query_embedding = await self.embedding_provider.embed_text(query, input_type="query")

        # Fetch 2x for reranking headroom
        raw_results = await self.repository.search_by_embedding(
            connector_id=connector_id,
            tenant_id=tenant_id,
            query_embedding=query_embedding,
            top_k=top_k * 2,
            score_threshold=score_threshold,
            memory_type=memory_type,
            confidence_level=confidence_level,
        )

        now = datetime.now(tz=UTC)
        ranked: list[tuple[ConnectorMemoryModel, float, float]] = []

        for model, similarity in raw_results:
            # Confidence boost
            conf_boost = CONFIDENCE_BOOST.get(str(model.confidence_level), 1.0)

            # Staleness decay: 1.0 / (1 + days_since_last_access / 30)
            if model.last_accessed is not None:
                days_since = (now - model.last_accessed).total_seconds() / 86400
            else:
                days_since = 0  # Never accessed = treat as fresh

            staleness_factor = 1.0 / (1 + days_since / 30)

            final_score = similarity * conf_boost * staleness_factor
            ranked.append((model, similarity, final_score))

        # Sort by final_score descending, take top_k
        ranked.sort(key=lambda x: x[2], reverse=True)
        ranked = ranked[:top_k]

        return [
            MemorySearchResult(
                memory=self._model_to_response(model),
                similarity=similarity,
                final_score=final_score,
            )
            for model, similarity, final_score in ranked
        ]

    # ------------------------------------------------------------------
    # Delegated CRUD
    # ------------------------------------------------------------------

    async def get_memory(self, memory_id: str, tenant_id: str) -> MemoryResponse | None:
        """Get a memory by ID, converted to response schema."""
        model = await self.repository.get_memory(memory_id, tenant_id)
        if model is None:
            return None
        return self._model_to_response(model)

    async def list_memories(self, filter_params: MemoryFilter) -> list[MemoryResponse]:
        """List memories with filters, converted to response schemas."""
        models = await self.repository.list_memories(filter_params)
        return [self._model_to_response(m) for m in models]

    async def update_memory(
        self, memory_id: str, tenant_id: str, updates: MemoryUpdate
    ) -> MemoryResponse | None:
        """Update a memory, converted to response schema."""
        model = await self.repository.update_memory(memory_id, tenant_id, updates)
        if model is None:
            return None
        return self._model_to_response(model)

    async def delete_memory(self, memory_id: str, tenant_id: str) -> bool:
        """Delete a memory by ID."""
        return await self.repository.delete_memory(memory_id, tenant_id)

    async def delete_memories_bulk(self, memory_ids: list[str], tenant_id: str) -> int:
        """Bulk delete memories."""
        return await self.repository.delete_memories_bulk(memory_ids, tenant_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _model_to_response(
        self, model: ConnectorMemoryModel, merged: bool = False
    ) -> MemoryResponse:
        """
        Convert a ConnectorMemoryModel to a MemoryResponse schema.

        Handles UUID-to-str conversion and enum normalization.
        """
        return MemoryResponse(
            id=str(model.id),
            tenant_id=model.tenant_id,
            connector_id=str(model.connector_id),
            title=model.title,
            body=model.body,
            memory_type=model.memory_type,
            tags=model.tags or [],
            confidence_level=model.confidence_level,
            source_type=model.source_type,
            created_by=model.created_by,
            provenance_trail=model.provenance_trail or [],
            occurrence_count=model.occurrence_count or 1,
            last_accessed=model.last_accessed,
            last_seen=model.last_seen,
            created_at=model.created_at,
            updated_at=model.updated_at,
            merged=merged,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_memory_service(session: AsyncSession) -> MemoryService:
    """
    Factory to create a MemoryService with wired dependencies.

    Reuses the shared Voyage AI embedding provider singleton.
    """
    from meho_app.modules.knowledge.embeddings import get_embedding_provider

    repository = MemoryRepository(session)
    embedding_provider = get_embedding_provider()
    return MemoryService(repository, embedding_provider)

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Repository for memory CRUD, search, and deduplication operations.
"""

# mypy: disable-error-code="arg-type,assignment,attr-defined,no-any-return"
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.memory.models import ConnectorMemoryModel
from meho_app.modules.memory.schemas import MemoryCreate, MemoryFilter, MemoryUpdate

logger = get_logger(__name__)


class MemoryRepository:
    """Repository for connector memory operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_memory(
        self, memory: MemoryCreate, embedding: list[float]
    ) -> ConnectorMemoryModel:
        """
        Create a new memory record with embedding.

        Args:
            memory: Memory creation data
            embedding: Vector embedding (1024 dimensions for Voyage AI)

        Returns:
            Created ConnectorMemoryModel instance
        """
        memory_data = memory.model_dump()

        # Remove conversation_id — it's a convenience field, not a model column
        memory_data.pop("conversation_id", None)

        # Convert enum values to strings for SQLAlchemy
        if "memory_type" in memory_data and hasattr(memory_data["memory_type"], "value"):
            memory_data["memory_type"] = memory_data["memory_type"].value
        if "confidence_level" in memory_data and hasattr(memory_data["confidence_level"], "value"):
            memory_data["confidence_level"] = memory_data["confidence_level"].value

        memory_id = uuid.uuid4()

        db_memory = ConnectorMemoryModel(
            id=memory_id,
            embedding=embedding,
            **memory_data,
        )

        self.session.add(db_memory)
        await self.session.flush()
        await self.session.refresh(db_memory)

        logger.debug(
            "memory_created",
            memory_id=str(memory_id),
            connector_id=memory_data.get("connector_id"),
            memory_type=memory_data.get("memory_type"),
        )

        return db_memory

    async def get_memory(self, memory_id: str, tenant_id: str) -> ConnectorMemoryModel | None:
        """
        Get a memory by ID with tenant isolation.

        Args:
            memory_id: UUID string
            tenant_id: Tenant scope for isolation

        Returns:
            ConnectorMemoryModel if found, None otherwise
        """
        try:
            memory_uuid = uuid.UUID(memory_id)
        except ValueError:
            return None

        result = await self.session.execute(
            select(ConnectorMemoryModel).where(
                and_(
                    ConnectorMemoryModel.id == memory_uuid,
                    ConnectorMemoryModel.tenant_id == tenant_id,
                )
            )
        )
        return result.scalar_one_or_none()

    async def list_memories(self, filter_params: MemoryFilter) -> list[ConnectorMemoryModel]:
        """
        List memories with filters and pagination.

        Args:
            filter_params: Filter criteria (connector_id required)

        Returns:
            List of matching ConnectorMemoryModel instances
        """
        query = select(ConnectorMemoryModel)
        conditions = []

        # connector_id is required on MemoryFilter
        try:
            cid = uuid.UUID(filter_params.connector_id)
            conditions.append(ConnectorMemoryModel.connector_id == cid)
        except ValueError:
            return []

        # Tenant isolation
        if filter_params.tenant_id is not None:
            conditions.append(ConnectorMemoryModel.tenant_id == filter_params.tenant_id)

        # Type filter
        if filter_params.memory_type is not None:
            conditions.append(ConnectorMemoryModel.memory_type == filter_params.memory_type.value)

        # Confidence filter
        if filter_params.confidence_level is not None:
            conditions.append(
                ConnectorMemoryModel.confidence_level == filter_params.confidence_level.value
            )

        # Date range filters
        if filter_params.created_after is not None:
            conditions.append(ConnectorMemoryModel.created_at >= filter_params.created_after)

        if filter_params.created_before is not None:
            conditions.append(ConnectorMemoryModel.created_at <= filter_params.created_before)

        # Tags filter (AND logic — memory must contain all specified tags)
        if filter_params.tags:
            for tag in filter_params.tags:
                conditions.append(ConnectorMemoryModel.tags.contains([tag]))

        if conditions:
            query = query.where(and_(*conditions))

        # Ordering and pagination
        query = query.order_by(ConnectorMemoryModel.created_at.desc())
        query = query.limit(filter_params.limit).offset(filter_params.offset)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def update_memory(
        self, memory_id: str, tenant_id: str, updates: MemoryUpdate
    ) -> ConnectorMemoryModel | None:
        """
        Update a memory with PATCH semantics (only non-None fields).

        Args:
            memory_id: UUID string
            tenant_id: Tenant scope for isolation
            updates: Fields to update

        Returns:
            Updated ConnectorMemoryModel if found, None otherwise
        """
        db_memory = await self.get_memory(memory_id, tenant_id)
        if db_memory is None:
            return None

        update_data = updates.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            if value is not None:
                # Convert enum values to strings
                if hasattr(value, "value"):
                    value = value.value
                setattr(db_memory, field, value)

        db_memory.updated_at = datetime.now(tz=UTC)

        await self.session.flush()
        return db_memory

    async def delete_memory(self, memory_id: str, tenant_id: str) -> bool:
        """
        Delete a memory by ID with tenant isolation.

        Args:
            memory_id: UUID string
            tenant_id: Tenant scope for isolation

        Returns:
            True if deleted, False if not found
        """
        db_memory = await self.get_memory(memory_id, tenant_id)
        if db_memory is None:
            return False

        await self.session.delete(db_memory)
        await self.session.flush()
        return True

    async def delete_memories_bulk(self, memory_ids: list[str], tenant_id: str) -> int:
        """
        Bulk delete memories with tenant isolation.

        Args:
            memory_ids: List of UUID strings
            tenant_id: Tenant scope for isolation

        Returns:
            Number of memories deleted
        """
        if not memory_ids:
            return 0

        try:
            memory_uuids = [uuid.UUID(mid) for mid in memory_ids]
        except ValueError:
            return 0

        result = await self.session.execute(
            delete(ConnectorMemoryModel).where(
                and_(
                    ConnectorMemoryModel.id.in_(memory_uuids),
                    ConnectorMemoryModel.tenant_id == tenant_id,
                )
            )
        )
        await self.session.flush()

        deleted_count = result.rowcount
        logger.info(
            "memories_bulk_deleted",
            count=deleted_count,
            tenant_id=tenant_id,
        )
        return deleted_count

    async def find_similar(
        self,
        connector_id: str,
        embedding: list[float],
        memory_type: str,
        threshold: float,
        tenant_id: str,
    ) -> list[tuple[ConnectorMemoryModel, float]]:
        """
        Find similar memories for deduplication using cosine similarity.

        Args:
            connector_id: Connector scope (dedup is per-connector)
            embedding: Query embedding vector
            memory_type: Only compare within same type
            threshold: Cosine similarity threshold (0-1)
            tenant_id: Tenant scope for isolation

        Returns:
            List of (model, cosine_similarity) tuples ordered by similarity desc
        """
        try:
            cid = uuid.UUID(connector_id)
        except ValueError:
            return []

        # pgvector cosine_distance: 0 = identical, 2 = opposite
        # similarity = 1 - (distance / 2), so distance = 2 * (1 - similarity)
        max_distance = 2 * (1 - threshold)

        distance_expr = ConnectorMemoryModel.embedding.cosine_distance(embedding)

        query = (
            select(ConnectorMemoryModel, distance_expr.label("distance"))
            .where(
                and_(
                    ConnectorMemoryModel.connector_id == cid,
                    ConnectorMemoryModel.memory_type == memory_type,
                    ConnectorMemoryModel.tenant_id == tenant_id,
                    distance_expr < max_distance,
                )
            )
            .order_by("distance")
            .limit(5)
        )

        result = await self.session.execute(query)
        rows = result.all()

        # Convert distance to similarity
        return [(model, 1 - (distance / 2)) for model, distance in rows]

    async def search_by_embedding(
        self,
        connector_id: str,
        tenant_id: str,
        query_embedding: list[float],
        top_k: int = 10,
        score_threshold: float = 0.7,
        memory_type: str | None = None,
        confidence_level: str | None = None,
    ) -> list[tuple[ConnectorMemoryModel, float]]:
        """
        Semantic search for memories using cosine similarity.

        Updates last_accessed on returned memories for staleness tracking.

        Args:
            connector_id: Connector scope
            tenant_id: Tenant scope for isolation
            query_embedding: Query vector (1024 dimensions)
            top_k: Maximum results
            score_threshold: Minimum similarity (0-1)
            memory_type: Optional type filter
            confidence_level: Optional confidence filter

        Returns:
            List of (model, similarity) tuples ordered by distance
        """
        try:
            cid = uuid.UUID(connector_id)
        except ValueError:
            return []

        max_distance = 2 * (1 - score_threshold)
        distance_expr = ConnectorMemoryModel.embedding.cosine_distance(query_embedding)

        conditions = [
            ConnectorMemoryModel.connector_id == cid,
            ConnectorMemoryModel.tenant_id == tenant_id,
            distance_expr < max_distance,
        ]

        if memory_type is not None:
            conditions.append(ConnectorMemoryModel.memory_type == memory_type)

        if confidence_level is not None:
            conditions.append(ConnectorMemoryModel.confidence_level == confidence_level)

        query = (
            select(ConnectorMemoryModel, distance_expr.label("distance"))
            .where(and_(*conditions))
            .order_by("distance")
            .limit(top_k)
        )

        result = await self.session.execute(query)
        rows = result.all()

        # Convert distance to similarity and collect results
        results = [(model, 1 - (distance / 2)) for model, distance in rows]

        # Update last_accessed on returned memories for staleness tracking
        if results:
            now = datetime.now(tz=UTC)
            memory_ids = [model.id for model, _ in results]
            await self.session.execute(
                update(ConnectorMemoryModel)
                .where(ConnectorMemoryModel.id.in_(memory_ids))
                .values(last_accessed=now)
            )
            await self.session.flush()

            logger.info(
                "memory_search_completed",
                connector_id=connector_id,
                results_count=len(results),
                score_threshold=score_threshold,
            )

        return results

    async def count_memories(self, connector_id: str, tenant_id: str) -> int:
        """
        Count memories for a connector with tenant isolation.

        Args:
            connector_id: Connector scope
            tenant_id: Tenant scope for isolation

        Returns:
            Count of memories
        """
        try:
            cid = uuid.UUID(connector_id)
        except ValueError:
            return 0

        result = await self.session.execute(
            select(func.count())
            .select_from(ConnectorMemoryModel)
            .where(
                and_(
                    ConnectorMemoryModel.connector_id == cid,
                    ConnectorMemoryModel.tenant_id == tenant_id,
                )
            )
        )
        return result.scalar_one()

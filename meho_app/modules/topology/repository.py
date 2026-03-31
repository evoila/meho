# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Repository for topology database operations.

Handles CRUD and graph traversal for entities, relationships, and SAME_AS.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from meho_app.core.otel import get_logger

from .models import (
    TopologyEmbeddingModel,
    TopologyEntityModel,
    TopologyRelationshipModel,
    TopologySameAsModel,
    TopologySameAsSuggestionModel,
)
from .schemas import (
    SameAsSuggestionCreate,
    TopologyChainItem,
    TopologyEntityCreate,
)

logger = get_logger(__name__)


class TopologyRepository:
    """Repository for topology database operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    # =========================================================================
    # Entity CRUD
    # =========================================================================

    async def create_entity(
        self,
        entity: TopologyEntityCreate,
        tenant_id: str,
    ) -> TopologyEntityModel:
        """Create a new topology entity."""
        # Build canonical_id from scope and name if not provided
        canonical_id = entity.canonical_id
        if not canonical_id:
            if entity.scope:
                # Build canonical_id from scope values + name
                scope_parts = [str(v) for v in entity.scope.values()]
                canonical_id = "/".join([*scope_parts, entity.name])
            else:
                canonical_id = entity.name

        db_entity = TopologyEntityModel(
            name=entity.name,
            entity_type=entity.entity_type,
            connector_type=entity.connector_type or "unknown",
            connector_id=entity.connector_id,
            connector_name=entity.connector_name,
            scope=entity.scope or {},
            canonical_id=canonical_id,
            description=entity.description,
            raw_attributes=entity.raw_attributes or {},
            tenant_id=tenant_id,
            discovered_at=datetime.now(tz=UTC),
        )
        self.session.add(db_entity)
        await self.session.flush()
        await self.session.refresh(db_entity)
        return db_entity

    async def get_entity_by_id(self, entity_id: UUID) -> TopologyEntityModel | None:
        """Get an entity by ID."""
        result = await self.session.execute(
            select(TopologyEntityModel)
            .options(selectinload(TopologyEntityModel.embedding))
            .where(TopologyEntityModel.id == entity_id)
        )
        return result.scalar_one_or_none()

    async def get_entity_by_name(
        self,
        name: str,
        tenant_id: str,
        connector_id: UUID | None = None,
        entity_type: str | None = None,
    ) -> TopologyEntityModel | None:
        """
        Get an entity by name within a tenant.

        For precise identity resolution, provide connector_id and entity_type
        to narrow down to the exact entity (avoids false matches when multiple
        entities share the same name across connectors or types).
        """
        query = select(TopologyEntityModel).where(
            and_(
                TopologyEntityModel.name == name,
                TopologyEntityModel.tenant_id == tenant_id,
                TopologyEntityModel.stale_at.is_(None),  # Exclude stale entities
            )
        )

        if connector_id:
            query = query.where(TopologyEntityModel.connector_id == connector_id)

        if entity_type:
            query = query.where(TopologyEntityModel.entity_type == entity_type)

        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_entity_by_canonical_id(
        self,
        tenant_id: str,
        connector_id: UUID | None,
        entity_type: str,
        canonical_id: str,
    ) -> TopologyEntityModel | None:
        """
        Find entity by its unique canonical identity.

        The unique constraint is: (tenant_id, connector_id, entity_type, canonical_id)

        Args:
            tenant_id: Tenant ID
            connector_id: Connector ID (can be None for external entities)
            entity_type: Entity type (e.g., "Pod", "VM")
            canonical_id: Canonical ID (e.g., "prod/nginx")

        Returns:
            TopologyEntityModel if found, None otherwise
        """
        conditions = [
            TopologyEntityModel.tenant_id == tenant_id,
            TopologyEntityModel.entity_type == entity_type,
            TopologyEntityModel.canonical_id == canonical_id,
            TopologyEntityModel.stale_at.is_(None),
        ]

        # Handle connector_id (may be None for external entities)
        if connector_id is not None:
            conditions.append(TopologyEntityModel.connector_id == connector_id)
        else:
            conditions.append(TopologyEntityModel.connector_id.is_(None))

        result = await self.session.execute(
            select(TopologyEntityModel)
            .options(selectinload(TopologyEntityModel.embedding))
            .where(and_(*conditions))
        )
        return result.scalar_one_or_none()

    async def upsert_entity(
        self,
        entity: TopologyEntityCreate,
        tenant_id: str,
        canonical_id: str,
    ) -> tuple[TopologyEntityModel, bool]:
        """
        Upsert entity by canonical identity.

        If an entity with the same (tenant_id, connector_id, entity_type, canonical_id)
        exists, update it. Otherwise, create a new one.

        Args:
            entity: Entity data to upsert
            tenant_id: Tenant ID
            canonical_id: Pre-computed canonical ID

        Returns:
            Tuple of (entity, is_new) where is_new is True if entity was created
        """
        # Check for existing entity by canonical identity
        existing = await self.get_entity_by_canonical_id(
            tenant_id=tenant_id,
            connector_id=entity.connector_id,
            entity_type=entity.entity_type,
            canonical_id=canonical_id,
        )

        if existing:
            # Update existing entity
            existing.name = entity.name
            existing.description = entity.description
            existing.connector_name = entity.connector_name
            existing.scope = entity.scope or {}
            existing.raw_attributes = entity.raw_attributes or {}
            existing.last_verified_at = datetime.now(tz=UTC)
            # Clear stale flag if it was set
            existing.stale_at = None

            await self.session.flush()
            await self.session.refresh(existing)

            logger.debug(
                f"Updated existing entity: {entity.name} "
                f"(type={entity.entity_type}, canonical_id={canonical_id})"
            )
            return existing, False

        # Create new entity
        db_entity = TopologyEntityModel(
            name=entity.name,
            entity_type=entity.entity_type,
            connector_type=entity.connector_type or "unknown",
            connector_id=entity.connector_id,
            connector_name=entity.connector_name,
            scope=entity.scope or {},
            canonical_id=canonical_id,
            description=entity.description,
            raw_attributes=entity.raw_attributes or {},
            tenant_id=tenant_id,
            discovered_at=datetime.now(tz=UTC),
        )
        self.session.add(db_entity)
        await self.session.flush()
        await self.session.refresh(db_entity)

        logger.info(
            f"Created new entity: {entity.name} "
            f"(type={entity.entity_type}, canonical_id={canonical_id})"
        )
        return db_entity, True

    async def find_entities_by_name_pattern(
        self,
        pattern: str,
        tenant_id: str,
        limit: int = 10,
    ) -> list[TopologyEntityModel]:
        """Find entities matching a name pattern (case-insensitive)."""
        result = await self.session.execute(
            select(TopologyEntityModel)
            .where(
                and_(
                    TopologyEntityModel.tenant_id == tenant_id,
                    TopologyEntityModel.stale_at.is_(None),
                    TopologyEntityModel.name.ilike(f"%{pattern}%"),
                )
            )
            .limit(limit)
        )
        return list(result.scalars().all())

    async def update_entity_verified(self, entity_id: UUID) -> None:
        """Update the last_verified_at timestamp."""
        await self.session.execute(
            update(TopologyEntityModel)
            .where(TopologyEntityModel.id == entity_id)
            .values(last_verified_at=datetime.now(tz=UTC))
        )

    async def mark_entity_stale(self, entity_id: UUID) -> int:
        """Mark an entity as stale. Returns number of relationships affected."""
        now = datetime.now(tz=UTC)

        # Mark entity as stale
        await self.session.execute(
            update(TopologyEntityModel)
            .where(TopologyEntityModel.id == entity_id)
            .values(stale_at=now)
        )

        # Count affected relationships (before deletion or marking)
        rel_result = await self.session.execute(
            select(TopologyRelationshipModel).where(
                or_(
                    TopologyRelationshipModel.from_entity_id == entity_id,
                    TopologyRelationshipModel.to_entity_id == entity_id,
                )
            )
        )
        affected_count = len(list(rel_result.scalars().all()))

        return affected_count

    async def delete_stale_entities(
        self, older_than: datetime, tenant_id: str | None = None
    ) -> int:
        """Delete entities that have been stale for a while.

        Args:
            older_than: Only delete entities stale before this timestamp.
            tenant_id: Scope deletion to this tenant only (security).

        Returns count deleted.
        """
        conditions = [
            TopologyEntityModel.stale_at.isnot(None),
            TopologyEntityModel.stale_at < older_than,
        ]
        if tenant_id:
            conditions.append(TopologyEntityModel.tenant_id == tenant_id)

        result = await self.session.execute(
            delete(TopologyEntityModel).where(and_(*conditions))
        )
        return result.rowcount or 0

    async def get_entities_by_connector(
        self,
        connector_id: UUID,
        tenant_id: str,
        limit: int = 100,
        include_stale: bool = False,
    ) -> list[TopologyEntityModel]:
        """
        Get entities belonging to a specific connector.

        Used for cross-connector correlation to find entities that need
        to be correlated with related connectors.

        Args:
            connector_id: The connector ID
            tenant_id: Tenant ID
            limit: Maximum entities to return
            include_stale: Whether to include stale entities

        Returns:
            List of entities with embeddings loaded
        """
        conditions = [
            TopologyEntityModel.connector_id == connector_id,
            TopologyEntityModel.tenant_id == tenant_id,
        ]

        if not include_stale:
            conditions.append(TopologyEntityModel.stale_at.is_(None))

        result = await self.session.execute(
            select(TopologyEntityModel)
            .options(selectinload(TopologyEntityModel.embedding))
            .where(and_(*conditions))
            .order_by(TopologyEntityModel.discovered_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_entities_by_type(
        self,
        tenant_id: str,
        entity_type: str,
        connector_id: UUID | None = None,
        limit: int = 1000,
    ) -> list[TopologyEntityModel]:
        """
        Get entities by their entity_type within a tenant.

        Used for typed lookups (e.g., finding all K8s Pods for service correlation)
        instead of text search which is fragile.

        Args:
            tenant_id: Tenant ID
            entity_type: Entity type string (e.g., "K8s Pod", "K8s Node", "VM")
            connector_id: Optional connector filter
            limit: Maximum entities to return

        Returns:
            List of matching entities
        """
        conditions = [
            TopologyEntityModel.tenant_id == tenant_id,
            TopologyEntityModel.entity_type == entity_type,
            TopologyEntityModel.stale_at.is_(None),
        ]

        if connector_id:
            conditions.append(TopologyEntityModel.connector_id == connector_id)

        result = await self.session.execute(
            select(TopologyEntityModel)
            .where(and_(*conditions))
            .order_by(TopologyEntityModel.discovered_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    # =========================================================================
    # Embedding Operations
    # =========================================================================

    async def store_embedding(
        self,
        entity_id: UUID,
        embedding: list[float],
    ) -> TopologyEmbeddingModel:
        """Store or update an embedding for an entity."""
        # Check if exists
        existing = await self.session.execute(
            select(TopologyEmbeddingModel).where(TopologyEmbeddingModel.entity_id == entity_id)
        )
        db_embedding = existing.scalar_one_or_none()

        if db_embedding:
            db_embedding.embedding = embedding
        else:
            db_embedding = TopologyEmbeddingModel(
                entity_id=entity_id,
                embedding=embedding,
            )
            self.session.add(db_embedding)

        await self.session.flush()
        return db_embedding

    async def find_similar_entities(
        self,
        embedding: list[float],
        tenant_id: str,
        exclude_entity_id: UUID | None = None,
        exclude_connector_id: UUID | None = None,
        filter_connector_id: UUID | None = None,
        limit: int = 5,
        min_similarity: float = 0.7,
    ) -> list[tuple[TopologyEntityModel, float]]:
        """
        Find entities with similar embeddings.

        Used for cross-connector correlation discovery.

        Args:
            embedding: Query embedding vector
            tenant_id: Tenant ID
            exclude_entity_id: Entity to exclude (usually the source entity)
            exclude_connector_id: Connector to exclude (for cross-connector only search)
            filter_connector_id: Only search within this specific connector
            limit: Maximum results
            min_similarity: Minimum similarity threshold (0-1)

        Returns:
            List of (entity, similarity) tuples sorted by similarity descending
        """
        # Build the query with vector similarity using SQLAlchemy ORM
        # Cosine distance: 0 = identical, 2 = opposite
        # Similarity = 1 - (distance / 2), so max_distance = 2 * (1 - min_similarity)
        max_distance = 2 * (1 - min_similarity)

        # Use ORM's cosine_distance method (properly handles vector binding)
        distance_expr = TopologyEmbeddingModel.embedding.cosine_distance(embedding)

        # Build conditions
        conditions = [
            TopologyEntityModel.tenant_id == tenant_id,
            TopologyEntityModel.stale_at.is_(None),
            distance_expr < max_distance,
        ]

        if exclude_entity_id:
            conditions.append(TopologyEntityModel.id != exclude_entity_id)

        if exclude_connector_id:
            conditions.append(TopologyEntityModel.connector_id != exclude_connector_id)

        if filter_connector_id:
            # Only search within this specific connector
            conditions.append(TopologyEntityModel.connector_id == filter_connector_id)

        query = (
            select(TopologyEntityModel, distance_expr.label("distance"))
            .join(
                TopologyEmbeddingModel, TopologyEmbeddingModel.entity_id == TopologyEntityModel.id
            )
            .where(and_(*conditions))
            .order_by(distance_expr)
            .limit(limit)
        )

        result = await self.session.execute(query)
        rows = result.all()

        entities_with_similarity = []
        for entity, distance in rows:
            # Convert distance to similarity: similarity = 1 - (distance / 2)
            similarity = 1 - (distance / 2)
            entities_with_similarity.append((entity, similarity))

        return entities_with_similarity

    async def find_cross_connector_similar_pairs(
        self,
        tenant_id: str,
        min_similarity: float,
        limit: int,
    ) -> list[tuple[UUID, UUID, float]]:
        """
        Find all cross-connector entity pairs with high embedding similarity.

        Unlike find_similar_entities() which searches FROM a single entity,
        this method scans ALL entity pairs across different connectors to find
        potential SAME_AS candidates.

        The query uses pgvector's cosine distance operator (<=>):
        - Compares embeddings between entities from DIFFERENT connectors
        - Returns pairs sorted by similarity (highest first)
        - Only returns each pair once (entity_a_id < entity_b_id)

        Args:
            tenant_id: Tenant ID to scope the search
            min_similarity: Minimum similarity threshold (0-1)
            limit: Maximum pairs to return

        Returns:
            List of (entity_a_id, entity_b_id, similarity) tuples
            sorted by similarity descending

        Example:
            pairs = await repo.find_cross_connector_similar_pairs(
                tenant_id="tenant-123",
                min_similarity=0.70,
                limit=100,
            )
            # Returns: [(uuid_a, uuid_b, 0.85), (uuid_c, uuid_d, 0.78), ...]
        """
        from sqlalchemy import text

        # pgvector cosine distance: 0 = identical, 2 = opposite
        # Similarity = 1 - (distance / 2)
        # So min_similarity of 0.7 means max_distance of 0.6
        max_distance = 2 * (1 - min_similarity)

        # Use raw SQL for efficient cross-join with pgvector
        # The query:
        # 1. Joins embeddings (a) with embeddings (b) where a.entity_id < b.entity_id (avoid duplicates)
        # 2. Joins both to their entities to check tenant_id and connector_id
        # 3. Filters for different connectors (cross-connector only)
        # 4. Filters for non-stale entities
        # 5. Filters by similarity threshold
        # 6. Orders by similarity descending
        query = text("""
            SELECT
                ea.id as entity_a_id,
                eb.id as entity_b_id,
                1 - (a.embedding <=> b.embedding) / 2 as similarity
            FROM topology_embeddings a
            JOIN topology_embeddings b ON a.entity_id < b.entity_id
            JOIN topology_entities ea ON a.entity_id = ea.id
            JOIN topology_entities eb ON b.entity_id = eb.id
            WHERE ea.tenant_id = :tenant_id
              AND eb.tenant_id = :tenant_id
              AND ea.connector_id IS NOT NULL
              AND eb.connector_id IS NOT NULL
              AND ea.connector_id != eb.connector_id
              AND ea.stale_at IS NULL
              AND eb.stale_at IS NULL
              AND (a.embedding <=> b.embedding) < :max_distance
            ORDER BY (a.embedding <=> b.embedding) ASC
            LIMIT :limit
        """)

        result = await self.session.execute(
            query,
            {
                "tenant_id": tenant_id,
                "max_distance": max_distance,
                "limit": limit,
            },
        )

        pairs = []
        for row in result:
            pairs.append((row.entity_a_id, row.entity_b_id, float(row.similarity)))

        return pairs

    # =========================================================================
    # Relationship CRUD
    # =========================================================================

    async def create_relationship(
        self,
        from_entity_id: UUID,
        to_entity_id: UUID,
        relationship_type: str,
    ) -> TopologyRelationshipModel:
        """Create a new relationship between entities."""
        db_rel = TopologyRelationshipModel(
            from_entity_id=from_entity_id,
            to_entity_id=to_entity_id,
            relationship_type=relationship_type,
            discovered_at=datetime.now(tz=UTC),
        )
        self.session.add(db_rel)
        await self.session.flush()
        return db_rel

    async def get_relationship(
        self,
        from_entity_id: UUID,
        to_entity_id: UUID,
        relationship_type: str,
    ) -> TopologyRelationshipModel | None:
        """Get a specific relationship if it exists."""
        result = await self.session.execute(
            select(TopologyRelationshipModel).where(
                TopologyRelationshipModel.from_entity_id == from_entity_id,
                TopologyRelationshipModel.to_entity_id == to_entity_id,
                TopologyRelationshipModel.relationship_type == relationship_type,
            )
        )
        return result.scalar_one_or_none()

    async def get_relationships_from(
        self,
        entity_id: UUID,
    ) -> list[TopologyRelationshipModel]:
        """Get all relationships originating from an entity."""
        result = await self.session.execute(
            select(TopologyRelationshipModel)
            .options(selectinload(TopologyRelationshipModel.to_entity))
            .where(TopologyRelationshipModel.from_entity_id == entity_id)
        )
        return list(result.scalars().all())

    async def get_relationships_to(
        self,
        entity_id: UUID,
    ) -> list[TopologyRelationshipModel]:
        """Get all relationships pointing to an entity."""
        result = await self.session.execute(
            select(TopologyRelationshipModel)
            .options(selectinload(TopologyRelationshipModel.from_entity))
            .where(TopologyRelationshipModel.to_entity_id == entity_id)
        )
        return list(result.scalars().all())

    # =========================================================================
    # SAME_AS CRUD
    # =========================================================================

    async def create_same_as(
        self,
        entity_a_id: UUID,
        entity_b_id: UUID,
        similarity_score: float,
        verified_via: list[str],
        tenant_id: str,
    ) -> TopologySameAsModel:
        """
        Create a SAME_AS relationship between entities.

        The tenant_id is required and must match both entity_a and entity_b's
        tenant_id. This invariant is enforced at the service layer.
        """
        db_same_as = TopologySameAsModel(
            entity_a_id=entity_a_id,
            entity_b_id=entity_b_id,
            tenant_id=tenant_id,
            similarity_score=similarity_score,
            verified_via=verified_via,
            discovered_at=datetime.now(tz=UTC),
        )
        self.session.add(db_same_as)
        await self.session.flush()
        return db_same_as

    async def get_same_as_for_entity(
        self,
        entity_id: UUID,
        tenant_id: str | None = None,
    ) -> list[TopologySameAsModel]:
        """
        Get all SAME_AS relationships for an entity.

        When tenant_id is provided, only returns SAME_AS rows belonging to
        that tenant -- preventing cross-tenant entity leakage during traversal.
        """
        conditions = [
            or_(
                TopologySameAsModel.entity_a_id == entity_id,
                TopologySameAsModel.entity_b_id == entity_id,
            )
        ]

        if tenant_id:
            conditions.append(TopologySameAsModel.tenant_id == tenant_id)

        result = await self.session.execute(
            select(TopologySameAsModel)
            .options(
                selectinload(TopologySameAsModel.entity_a),
                selectinload(TopologySameAsModel.entity_b),
            )
            .where(and_(*conditions))
        )
        return list(result.scalars().all())

    async def get_same_as_entities(
        self,
        entity_id: UUID,
        tenant_id: str | None = None,
    ) -> list[tuple[TopologyEntityModel, list[str]]]:
        """
        Get all entities confirmed as SAME_AS this entity.

        Unlike get_same_as_for_entity() which returns relationship records,
        this returns the actual correlated entities with their verification info.

        Args:
            entity_id: The entity to find correlations for
            tenant_id: Optional tenant ID for tenant-scoped filtering

        Returns:
            List of (entity, verified_via) tuples for each SAME_AS entity
        """
        # Get all SAME_AS relationships for this entity
        same_as_rels = await self.get_same_as_for_entity(entity_id, tenant_id=tenant_id)

        entities_with_verification: list[tuple[TopologyEntityModel, list[str]]] = []

        for same_as in same_as_rels:
            # Get the OTHER entity (not the one we queried for)
            if same_as.entity_a_id == entity_id:
                other_entity = same_as.entity_b
            else:
                other_entity = same_as.entity_a

            # Skip if entity is stale or not loaded
            if other_entity and other_entity.stale_at is None:
                entities_with_verification.append((other_entity, same_as.verified_via or []))

        return entities_with_verification

    # =========================================================================
    # Graph Traversal
    # =========================================================================

    async def traverse_topology(
        self,
        start_entity_id: UUID,
        tenant_id: str,
        max_depth: int = 10,
        cross_connectors: bool = True,
    ) -> list[TopologyChainItem]:
        """
        Traverse the topology graph from a starting entity.

        Returns a list of chain items representing the path through the topology.
        Optionally follows SAME_AS relationships to cross connector boundaries.

        SECURITY: All entities and SAME_AS edges are filtered by tenant_id.
        No entity from another tenant will appear in traversal results.

        Args:
            start_entity_id: Entity to start traversal from
            tenant_id: Required tenant ID -- all traversed entities must belong to this tenant
            max_depth: Maximum traversal depth
            cross_connectors: Whether to follow SAME_AS edges across connectors
        """
        chain: list[TopologyChainItem] = []
        visited: set[UUID] = set()

        async def _traverse(entity_id: UUID, depth: int, prev_relationship: str | None = None):
            if depth > max_depth or entity_id in visited:
                return

            visited.add(entity_id)

            # Get entity
            entity = await self.get_entity_by_id(entity_id)
            if not entity or entity.stale_at is not None:
                return

            # TENANT SAFETY: Skip entities that don't belong to this tenant
            if entity.tenant_id != tenant_id:
                return

            # Add to chain
            chain.append(
                TopologyChainItem(
                    depth=depth,
                    entity=entity.name,
                    entity_type=entity.entity_type,
                    connector=None,  # Would need connector lookup
                    connector_id=entity.connector_id,
                    relationship=prev_relationship,
                )
            )

            # Follow direct relationships
            relationships = await self.get_relationships_from(entity_id)
            for rel in relationships:
                if rel.to_entity and rel.to_entity.stale_at is None:
                    await _traverse(rel.to_entity_id, depth + 1, rel.relationship_type)

            # Follow SAME_AS relationships if enabled (tenant-filtered)
            if cross_connectors:
                same_as_rels = await self.get_same_as_for_entity(entity_id, tenant_id=tenant_id)
                for same_as in same_as_rels:
                    # Get the other entity
                    other_id = (
                        same_as.entity_b_id
                        if same_as.entity_a_id == entity_id
                        else same_as.entity_a_id
                    )
                    await _traverse(other_id, depth + 1, "same_as")

        await _traverse(start_entity_id, 0)
        return chain

    async def get_all_entities_for_tenant(
        self,
        tenant_id: str,
        include_stale: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[TopologyEntityModel], int]:
        """Get all entities for a tenant with pagination."""
        query = select(TopologyEntityModel).where(TopologyEntityModel.tenant_id == tenant_id)

        if not include_stale:
            query = query.where(TopologyEntityModel.stale_at.is_(None))

        # Count total
        count_result = await self.session.execute(
            select(TopologyEntityModel.id).where(TopologyEntityModel.tenant_id == tenant_id)
        )
        total = len(list(count_result.scalars().all()))

        # Get page
        query = query.offset(offset).limit(limit)
        result = await self.session.execute(query)
        entities = list(result.scalars().all())

        return entities, total

    # =========================================================================
    # Connector Cascade Operations
    # =========================================================================

    async def delete_entities_by_connector(self, connector_id: UUID) -> int:
        """
        Delete all topology entities for a given connector.

        Called when a connector is deleted to prevent orphaned entities.
        The ON DELETE CASCADE foreign keys will automatically clean up:
        - topology_embeddings
        - topology_relationships
        - topology_same_as

        Returns the number of entities deleted.
        """
        result = await self.session.execute(
            delete(TopologyEntityModel).where(TopologyEntityModel.connector_id == connector_id)
        )
        return result.rowcount or 0

    async def delete_orphaned_entities(self, valid_connector_ids: list[UUID]) -> int:
        """
        Delete topology entities whose connector_id is not in the list of valid connectors.

        Used for cleanup of entities whose connectors have been deleted.
        Excludes entities with NULL connector_id (external entities like URLs).

        Returns the number of entities deleted.
        """
        if not valid_connector_ids:
            # If no valid connectors, delete all entities that have a connector_id
            result = await self.session.execute(
                delete(TopologyEntityModel).where(TopologyEntityModel.connector_id.isnot(None))
            )
        else:
            result = await self.session.execute(
                delete(TopologyEntityModel).where(
                    and_(
                        TopologyEntityModel.connector_id.isnot(None),
                        TopologyEntityModel.connector_id.not_in(valid_connector_ids),
                    )
                )
            )
        return result.rowcount or 0

    # =========================================================================
    # SAME_AS Suggestion CRUD (Phase 2 Correlation)
    # =========================================================================

    async def create_suggestion(
        self,
        suggestion: SameAsSuggestionCreate,
        tenant_id: str,
    ) -> TopologySameAsSuggestionModel:
        """
        Create a new SAME_AS suggestion.

        Used when automatic correlation detects a potential match between
        an entity and a connector target.
        """
        db_suggestion = TopologySameAsSuggestionModel(
            entity_a_id=suggestion.entity_a_id,
            entity_b_id=suggestion.entity_b_id,
            confidence=suggestion.confidence,
            match_type=suggestion.match_type,
            match_details=suggestion.match_details,
            status="pending",
            tenant_id=tenant_id,
            suggested_at=datetime.now(tz=UTC),
        )
        self.session.add(db_suggestion)
        await self.session.flush()
        await self.session.refresh(db_suggestion)
        return db_suggestion

    async def get_suggestion_by_id(
        self,
        suggestion_id: UUID,
    ) -> TopologySameAsSuggestionModel | None:
        """Get a suggestion by ID with entities loaded."""
        result = await self.session.execute(
            select(TopologySameAsSuggestionModel)
            .options(
                selectinload(TopologySameAsSuggestionModel.entity_a),
                selectinload(TopologySameAsSuggestionModel.entity_b),
            )
            .where(TopologySameAsSuggestionModel.id == suggestion_id)
        )
        return result.scalar_one_or_none()

    async def get_pending_suggestions(
        self,
        tenant_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[TopologySameAsSuggestionModel], int]:
        """
        Get all pending suggestions for a tenant.

        Returns suggestions with entities loaded for display.
        """
        # Count total pending
        count_result = await self.session.execute(
            select(TopologySameAsSuggestionModel.id).where(
                and_(
                    TopologySameAsSuggestionModel.tenant_id == tenant_id,
                    TopologySameAsSuggestionModel.status == "pending",
                )
            )
        )
        total = len(list(count_result.scalars().all()))

        # Get page with entities
        result = await self.session.execute(
            select(TopologySameAsSuggestionModel)
            .options(
                selectinload(TopologySameAsSuggestionModel.entity_a),
                selectinload(TopologySameAsSuggestionModel.entity_b),
            )
            .where(
                and_(
                    TopologySameAsSuggestionModel.tenant_id == tenant_id,
                    TopologySameAsSuggestionModel.status == "pending",
                )
            )
            .order_by(TopologySameAsSuggestionModel.suggested_at.desc())
            .offset(offset)
            .limit(limit)
        )
        suggestions = list(result.scalars().all())

        return suggestions, total

    async def get_existing_suggestion(
        self,
        entity_a_id: UUID,
        entity_b_id: UUID,
    ) -> TopologySameAsSuggestionModel | None:
        """
        Check if a suggestion already exists between two entities.

        Checks both directions (a-b and b-a) since order doesn't matter.
        """
        result = await self.session.execute(
            select(TopologySameAsSuggestionModel).where(
                or_(
                    and_(
                        TopologySameAsSuggestionModel.entity_a_id == entity_a_id,
                        TopologySameAsSuggestionModel.entity_b_id == entity_b_id,
                    ),
                    and_(
                        TopologySameAsSuggestionModel.entity_a_id == entity_b_id,
                        TopologySameAsSuggestionModel.entity_b_id == entity_a_id,
                    ),
                )
            )
        )
        return result.scalar_one_or_none()

    async def approve_suggestion(
        self,
        suggestion_id: UUID,
        user_id: str,
    ) -> TopologySameAsModel | None:
        """
        Approve a suggestion and create the SAME_AS relationship.

        Returns the created SAME_AS model, or None if suggestion not found.
        """
        suggestion = await self.get_suggestion_by_id(suggestion_id)
        if not suggestion or suggestion.status != "pending":
            return None

        # Update suggestion status
        await self.session.execute(
            update(TopologySameAsSuggestionModel)
            .where(TopologySameAsSuggestionModel.id == suggestion_id)
            .values(
                status="approved",
                resolved_at=datetime.now(tz=UTC),
                resolved_by=user_id,
            )
        )

        # Create SAME_AS relationship (tenant_id from suggestion)
        same_as = await self.create_same_as(
            entity_a_id=suggestion.entity_a_id,
            entity_b_id=suggestion.entity_b_id,
            similarity_score=suggestion.confidence,
            verified_via=[suggestion.match_type, "user_approved"],
            tenant_id=suggestion.tenant_id,
        )

        return same_as

    async def reject_suggestion(
        self,
        suggestion_id: UUID,
        user_id: str,
    ) -> bool:
        """
        Reject a suggestion.

        Returns True if rejected, False if not found or already resolved.
        """
        suggestion = await self.get_suggestion_by_id(suggestion_id)
        if not suggestion or suggestion.status != "pending":
            return False

        await self.session.execute(
            update(TopologySameAsSuggestionModel)
            .where(TopologySameAsSuggestionModel.id == suggestion_id)
            .values(
                status="rejected",
                resolved_at=datetime.now(tz=UTC),
                resolved_by=user_id,
            )
        )

        return True

    async def check_existing_same_as(
        self,
        entity_a_id: UUID,
        entity_b_id: UUID,
    ) -> TopologySameAsModel | None:
        """
        Check if a SAME_AS relationship already exists between two entities.

        Checks both directions (a-b and b-a) since order doesn't matter.
        """
        result = await self.session.execute(
            select(TopologySameAsModel).where(
                or_(
                    and_(
                        TopologySameAsModel.entity_a_id == entity_a_id,
                        TopologySameAsModel.entity_b_id == entity_b_id,
                    ),
                    and_(
                        TopologySameAsModel.entity_a_id == entity_b_id,
                        TopologySameAsModel.entity_b_id == entity_a_id,
                    ),
                )
            )
        )
        return result.scalar_one_or_none()

    # =========================================================================
    # LLM Verification (Phase 3)
    # =========================================================================

    async def update_suggestion_verification(
        self,
        suggestion_id: UUID,
        llm_result: dict | None,
    ) -> None:
        """
        Store LLM verification result on a suggestion.

        Called after running LLM verification to persist the result,
        regardless of whether it led to approval/rejection.

        Args:
            suggestion_id: ID of the suggestion
            llm_result: LLM correlation result dict (or None if verification failed)
        """
        await self.session.execute(
            update(TopologySameAsSuggestionModel)
            .where(TopologySameAsSuggestionModel.id == suggestion_id)
            .values(
                llm_verification_attempted=True,
                llm_verification_result=llm_result,
            )
        )
        await self.session.flush()

    async def get_suggestions_needing_verification(
        self,
        tenant_id: str,
        min_confidence: float = 0.70,
        max_confidence: float = 0.89,
        limit: int = 100,
    ) -> list[TopologySameAsSuggestionModel]:
        """
        Get pending suggestions in the LLM verification confidence range.

        Returns suggestions that:
        - Have status 'pending'
        - Have confidence between min and max (inclusive)
        - Have NOT been LLM-verified yet

        Args:
            tenant_id: Tenant ID
            min_confidence: Minimum confidence for LLM verification (default 0.70)
            max_confidence: Maximum confidence for LLM verification (default 0.89)
            limit: Maximum suggestions to return

        Returns:
            List of suggestions needing LLM verification
        """
        result = await self.session.execute(
            select(TopologySameAsSuggestionModel)
            .options(
                selectinload(TopologySameAsSuggestionModel.entity_a),
                selectinload(TopologySameAsSuggestionModel.entity_b),
            )
            .where(
                and_(
                    TopologySameAsSuggestionModel.tenant_id == tenant_id,
                    TopologySameAsSuggestionModel.status == "pending",
                    TopologySameAsSuggestionModel.confidence >= min_confidence,
                    TopologySameAsSuggestionModel.confidence <= max_confidence,
                    TopologySameAsSuggestionModel.llm_verification_attempted == False,  # noqa: E712
                )
            )
            .order_by(TopologySameAsSuggestionModel.suggested_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    # =========================================================================
    # Deterministic Resolution Support (Phase 15)
    # =========================================================================

    async def delete_same_as(
        self,
        entity_a_id: UUID,
        entity_b_id: UUID,
    ) -> bool:
        """
        Delete a SAME_AS relationship between two entities (in either direction).

        Returns True if a relationship was deleted, False if none existed.
        """
        result = await self.session.execute(
            delete(TopologySameAsModel).where(
                or_(
                    and_(
                        TopologySameAsModel.entity_a_id == entity_a_id,
                        TopologySameAsModel.entity_b_id == entity_b_id,
                    ),
                    and_(
                        TopologySameAsModel.entity_a_id == entity_b_id,
                        TopologySameAsModel.entity_b_id == entity_a_id,
                    ),
                )
            )
        )
        deleted = result.rowcount > 0
        if deleted:
            logger.info(f"Removed stale SAME_AS: {entity_a_id} <-> {entity_b_id}")
        return deleted

    async def get_deterministic_same_as_for_entity(
        self,
        entity_id: UUID,
    ) -> list[TopologySameAsModel]:
        """
        Get all SAME_AS relationships for an entity that were created via
        deterministic resolution (verified_via contains 'deterministic_resolution').

        Used to find stale relationships that need re-validation after entity updates.
        """
        result = await self.session.execute(
            select(TopologySameAsModel).where(
                and_(
                    or_(
                        TopologySameAsModel.entity_a_id == entity_id,
                        TopologySameAsModel.entity_b_id == entity_id,
                    ),
                    TopologySameAsModel.verified_via.any("deterministic_resolution"),
                )
            )
        )
        return list(result.scalars().all())

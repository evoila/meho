# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Repository for knowledge chunk CRUD operations.
"""

# mypy: disable-error-code="arg-type,assignment,attr-defined,no-any-return"
import uuid

from sqlalchemy import Boolean, and_, cast, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.modules.knowledge.models import KnowledgeChunkModel
from meho_app.modules.knowledge.schemas import (
    KnowledgeChunk,
    KnowledgeChunkCreate,
    KnowledgeChunkFilter,
    ScopeFilter,
)

logger = get_logger(__name__)


class KnowledgeRepository:
    """Repository for knowledge chunk operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_chunk(
        self, chunk: KnowledgeChunkCreate, embedding: list[float] | None = None
    ) -> KnowledgeChunk:
        """
        Create a new knowledge chunk.

        Args:
            chunk: Chunk data without ID
            embedding: Optional vector embedding (1024 dimensions for Voyage AI)

        Returns:
            Created chunk with ID and timestamps
        """
        # Convert Pydantic model to dict, preserving datetime objects
        # Note: model_dump() keeps datetime objects (unlike model_dump_json which serializes to ISO strings)
        chunk_data = chunk.model_dump()

        # Manually convert enum to string value (SQLAlchemy needs this)
        if "knowledge_type" in chunk_data and hasattr(chunk_data["knowledge_type"], "value"):
            chunk_data["knowledge_type"] = chunk_data["knowledge_type"].value

        # Log what we're about to insert
        search_metadata_val = chunk_data.get("search_metadata")
        logger.debug(
            f"📝 Creating chunk: keys={list(chunk_data.keys())}, "
            f"has_search_metadata={'search_metadata' in chunk_data}, "
            f"search_metadata_type={type(search_metadata_val).__name__}"
        )

        chunk_id = uuid.uuid4()

        # Add embedding if provided
        if embedding is not None:
            chunk_data["embedding"] = embedding

        db_chunk = KnowledgeChunkModel(id=chunk_id, **chunk_data)

        # Log what was set on the model BEFORE commit
        logger.debug(
            f"📝 Chunk before flush: id={chunk_id}, "
            f"search_metadata_type={type(db_chunk.search_metadata).__name__}"
        )

        self.session.add(db_chunk)
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
        await self.session.refresh(db_chunk)

        # Log what was actually saved AFTER commit
        logger.debug(
            f"✅ Chunk saved: id={chunk_id}, "
            f"search_metadata_is_none={db_chunk.search_metadata is None}"
        )

        # Convert UUID to string for Pydantic
        return self._model_to_schema(db_chunk)

    async def get_chunk(self, chunk_id: str) -> KnowledgeChunk | None:
        """
        Get a chunk by ID (NO ACL CHECK - use get_chunks_with_acl for security).

        Args:
            chunk_id: UUID string

        Returns:
            Chunk if found, None otherwise
        """
        try:
            chunk_uuid = uuid.UUID(chunk_id)
        except ValueError:
            return None

        result = await self.session.execute(
            select(KnowledgeChunkModel).where(KnowledgeChunkModel.id == chunk_uuid)
        )
        db_chunk = result.scalar_one_or_none()

        if db_chunk is None:
            return None

        return self._model_to_schema(db_chunk)

    async def get_chunks_with_acl(
        self, chunk_ids: list[str], user_context: UserContext
    ) -> list[KnowledgeChunk]:
        """
        Get multiple chunks by ID with ACL enforcement.

        SECURITY: Returns only chunks the user has permission to access based on:
        - Tenant membership (chunk.tenant_id == user.tenant_id OR chunk is global)
        - Connector scoping (applied at query level, not ACL)
        - User ownership (chunk.user_id == user.user_id)
        - Role permissions (user has required role in chunk.roles)
        - Group permissions (user has required group in chunk.groups)

        Args:
            chunk_ids: List of chunk UUID strings
            user_context: User context for ACL filtering

        Returns:
            List of chunks user can access (may be fewer than requested if ACL blocks some)
        """
        if not chunk_ids:
            return []

        # Convert to UUIDs
        chunk_uuids = []
        for chunk_id in chunk_ids:
            try:
                chunk_uuids.append(uuid.UUID(chunk_id))
            except ValueError:
                continue  # Skip invalid UUIDs

        if not chunk_uuids:
            return []

        # Build query with ID filter
        stmt = select(KnowledgeChunkModel).where(KnowledgeChunkModel.id.in_(chunk_uuids))

        # Apply ACL filter (same logic as search_by_embedding)
        acl_conditions = self._build_acl_filter(user_context)
        for condition in acl_conditions:
            stmt = stmt.where(condition)

        result = await self.session.execute(stmt)
        chunk_models = result.scalars().all()

        return [self._model_to_schema(chunk_model) for chunk_model in chunk_models]

    async def list_chunks(self, filter_params: KnowledgeChunkFilter) -> list[KnowledgeChunk]:
        """
        List chunks with filters.

        Args:
            filter_params: Filter criteria

        Returns:
            List of matching chunks
        """
        query = select(KnowledgeChunkModel)

        conditions = []

        # Filter by tenant
        if filter_params.tenant_id is not None:
            conditions.append(KnowledgeChunkModel.tenant_id == filter_params.tenant_id)

        # Filter by connector (replaces system_id filter)
        if getattr(filter_params, "connector_id", None) is not None:
            import uuid as _uuid

            try:
                cid = _uuid.UUID(filter_params.connector_id)
                conditions.append(KnowledgeChunkModel.connector_id == cid)
            except ValueError:
                pass

        # Filter by user
        if filter_params.user_id is not None:
            conditions.append(KnowledgeChunkModel.user_id == filter_params.user_id)

        # Filter by source_uri (for document deletion, etc.)
        if filter_params.source_uri is not None:
            conditions.append(KnowledgeChunkModel.source_uri == filter_params.source_uri)

        # Filter by source_uri prefix (TASK-75: for SOAP connector cleanup)
        if filter_params.source_uri_prefix is not None:
            conditions.append(
                KnowledgeChunkModel.source_uri.like(f"{filter_params.source_uri_prefix}%")
            )

        # Filter by tags (AND logic - chunk must have all specified tags)
        if filter_params.tags:
            for tag in filter_params.tags:
                # JSONB contains check
                conditions.append(KnowledgeChunkModel.tags.contains([tag]))

        # Apply all conditions
        if conditions:
            query = query.where(and_(*conditions))

        # Pagination and ordering
        query = query.order_by(KnowledgeChunkModel.created_at.desc())
        query = query.limit(filter_params.limit).offset(filter_params.offset)

        result = await self.session.execute(query)
        db_chunks = result.scalars().all()

        return [self._model_to_schema(chunk) for chunk in db_chunks]

    async def search_by_embedding(
        self,
        query_embedding: list[float],
        user_context: "UserContext",
        top_k: int = 10,
        score_threshold: float = 0.7,
        metadata_filters: dict | None = None,
        scope_filter: ScopeFilter | None = None,
    ) -> list[tuple[KnowledgeChunk, float]]:
        """
        Semantic search using pgvector cosine similarity.

        Args:
            query_embedding: Query vector (1024 dimensions for Voyage AI)
            user_context: User context for ACL filtering
            top_k: Maximum results to return
            score_threshold: Minimum similarity score (0-1)
            metadata_filters: Optional metadata filters (resource_type, content_type, etc.)
            scope_filter: Optional scope filter for three-tier knowledge queries

        Returns:
            List of (chunk, similarity_score) tuples, ordered by similarity
        """
        # Build ACL filter
        acl_conditions = self._build_acl_filter(user_context)

        # Build scope filter (Phase 65: three-tier knowledge)
        scope_conditions = self._build_scope_filter(scope_filter)

        # Build metadata filters
        metadata_conditions = []
        if metadata_filters:
            for key, value in metadata_filters.items():
                if isinstance(value, bool):
                    # Boolean fields: has_json_example, has_code_example
                    metadata_conditions.append(
                        cast(KnowledgeChunkModel.search_metadata[key].astext, Boolean) == value
                    )
                else:
                    # String fields: resource_type, content_type, chapter, etc.
                    metadata_conditions.append(
                        KnowledgeChunkModel.search_metadata[key].astext == str(value)
                    )

        # Calculate cosine distance (0 = identical, 2 = opposite)
        # Convert score_threshold (0-1) to max distance
        # similarity = 1 - (distance / 2), so distance = 2 * (1 - similarity)
        max_distance = 2 * (1 - score_threshold)

        # Build query
        distance_expr = KnowledgeChunkModel.embedding.cosine_distance(query_embedding)

        query = (
            select(KnowledgeChunkModel, distance_expr.label("distance"))
            .where(and_(*acl_conditions))
            .where(distance_expr < max_distance)
        )

        # Apply scope filter (Phase 65)
        if scope_conditions:
            query = query.where(and_(*scope_conditions))

        if metadata_conditions:
            query = query.where(and_(*metadata_conditions))

        query = query.order_by("distance").limit(top_k)

        # Execute query
        result = await self.session.execute(query)
        rows = result.all()

        # Log search results using structured logging
        logger.info(
            "vector_search_completed",
            top_k=top_k,
            score_threshold=score_threshold,
            results_count=len(rows),
            has_metadata_filters=metadata_filters is not None,
            tenant_id=user_context.tenant_id,
        )

        # Convert to (chunk, similarity_score) tuples
        chunks_with_scores = []
        for db_chunk, distance in rows:
            # Convert distance to similarity score (0-1 range)
            similarity = 1 - (distance / 2)
            chunk = self._model_to_schema(db_chunk)
            chunks_with_scores.append((chunk, similarity))

        return chunks_with_scores

    def _build_scope_filter(self, scope_filter: ScopeFilter | None) -> list:
        """
        Build scope-aware WHERE clause conditions for three-tier knowledge queries.

        Three modes:
        1. No scope filter (ask mode): returns all scopes for the tenant
        2. Instance-scoped (specialist agent): returns global + matching type + matching instance
        3. Explicit scope_types: returns only requested scopes

        Args:
            scope_filter: Optional scope filter with scope_types, connector_type, connector_id

        Returns:
            List of SQLAlchemy filter conditions (may be empty if no filtering needed)
        """
        if scope_filter is None:
            # Ask mode: no scope restriction, return all knowledge for the tenant
            return []

        conditions = []

        if scope_filter.connector_id and scope_filter.connector_type:
            # Specialist agent context: return global + matching type + matching instance
            import uuid as _uuid

            try:
                cid = _uuid.UUID(scope_filter.connector_id)
            except ValueError:
                cid = None

            scope_parts = [
                # Global knowledge
                KnowledgeChunkModel.scope_type == "global",
                # Type-level knowledge for this connector type
                and_(
                    KnowledgeChunkModel.scope_type == "type",
                    KnowledgeChunkModel.connector_type_scope == scope_filter.connector_type,
                ),
            ]
            if cid is not None:
                # Instance-level knowledge for this specific connector
                scope_parts.append(
                    and_(
                        KnowledgeChunkModel.scope_type == "instance",
                        KnowledgeChunkModel.connector_id == cid,
                    )
                )
            conditions.append(or_(*scope_parts))

        elif scope_filter.scope_types:
            # Explicit scope type filter
            conditions.append(KnowledgeChunkModel.scope_type.in_(scope_filter.scope_types))
            if scope_filter.connector_type:
                conditions.append(
                    or_(
                        KnowledgeChunkModel.connector_type_scope == scope_filter.connector_type,
                        KnowledgeChunkModel.connector_type_scope.is_(None),
                    )
                )
            if scope_filter.connector_id:
                import uuid as _uuid

                try:
                    cid = _uuid.UUID(scope_filter.connector_id)
                    conditions.append(
                        or_(
                            KnowledgeChunkModel.connector_id == cid,
                            KnowledgeChunkModel.connector_id.is_(None),
                        )
                    )
                except ValueError:
                    pass

        return conditions

    def _build_acl_filter(self, user_context: UserContext) -> list:
        """
        Build ACL filter conditions for a query.

        SECURITY LOGIC (Session 39 fix):
        1. Tenant isolation: user can only see global OR their tenant's chunks
        2. Role-based: chunk has no role requirements OR user has at least one required role
        3. Group-based: chunk has no group requirements OR user has at least one required group

        These are AND conditions (all must be true), not OR.
        """
        conditions = []

        # 1. TENANT ISOLATION (highest priority security boundary)
        # User can access: global knowledge (tenant_id=NULL) OR their tenant's knowledge
        tenant_filter = KnowledgeChunkModel.tenant_id.is_(None)  # Global chunks
        if user_context.tenant_id:
            tenant_filter = or_(
                tenant_filter, KnowledgeChunkModel.tenant_id == user_context.tenant_id
            )
        conditions.append(tenant_filter)

        # 2. ROLE-BASED ACCESS CONTROL (within tenant)
        # Chunk is accessible if: it has NO role requirements OR user has at least one required role
        if user_context.tenant_id:  # Only apply role checks within a tenant
            role_conditions = [
                # Chunk has no role restrictions (empty array)
                or_(KnowledgeChunkModel.roles == [], KnowledgeChunkModel.roles.is_(None))
            ]

            # If user has roles, they can access chunks requiring those roles
            if user_context.roles:
                for role in user_context.roles:
                    role_conditions.append(func.jsonb_exists(KnowledgeChunkModel.roles, role))

            # Combine: no requirements OR user has required role
            conditions.append(or_(*role_conditions))

        # 3. GROUP-BASED ACCESS CONTROL (within tenant)
        # Chunk is accessible if: it has NO group requirements OR user has at least one required group
        if user_context.tenant_id:  # Only apply group checks within a tenant
            group_conditions = [
                # Chunk has no group restrictions (empty array)
                or_(KnowledgeChunkModel.groups == [], KnowledgeChunkModel.groups.is_(None))
            ]

            # If user has groups, they can access chunks requiring those groups
            if user_context.groups:
                for group in user_context.groups:
                    group_conditions.append(func.jsonb_exists(KnowledgeChunkModel.groups, group))

            # Combine: no requirements OR user has required group
            conditions.append(or_(*group_conditions))

        # 4. SYSTEM-SCOPED removed — replaced by connector_id scoping at query level
        # (system_id column dropped in 08-01 migration)

        # 5. USER-SCOPED (optional filter - for user-specific knowledge)
        if user_context.user_id:
            user_filter = or_(
                KnowledgeChunkModel.user_id.is_(None),  # Not user-specific
                KnowledgeChunkModel.user_id == user_context.user_id,
            )
            conditions.append(user_filter)

        return conditions

    async def delete_chunk(self, chunk_id: str) -> bool:
        """
        Delete a chunk by ID.

        Args:
            chunk_id: UUID string

        Returns:
            True if deleted, False if not found
        """
        try:
            chunk_uuid = uuid.UUID(chunk_id)
        except ValueError:
            return False

        result = await self.session.execute(
            select(KnowledgeChunkModel).where(KnowledgeChunkModel.id == chunk_uuid)
        )
        db_chunk = result.scalar_one_or_none()

        if db_chunk is None:
            return False

        await self.session.delete(db_chunk)
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
        return True

    async def delete_chunks_batch(self, chunk_ids: list[str]) -> int:
        """
        Delete multiple chunks efficiently in a single query (Session 30).

        Much faster than deleting one-by-one. Embeddings are automatically
        deleted with pgvector (same row).

        Raises on failure; does NOT call rollback (caller owns the transaction).

        Args:
            chunk_ids: List of chunk ID strings

        Returns:
            Number of chunks deleted
        """
        if not chunk_ids:
            return 0

        chunk_uuids = [uuid.UUID(cid) for cid in chunk_ids]

        result = await self.session.execute(
            delete(KnowledgeChunkModel).where(KnowledgeChunkModel.id.in_(chunk_uuids))
        )
        await self.session.flush()

        deleted_count = result.rowcount
        logger.info("batch_deleted_chunks", count=deleted_count, expected=len(chunk_ids))

        return deleted_count

    async def delete_by_source_uri(self, source_uri: str) -> int:
        """Delete all chunks matching a source_uri (e.g. 'document://<id>')."""
        result = await self.session.execute(
            delete(KnowledgeChunkModel).where(
                KnowledgeChunkModel.source_uri == source_uri
            )
        )
        await self.session.flush()
        deleted = result.rowcount
        logger.info("deleted_chunks_by_source_uri", source_uri=source_uri, count=deleted)
        return deleted

    async def count_chunks(self, filter_params: KnowledgeChunkFilter | None = None) -> int:
        """
        Count chunks matching filter.

        Args:
            filter_params: Optional filter criteria

        Returns:
            Count of matching chunks
        """
        from sqlalchemy import func

        query = select(func.count()).select_from(KnowledgeChunkModel)

        if filter_params:
            conditions = []

            if filter_params.tenant_id is not None:
                conditions.append(KnowledgeChunkModel.tenant_id == filter_params.tenant_id)
            if getattr(filter_params, "connector_id", None) is not None:
                import uuid as _uuid

                try:
                    cid = _uuid.UUID(filter_params.connector_id)
                    conditions.append(KnowledgeChunkModel.connector_id == cid)
                except ValueError:
                    pass
            if filter_params.user_id is not None:
                conditions.append(KnowledgeChunkModel.user_id == filter_params.user_id)
            if filter_params.tags:
                for tag in filter_params.tags:
                    conditions.append(KnowledgeChunkModel.tags.contains([tag]))

            if conditions:
                query = query.where(and_(*conditions))

        result = await self.session.execute(query)
        return result.scalar_one()

    def _model_to_schema(self, db_chunk: KnowledgeChunkModel) -> KnowledgeChunk:
        """
        Convert SQLAlchemy model to Pydantic schema.
        Handles UUID to string conversion and enum conversion.
        """
        from meho_app.modules.knowledge.schemas import KnowledgeType

        # Convert knowledge_type string to enum
        knowledge_type = (
            KnowledgeType(db_chunk.knowledge_type)
            if db_chunk.knowledge_type
            else KnowledgeType.DOCUMENTATION
        )

        return KnowledgeChunk(
            id=str(db_chunk.id),
            text=db_chunk.text,
            tenant_id=db_chunk.tenant_id,
            connector_id=str(db_chunk.connector_id) if db_chunk.connector_id else None,
            user_id=db_chunk.user_id,
            roles=db_chunk.roles or [],
            groups=db_chunk.groups or [],
            tags=db_chunk.tags or [],
            source_uri=db_chunk.source_uri,
            # Scope fields (Phase 65)
            scope_type=db_chunk.scope_type or "instance",
            connector_type_scope=db_chunk.connector_type_scope,
            # Lifecycle fields
            expires_at=db_chunk.expires_at,
            knowledge_type=knowledge_type,
            priority=db_chunk.priority or 0,
            # Timestamps
            created_at=db_chunk.created_at,
            updated_at=db_chunk.updated_at,
            # Rich metadata for enhanced retrieval
            search_metadata=db_chunk.search_metadata,
        )

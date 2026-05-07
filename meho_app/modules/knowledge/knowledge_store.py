# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unified knowledge store using PostgreSQL with pgvector.

Provides high-level interface for adding and searching knowledge chunks.
All data (text, metadata, vectors) stored in single PostgreSQL database.

Supports connector-scoped search (specialist agent) and cross-connector search (orchestrator).
"""

# mypy: disable-error-code="misc,valid-type,attr-defined,var-annotated"
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import select

from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.modules.knowledge.embeddings import EmbeddingProvider
from meho_app.modules.knowledge.repository import KnowledgeRepository
from meho_app.modules.knowledge.retrieval_context import (
    build_retrieval_text_from_metadata,
    clean_heading_path,
    extract_filename,
)
from meho_app.modules.knowledge.schemas import (
    KnowledgeChunk,
    KnowledgeChunkCreate,
    KnowledgeType,
)

if TYPE_CHECKING:
    from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService

logger = get_logger(__name__)
RERANK_CANDIDATE_MULTIPLIER = 5
RERANK_CANDIDATE_LIMIT = 100


class KnowledgeStore:
    """Unified interface for knowledge storage and retrieval using PostgreSQL+pgvector"""

    def __init__(
        self,
        repository: KnowledgeRepository,
        embedding_provider: EmbeddingProvider,
        hybrid_search_service: Optional["PostgresFTSHybridService"] = None,
    ) -> None:
        """
        Initialize knowledge store.

        Args:
            repository: PostgreSQL repository (handles both data and vector search with pgvector)
            embedding_provider: Provider for generating embeddings
            hybrid_search_service: Optional compatibility search service for
                semantic ranking plus reranking.

        Note: Everything remains in PostgreSQL - vectors (pgvector), metadata
        (JSONB), and chunk text. Retrieval ranking itself follows the Farseer-style
        semantic ranker + reranker flow.
        """
        self.repository = repository
        self.embedding_provider = embedding_provider
        self.hybrid_search_service = hybrid_search_service

        # Warn if hybrid search is not provided (will use semantic-only search)
        if hybrid_search_service is None:
            logger.warning(
                "KnowledgeStore created without hybrid_search_service. "
                "Search will use semantic embeddings only (no BM25 fusion). "
                "Production should pass a PostgresFTSHybridService."
            )

    async def add_chunk(self, chunk_create: KnowledgeChunkCreate) -> KnowledgeChunk:
        """
        Add a knowledge chunk to PostgreSQL with embedding.

        Args:
            chunk_create: Chunk data without ID

        Returns:
            Created chunk with ID

        Raises:
            ValidationError: If text exceeds embedding model token limit
        """
        # Validate text size for embedding model
        from meho_app.modules.knowledge.text_validation import (
            truncate_text_to_token_limit,
            validate_text_for_embedding,
        )

        is_valid, _error_msg, token_count = validate_text_for_embedding(chunk_create.text)

        if not is_valid:
            logger.warning(f"Text exceeds token limit ({token_count} tokens), truncating...")
            # Auto-truncate instead of failing
            chunk_create.text = truncate_text_to_token_limit(chunk_create.text)
            logger.info("Truncated text to fit within token limit")

        try:
            retrieval_text = build_retrieval_text_from_metadata(
                text=chunk_create.text,
                source_uri=chunk_create.source_uri,
                metadata=chunk_create.search_metadata,
            )
            retrieval_is_valid, _error_msg, retrieval_token_count = validate_text_for_embedding(
                retrieval_text
            )
            if not retrieval_is_valid:
                logger.warning(
                    f"Retrieval text exceeds token limit ({retrieval_token_count} tokens), truncating for embedding only"
                )
                retrieval_text = truncate_text_to_token_limit(retrieval_text)

            # 1. Generate embedding
            embedding = await self.embedding_provider.embed_text(retrieval_text)

            # 2. Create in database with embedding (single atomic operation!)
            chunk = await self.repository.create_chunk(chunk_create, embedding=embedding)

            resource_type = (
                chunk.search_metadata.get("resource_type")
                if isinstance(chunk.search_metadata, dict)
                else None
            )
            logger.info(
                f"📦 Chunk created with embedding: id={chunk.id}, "
                f"has_embedding=True, has_search_metadata={chunk.search_metadata is not None}, "
                f"resource_type={resource_type}"
            )

            return chunk

        except Exception as e:
            # Embedding generation failed - log and re-raise
            # No cleanup needed since PostgreSQL transaction will rollback
            logger.error(f"Failed to create chunk with embedding: {e}")
            raise

    async def search(
        self,
        query: str,
        user_context: UserContext,
        top_k: int = 10,
        score_threshold: float = 0.7,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[KnowledgeChunk]:
        """
        Semantic search over knowledge base with ACL and metadata filtering.

        Args:
            query: Search query text
            user_context: User context for ACL filtering
            top_k: Maximum number of results
            score_threshold: Minimum similarity score (0-1)
            metadata_filters: Optional metadata filters for enhanced retrieval:
                {
                    "resource_type": "roles",
                    "content_type": "example_json",
                    "endpoint_path": "/v1/roles",
                    "chapter": "Roles Management"
                }

        Returns:
            List of matching chunks ordered by relevance
        """
        logger.debug(
            "knowledge_store_search",
            query=query,
            tenant_id=user_context.tenant_id,
            user_id=user_context.user_id,
            top_k=top_k,
            score_threshold=score_threshold,
            metadata_filters=metadata_filters,
        )

        # 1. Generate query embedding
        query_embedding = await self.embedding_provider.embed_text(query)

        # 2. Search PostgreSQL with pgvector (handles ACL + metadata filtering)
        chunks_with_scores = await self.repository.search_by_embedding(
            query_embedding=query_embedding,
            user_context=user_context,
            top_k=top_k,
            score_threshold=score_threshold,
            metadata_filters=metadata_filters,
        )

        logger.debug(
            "knowledge_store_search_results",
            results_count=len(chunks_with_scores),
        )

        # 3. Re-rank based on lifecycle (recency + type + priority)
        results_with_scores = []
        for chunk, similarity in chunks_with_scores:
            results_with_scores.append({"chunk": chunk, "score": similarity, "id": chunk.id})

        ranked_results = self._apply_lifecycle_ranking(
            [item["chunk"] for item in results_with_scores], results_with_scores
        )

        # 4. Sort by final score and return
        sorted_chunks = sorted(ranked_results, key=lambda x: x["final_score"], reverse=True)
        return [item["chunk"] for item in sorted_chunks]

    async def search_hybrid(
        self,
        query: str,
        user_context: UserContext,
        top_k: int = 10,
        score_threshold: float = 0.7,
        metadata_filters: dict[str, Any] | None = None,
    ) -> list[KnowledgeChunk]:
        """
        Compatibility search using the unified semantic ranker + reranker flow.

        Args:
            query: Search query text
            user_context: User context for ACL filtering
            top_k: Maximum number of results
            score_threshold: Minimum similarity score for the ranker (ignored
                when reranking is available, to match Farseer-style broad recall)
            metadata_filters: Optional metadata filters

        Returns:
            List of matching chunks ordered by unified search relevance
        """
        if not self.hybrid_search_service:
            # Fallback to semantic search if hybrid not available
            logger.warning("hybrid_search_not_available_falling_back_to_semantic")
            return await self.search(
                query=query,
                user_context=user_context,
                top_k=top_k,
                score_threshold=score_threshold,
                metadata_filters=metadata_filters,
            )

        # Use hybrid search service
        results_dicts = await self.hybrid_search_service.adaptive_search(
            query=query,
            user_context=user_context,
            filters=metadata_filters,
            top_k=top_k * 2,  # Get more candidates for lifecycle ranking
            score_threshold=score_threshold,
        )

        # Convert dict results back to KnowledgeChunk objects
        # The hybrid search returns simplified dicts, we need full chunks

        # Fetch full chunks with ACL enforcement
        acl_filtered_chunks = await self.repository.get_chunks_with_acl(
            chunk_ids=[r["id"] for r in results_dicts], user_context=user_context
        )

        # Create chunk lookup by ID
        chunk_by_id = {str(chunk.id): chunk for chunk in acl_filtered_chunks}

        # Rebuild chunks list in original service order, but only with ACL-approved chunks
        chunks = []
        for r in results_dicts:
            chunk = chunk_by_id.get(r["id"])
            if chunk:  # Only include if ACL check passed
                chunks.append(chunk)

        # Return chunks in service order (no lifecycle reranking)
        # Already ACL-filtered and ordered by semantic ranker + reranker relevance
        return chunks[:top_k]

    def _get_reranker(self) -> Any | None:
        """Cross-encoder reranker is intentionally absent in the preview path.

        The signature is kept so the conditional branches in the search
        methods continue to compile and short-circuit cleanly. When
        MEHO.Knowledge takes over remote embedding/reranking the reranker
        will return as a real provider behind this method.
        """
        return None

    @staticmethod
    def _metadata_to_dict(raw_metadata: Any) -> dict[str, Any]:
        """Normalize search metadata to a plain dictionary."""
        if raw_metadata is None:
            return {}
        if hasattr(raw_metadata, "model_dump"):
            dumped: dict[str, Any] = raw_metadata.model_dump()
            return dumped
        if isinstance(raw_metadata, dict):
            return dict(raw_metadata)
        return {}

    @staticmethod
    def _normalize_page_numbers(values: Any) -> list[int]:
        """Normalize page numbers into a sorted list of positive integers."""
        if not isinstance(values, list):
            return []

        normalized: list[int] = []
        for value in values:
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                page_number = value
            else:
                try:
                    page_number = int(value)
                except (TypeError, ValueError):
                    continue
            if page_number > 0:
                normalized.append(page_number)

        return sorted(set(normalized))

    @staticmethod
    def _normalize_int(value: Any) -> int:
        """Normalize an integer-like metadata field."""
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _extract_source_chunk_index(source_uri: str | None) -> int | None:
        """Extract a chunk index from URIs like `...#chunk=4` when available."""
        if not source_uri or "#chunk=" not in source_uri:
            return None

        chunk_suffix = source_uri.rsplit("#chunk=", 1)[-1]
        try:
            chunk_index = int(chunk_suffix)
        except ValueError:
            return None
        return chunk_index if chunk_index >= 0 else None

    @classmethod
    def _build_ranked_chunk_payload(cls, ranked_result: dict[str, Any]) -> dict[str, Any]:
        """Build a UI-ready ranked result payload from a retrieved chunk."""
        chunk = ranked_result["chunk"]
        metadata = cls._metadata_to_dict(getattr(chunk, "search_metadata", None))
        heading_path = clean_heading_path(
            metadata.get("heading_hierarchy") or metadata.get("heading_stack") or []
        )
        section_header = str(
            metadata.get("section")
            or metadata.get("chapter")
            or (heading_path[-1] if heading_path else "")
            or ""
        )

        page_numbers = cls._normalize_page_numbers(metadata.get("page_numbers"))
        page_number = cls._normalize_int(metadata.get("page_number"))
        page_start = cls._normalize_int(metadata.get("page_start"))
        page_end = cls._normalize_int(metadata.get("page_end"))
        if page_numbers:
            if page_number <= 0:
                page_number = page_numbers[0]
            if page_start <= 0:
                page_start = page_numbers[0]
            if page_end <= 0:
                page_end = page_numbers[-1]

        knowledge_type = getattr(chunk.knowledge_type, "value", chunk.knowledge_type)

        return {
            "id": str(chunk.id),
            "text": chunk.text,
            "score": float(ranked_result.get("rerank_score", ranked_result.get("similarity", 0.0))),
            "tenant_id": chunk.tenant_id,
            "source_uri": chunk.source_uri,
            "source_chunk_index": cls._extract_source_chunk_index(chunk.source_uri),
            "tags": chunk.tags or [],
            "knowledge_type": str(knowledge_type),
            "connector_id": str(chunk.connector_id) if chunk.connector_id else "",
            "scope_type": chunk.scope_type or "instance",
            "connector_type_scope": chunk.connector_type_scope,
            "doc_version": chunk.doc_version or "",
            "family_id": str(chunk.family_id) if getattr(chunk, "family_id", None) else "",
            "filename": extract_filename(
                source_uri=chunk.source_uri,
                document_name=str(metadata.get("document_name") or ""),
            ),
            "section_header": section_header,
            "heading_path": heading_path,
            "page_number": page_number,
            "page_numbers": page_numbers,
            "page_start": page_start,
            "page_end": page_end,
            "search_metadata": metadata,
        }

    async def _semantic_rank_and_rerank(
        self,
        *,
        query: str,
        user_context: UserContext,
        top_k: int,
        score_threshold: float,
        metadata_filters: dict[str, Any] | None = None,
        connector_id: str | None = None,
        doc_version: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run Farseer-style semantic ranking followed by optional reranking."""
        query_embedding = await self.embedding_provider.embed_text(query)
        rerank_candidates = min(
            max(top_k * RERANK_CANDIDATE_MULTIPLIER, top_k),
            RERANK_CANDIDATE_LIMIT,
        )
        chunks_with_scores = await self.repository.search_by_embedding(
            query_embedding=query_embedding,
            user_context=user_context,
            top_k=rerank_candidates,
            score_threshold=score_threshold,
            metadata_filters=metadata_filters,
            connector_id=connector_id,
            doc_version=doc_version,
        )

        candidates: list[dict[str, Any]] = []
        for chunk, similarity in chunks_with_scores:
            candidates.append(
                {
                    "id": str(chunk.id),
                    "chunk": chunk,
                    "similarity": similarity,
                    "retrieval_text": build_retrieval_text_from_metadata(
                        text=chunk.text,
                        source_uri=chunk.source_uri,
                        metadata=chunk.search_metadata,
                    ),
                }
            )

        if not candidates:
            return []

        reranker = self._get_reranker()
        if reranker is None or len(candidates) <= 1:
            logger.debug(
                "semantic_rank_and_rerank_skip",
                reason="no_reranker" if reranker is None else "single_candidate",
                num_candidates=len(candidates),
            )
            return candidates[:top_k]

        rerank_results = await reranker.rerank(
            query=query,
            documents=[candidate["retrieval_text"] for candidate in candidates],
            top_k=top_k,
        )

        reranked: list[dict[str, Any]] = []
        for rerank_result in rerank_results:
            original_idx = rerank_result["index"]
            if original_idx < len(candidates):
                candidate = candidates[original_idx].copy()
                candidate["rerank_score"] = rerank_result["relevance_score"]
                reranked.append(candidate)

        logger.info(
            "semantic_rank_and_rerank_completed",
            query=query,
            candidates_retrieved=len(candidates),
            reranked_results=len(reranked),
            top_rerank_score=reranked[0]["rerank_score"] if reranked else 0,
        )
        return reranked

    async def search_by_connector(
        self,
        query: str,
        user_context: UserContext,
        connector_id: str,
        top_k: int = 10,
        score_threshold: float = 0.0,
        metadata_filters: dict[str, Any] | None = None,
        doc_version: str | None = None,
    ) -> list[KnowledgeChunk]:
        """
        Connector-scoped search — strict scoping for specialist agents.

        Only returns results from the specified connector. No fallback to
        tenant-wide search. If the connector has zero docs, returns empty.

        Args:
            query: Search query text
            user_context: User context for ACL filtering
            connector_id: UUID string of the connector to scope search to
            top_k: Maximum number of results
            score_threshold: Minimum similarity score (0-1)
            metadata_filters: Optional metadata filters

        Returns:
            List of matching chunks from this connector only
        """
        logger.debug(
            "knowledge_store_search_by_connector",
            query=query,
            connector_id=connector_id,
            tenant_id=user_context.tenant_id,
            top_k=top_k,
        )

        retrieval_threshold = 0.0 if self._get_reranker() is not None else score_threshold
        ranked_results = await self._semantic_rank_and_rerank(
            query=query,
            user_context=user_context,
            top_k=top_k,
            score_threshold=retrieval_threshold,
            metadata_filters=metadata_filters,
            connector_id=connector_id,
            doc_version=doc_version,
        )
        chunks = [result["chunk"] for result in ranked_results]

        logger.info(
            "connector_scoped_search_completed",
            connector_id=connector_id,
            results_count=len(chunks),
            tenant_id=user_context.tenant_id,
            search_mode="semantic_rank_rerank" if self._get_reranker() else "semantic_rank_only",
        )
        return chunks[:top_k]

    async def _load_family_names(self, family_ids: list[Any]) -> dict[str, str]:
        """Batch-load family display names for a list of family UUIDs."""
        filtered = [fid for fid in family_ids if fid]
        if not filtered:
            return {}

        from meho_app.modules.knowledge.models import DocumentFamilyModel

        rows = await self.repository.session.execute(
            select(DocumentFamilyModel.id, DocumentFamilyModel.name).where(
                DocumentFamilyModel.id.in_(filtered)
            )
        )
        return {str(row_id): row_name for row_id, row_name in rows.all()}

    async def search_ranked_by_connector(
        self,
        query: str,
        user_context: UserContext,
        connector_id: str,
        top_k: int = 10,
        score_threshold: float = 0.0,
        metadata_filters: dict[str, Any] | None = None,
        doc_version: str | None = None,
    ) -> list[dict[str, Any]]:
        """Connector-scoped ranked results with scores and retrieval metadata."""
        retrieval_threshold = 0.0 if self._get_reranker() is not None else score_threshold
        ranked_results = await self._semantic_rank_and_rerank(
            query=query,
            user_context=user_context,
            top_k=top_k,
            score_threshold=retrieval_threshold,
            metadata_filters=metadata_filters,
            connector_id=connector_id,
            doc_version=doc_version,
        )

        family_name_by_id = await self._load_family_names(
            [getattr(r["chunk"], "family_id", None) for r in ranked_results]
        )
        payloads: list[dict[str, Any]] = []
        for result in ranked_results[:top_k]:
            payload = self._build_ranked_chunk_payload(result)
            payload["family_name"] = family_name_by_id.get(payload.get("family_id") or "", "")
            payloads.append(payload)
        return payloads

    async def search_cross_connector(
        self,
        query: str,
        user_context: UserContext,
        top_k: int = 10,
        score_threshold: float = 0.0,
        metadata_filters: dict[str, Any] | None = None,
        doc_version: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Cross-connector search with attribution — for orchestrator and KnowledgePage browse.

        Searches ALL connectors within the tenant. Returns results with connector
        attribution (connector_name, connector_type) so the agent can cite sources
        and the UI can display connector badges.

        Args:
            query: Search query text
            user_context: User context for ACL filtering
            top_k: Maximum number of results
            score_threshold: Minimum similarity score (0-1)
            metadata_filters: Optional metadata filters

        Returns:
            List of dicts with text, source_uri, connector_id, connector_name,
            connector_type, and score for each result
        """
        from meho_app.modules.connectors.models import ConnectorModel

        logger.debug(
            "knowledge_store_search_cross_connector",
            query=query,
            tenant_id=user_context.tenant_id,
            top_k=top_k,
        )

        retrieval_threshold = 0.0 if self._get_reranker() is not None else score_threshold
        ranked_results = await self._semantic_rank_and_rerank(
            query=query,
            user_context=user_context,
            top_k=top_k,
            score_threshold=retrieval_threshold,
            metadata_filters=metadata_filters,
            doc_version=doc_version,
        )

        connector_ids = [
            result["chunk"].connector_id
            for result in ranked_results
            if getattr(result["chunk"], "connector_id", None)
        ]
        connector_by_id: dict[str, tuple[str | None, str | None]] = {}
        if connector_ids:
            connector_rows = await self.repository.session.execute(
                select(
                    ConnectorModel.id,
                    ConnectorModel.name,
                    ConnectorModel.connector_type,
                ).where(ConnectorModel.id.in_(connector_ids))
            )
            connector_by_id = {
                str(connector_id): (connector_name, connector_type)
                for connector_id, connector_name, connector_type in connector_rows.all()
            }

        # Batch-load family names so the UI can render "VCF Docs, v9.0.0" next
        # to each chunk without an extra API round-trip.
        family_name_by_id = await self._load_family_names(
            [getattr(r["chunk"], "family_id", None) for r in ranked_results]
        )

        results: list[dict[str, Any]] = []
        for ranked_result in ranked_results:
            result = self._build_ranked_chunk_payload(ranked_result)
            connector_id = result["connector_id"]
            connector_name, connector_type = connector_by_id.get(connector_id, (None, None))
            scope = result["scope_type"] or "instance"
            result["connector_name"] = connector_name or (
                "Global" if scope == "global" else result["connector_type_scope"] or "Type"
            )
            result["connector_type"] = connector_type or result["connector_type_scope"] or scope
            result["family_name"] = family_name_by_id.get(result.get("family_id") or "", "")
            results.append(result)

        logger.info(
            "cross_connector_search_completed",
            results_count=len(results),
            tenant_id=user_context.tenant_id,
            search_mode="semantic_rank_rerank" if self._get_reranker() else "semantic_rank_only",
        )
        return results[:top_k]

    def _apply_lifecycle_ranking(
        self,
        chunks: list[KnowledgeChunk],
        vector_results: list[dict],
        lifecycle_weight: float = 1.0,
    ) -> list[dict]:
        """
        Apply lifecycle-aware ranking to search results.

        Adjusts vector similarity scores based on:
        - Knowledge type (documentation = always relevant, events = recency matters)
        - Recency (recent events = higher score, old events = lower score)
        - Priority (explicit priority boost)

        Args:
            chunks: List of knowledge chunks
            vector_results: List of vector search results with scores
            lifecycle_weight: Weight for lifecycle adjustments (0-1)
                            0 = ignore lifecycle (use base score only)
                            1 = full lifecycle influence (default)
                            0.2 = light lifecycle (80% base, 20% lifecycle)

        Returns:
            List of dicts with chunk and final_score
        """
        now = datetime.now(tz=UTC)
        results_with_scores = []

        # Create lookup for vector scores
        score_map = {r["id"]: r.get("score", 0.7) for r in vector_results}

        for chunk in chunks:
            base_score = score_map.get(chunk.id, 0.7)

            # Start with base similarity score
            lifecycle_multiplier = 1.0

            # Adjust for knowledge type
            if chunk.knowledge_type == KnowledgeType.DOCUMENTATION:
                # Documentation is always relevant
                lifecycle_multiplier *= 1.2

            elif chunk.knowledge_type == KnowledgeType.PROCEDURE:
                # Procedures are always relevant
                lifecycle_multiplier *= 1.2

            elif chunk.knowledge_type == KnowledgeType.EVENT:
                # Events: value decreases with age
                age_hours = (now - chunk.created_at).total_seconds() / 3600

                if age_hours < 1:
                    recency_boost = 1.5  # Very recent (< 1h)
                elif age_hours < 24:
                    recency_boost = 1.3  # Today
                elif age_hours < 168:  # 7 days
                    recency_boost = 1.0  # This week
                else:
                    recency_boost = 0.5  # Older (downweight significantly)

                lifecycle_multiplier *= recency_boost

            elif chunk.knowledge_type == KnowledgeType.EVENT_SUMMARY:
                # Event summaries are more valuable than individual events
                lifecycle_multiplier *= 1.1

            # Apply explicit priority (range: -100 to +100, normalized to multiplier)
            priority_boost = 1.0 + (chunk.priority / 100.0)
            lifecycle_multiplier *= priority_boost

            # Blend base score with lifecycle adjustment
            # lifecycle_weight=1.0 (default): full lifecycle influence
            # lifecycle_weight=0.2: 80% base score, 20% lifecycle adjustment
            final_score = base_score * (
                1.0 - lifecycle_weight + (lifecycle_weight * lifecycle_multiplier)
            )

            results_with_scores.append(
                {"chunk": chunk, "base_score": base_score, "final_score": final_score}
            )

        return results_with_scores

    async def delete_chunk(self, chunk_id: str) -> bool:
        """
        Delete chunk from PostgreSQL (embedding deleted automatically with pgvector).

        With pgvector, the embedding is stored in the same row, so deleting the row
        automatically removes the vector embedding. No separate vector store cleanup needed!

        Args:
            chunk_id: Chunk identifier

        Returns:
            True if deleted, False if not found
        """
        deleted = await self.repository.delete_chunk(chunk_id)

        if deleted:
            logger.info(f"Deleted chunk {chunk_id} from PostgreSQL (embedding included)")
        else:
            logger.warning(f"Chunk {chunk_id} not found")

        return deleted

    async def delete_document(  # NOSONAR (cognitive complexity)
        self,
        chunk_ids: list[str],
        tenant_id: str | None = None,
        job_repository: Any | None = None,
        job_id: str | None = None,
        object_storage: Any | None = None,
        storage_key: str | None = None,
    ) -> int:
        """
        Delete a document and all its chunks with progress tracking (Session 30).

        Stages:
        1. PREPARING - Count chunks
        2. DELETING_CHUNKS - Delete from PostgreSQL (batch operation)
        3. UPDATING_INDEX - Finalize search state
        4. CLEANUP_STORAGE - Delete original file

        Args:
            chunk_ids: List of chunk IDs to delete
            tenant_id: Tenant ID (for logging)
            job_repository: For progress tracking
            job_id: Deletion job ID
            object_storage: Object storage client
            storage_key: Original file storage key

        Returns:
            Number of chunks deleted

        Note: No manual search-index rebuild is required during deletion.
        """

        from meho_app.modules.knowledge.job_models import DeletionStage

        total_chunks = len(chunk_ids)

        # Stage 1: Preparing (5%)
        if job_repository and job_id:
            await job_repository.update_stage(
                job_id=job_id,
                current_stage=DeletionStage.PREPARING.value,
                stage_progress=1.0,
                overall_progress=0.05,
                status_message=f"Preparing to delete {total_chunks} chunks...",
            )

        # Stage 2: Deleting chunks (70% of time)
        if job_repository and job_id:
            await job_repository.update_stage(
                job_id=job_id,
                current_stage=DeletionStage.DELETING_CHUNKS.value,
                stage_progress=0.0,
                overall_progress=0.05,
                status_message=f"Deleting {total_chunks} chunks...",
            )

        # Batch delete for efficiency
        deleted_count = await self.repository.delete_chunks_batch(chunk_ids)

        if job_repository and job_id:
            await job_repository.update_stage(
                job_id=job_id,
                current_stage=DeletionStage.DELETING_CHUNKS.value,
                stage_progress=1.0,
                overall_progress=0.75,
                status_message=f"Deleted {deleted_count} chunks",
            )

        # Stage 3: Search state finalization
        if job_repository and job_id:
            await job_repository.update_stage(
                job_id=job_id,
                current_stage=DeletionStage.UPDATING_INDEX.value,
                stage_progress=1.0,
                overall_progress=0.85,
                status_message="Search state finalized",
            )

        logger.debug("knowledge_search_state_finalized", tenant_id=tenant_id)

        if job_repository and job_id:
            await job_repository.update_stage(
                job_id=job_id,
                current_stage=DeletionStage.UPDATING_INDEX.value,
                stage_progress=1.0,
                overall_progress=0.95,
                status_message="Search index updated",
            )

        # Stage 4: Cleanup storage (5% of time)
        if object_storage and storage_key:
            if job_repository and job_id:
                await job_repository.update_stage(
                    job_id=job_id,
                    current_stage=DeletionStage.CLEANUP_STORAGE.value,
                    stage_progress=0.0,
                    overall_progress=0.95,
                    status_message="Cleaning up storage...",
                )

            try:
                object_storage.delete_document(storage_key)
                logger.info(f"Deleted original file from storage: {storage_key}")
            except Exception as e:
                logger.warning(f"Failed to delete from storage: {e}")  # noqa: S608 -- static SQL query, no user input
                # Don't fail deletion if storage cleanup fails

        if job_repository and job_id:
            await job_repository.update_stage(
                job_id=job_id,
                current_stage=DeletionStage.CLEANUP_STORAGE.value,
                stage_progress=1.0,
                overall_progress=1.0,
                status_message=f"Deletion complete - {deleted_count} chunks removed",
            )

        return deleted_count

    async def get_chunk(self, chunk_id: str) -> KnowledgeChunk | None:
        """Get chunk by ID (from database only)"""
        return await self.repository.get_chunk(chunk_id)

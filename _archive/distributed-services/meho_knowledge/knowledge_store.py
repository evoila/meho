"""
Unified knowledge store using PostgreSQL with pgvector.

Provides high-level interface for adding and searching knowledge chunks.
All data (text, metadata, vectors) stored in single PostgreSQL database.
"""
# mypy: disable-error-code="misc,valid-type,attr-defined,var-annotated"
from meho_knowledge.repository import KnowledgeRepository
from meho_knowledge.embeddings import EmbeddingProvider
from meho_knowledge.schemas import KnowledgeChunkCreate, KnowledgeChunk, KnowledgeType
from meho_core.auth_context import UserContext
from meho_core.structured_logging import get_logger
from typing import List, Dict, Any, Optional, TYPE_CHECKING
from datetime import datetime

if TYPE_CHECKING:
    from meho_knowledge.hybrid_search import PostgresFTSHybridService

logger = get_logger(__name__)


class KnowledgeStore:
    """Unified interface for knowledge storage and retrieval using PostgreSQL+pgvector"""
    
    def __init__(
        self,
        repository: KnowledgeRepository,
        embedding_provider: EmbeddingProvider,
        hybrid_search_service: Optional["PostgresFTSHybridService"] = None
    ):
        """
        Initialize knowledge store.
        
        Args:
            repository: PostgreSQL repository (handles both data and vector search with pgvector)
            embedding_provider: Provider for generating embeddings
            hybrid_search_service: Optional hybrid search service (PostgreSQL FTS + semantic)
        
        Note: Everything in PostgreSQL - vectors (pgvector), full-text search (GIN), metadata (JSONB)!
        """
        self.repository = repository
        self.embedding_provider = embedding_provider
        self.hybrid_search_service = hybrid_search_service
        
        # Warn if hybrid search is not provided (will use semantic-only search)
        if hybrid_search_service is None:
            logger.warning(
                "knowledge_store_created_without_hybrid_search",
                warning="KnowledgeStore created without hybrid_search_service. "
                        "Search will use semantic embeddings only (no keyword matching). "
                        "This is OK for read-only operations, but production should include hybrid search."
            )
    
    async def add_chunk(self, chunk_create: KnowledgeChunkCreate) -> KnowledgeChunk:
        """
        Add a knowledge chunk to PostgreSQL with embedding.
        
        Args:
            chunk_create: Chunk data without ID
        
        Returns:
            Created chunk with ID
            
        Raises:
            ValidationError: If text exceeds OpenAI embedding token limit
        """
        # Validate text size for OpenAI embedding
        from meho_knowledge.text_validation import validate_text_for_embedding, truncate_text_to_token_limit
        from meho_core.errors import ValidationError as MehoValidationError
        
        is_valid, error_msg, token_count = validate_text_for_embedding(chunk_create.text)
        
        if not is_valid:
            logger.warning(f"Text exceeds token limit ({token_count} tokens), truncating...")
            # Auto-truncate instead of failing
            chunk_create.text = truncate_text_to_token_limit(chunk_create.text)
            logger.info(f"Truncated text to fit within token limit")
        
        try:
            # 1. Generate embedding
            embedding = await self.embedding_provider.embed_text(chunk_create.text)
            
            # 2. Create in database with embedding (single atomic operation!)
            chunk = await self.repository.create_chunk(chunk_create, embedding=embedding)
            
            logger.info(
                "chunk_created_with_embedding",
                chunk_id=str(chunk.id),
                has_embedding=True,
                has_search_metadata=chunk.search_metadata is not None,
                resource_type=chunk.search_metadata.get('resource_type') if isinstance(chunk.search_metadata, dict) else None
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
        metadata_filters: Optional[Dict[str, Any]] = None
    ) -> List[KnowledgeChunk]:
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
        # DEBUG: Log what knowledge_store.search receives
        import sys
        print(f"\n📚 KNOWLEDGE_STORE.search: query='{query}'", file=sys.stderr)
        print(f"📚 User context: tenant_id={user_context.tenant_id}, user_id={user_context.user_id}", file=sys.stderr)
        print(f"📚 Params: top_k={top_k}, score_threshold={score_threshold}, metadata_filters={metadata_filters}", file=sys.stderr)
        
        # 1. Generate query embedding
        query_embedding = await self.embedding_provider.embed_text(query)
        
        # 2. Search PostgreSQL with pgvector (handles ACL + metadata filtering)
        chunks_with_scores = await self.repository.search_by_embedding(
            query_embedding=query_embedding,
            user_context=user_context,
            top_k=top_k,
            score_threshold=score_threshold,
            metadata_filters=metadata_filters
        )
        
        print(f"📚 KNOWLEDGE_STORE.search: repository returned {len(chunks_with_scores)} results", file=sys.stderr)
        
        # 3. Re-rank based on lifecycle (recency + type + priority)
        results_with_scores = []
        for chunk, similarity in chunks_with_scores:
            results_with_scores.append({
                "chunk": chunk,
                "score": similarity,
                "id": chunk.id
            })
        
        ranked_results = self._apply_lifecycle_ranking(
            [item["chunk"] for item in results_with_scores],
            results_with_scores
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
        metadata_filters: Optional[Dict[str, Any]] = None
    ) -> List[KnowledgeChunk]:
        """
        Hybrid search combining PostgreSQL FTS and semantic search.
        
        Automatically balances keyword matching with semantic similarity using RRF.
        Better for technical queries with specific terms (endpoints, constants, role names).
        
        Args:
            query: Search query text
            user_context: User context for ACL filtering
            top_k: Maximum number of results
            score_threshold: Minimum similarity score for semantic component (0-1)
            metadata_filters: Optional metadata filters
        
        Returns:
            List of matching chunks ordered by hybrid RRF score
        """
        if not self.hybrid_search_service:
            # Fallback to semantic search if hybrid not available
            logger.warning("hybrid_search_not_available_falling_back_to_semantic")
            return await self.search(
                query=query,
                user_context=user_context,
                top_k=top_k,
                score_threshold=score_threshold,
                metadata_filters=metadata_filters
            )
        
        # Use hybrid search service
        results_dicts = await self.hybrid_search_service.adaptive_search(
            query=query,
            user_context=user_context,
            filters=metadata_filters,
            top_k=top_k * 2,  # Get more candidates for lifecycle ranking
            score_threshold=score_threshold
        )
        
        # Convert dict results back to KnowledgeChunk objects
        # The hybrid search returns simplified dicts, we need full chunks
        
        # Fetch full chunks with ACL enforcement
        # SECURITY: Must use get_chunks_with_acl to prevent users from accessing
        # restricted documents via keyword-heavy BM25 queries
        acl_filtered_chunks = await self.repository.get_chunks_with_acl(
            chunk_ids=[r["id"] for r in results_dicts],
            user_context=user_context
        )
        
        # Create chunk lookup by ID
        chunk_by_id = {str(chunk.id): chunk for chunk in acl_filtered_chunks}
        
        # Rebuild chunks list in original RRF order, but only with ACL-approved chunks
        chunks = []
        for r in results_dicts:
            chunk = chunk_by_id.get(r["id"])
            if chunk:  # Only include if ACL check passed
                chunks.append(chunk)
        
        # DISABLED: Lifecycle ranking interferes with RRF fusion ranking
        # RRF already provides sophisticated scoring by combining BM25 and semantic search
        # Lifecycle ranking was causing correct results to be reordered incorrectly
        
        # Return chunks in RRF order (no lifecycle reranking)
        # Already ACL-filtered and ordered by hybrid search relevance
        return chunks[:top_k]
    
    
    def _apply_lifecycle_ranking(
        self,
        chunks: List[KnowledgeChunk],
        vector_results: List[dict],
        lifecycle_weight: float = 1.0
    ) -> List[dict]:
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
        now = datetime.utcnow()
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
            final_score = base_score * (1.0 - lifecycle_weight + (lifecycle_weight * lifecycle_multiplier))
            
            results_with_scores.append({
                "chunk": chunk,
                "base_score": base_score,
                "final_score": final_score
            })
        
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
    
    async def delete_document(
        self,
        chunk_ids: List[str],
        tenant_id: Optional[str] = None,
        job_repository: Optional[any] = None,
        job_id: Optional[str] = None,
        object_storage: Optional[any] = None,
        storage_key: Optional[str] = None
    ) -> int:
        """
        Delete a document and all its chunks with progress tracking (Session 30).
        
        Stages:
        1. PREPARING - Count chunks
        2. DELETING_CHUNKS - Delete from PostgreSQL (batch operation)
        3. UPDATING_INDEX - Auto-maintained by PostgreSQL FTS
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
        
        Note: PostgreSQL FTS indexes are automatically maintained on DELETE.
        No manual index rebuilding required.
        """
        from meho_knowledge.job_models import DeletionStage
        from datetime import datetime
        
        total_chunks = len(chunk_ids)
        
        # Stage 1: Preparing (5%)
        if job_repository and job_id:
            await job_repository.update_stage(
                job_id=job_id,
                current_stage=DeletionStage.PREPARING.value,
                stage_progress=1.0,
                overall_progress=0.05,
                status_message=f"Preparing to delete {total_chunks} chunks..."
            )
        
        # Stage 2: Deleting chunks (70% of time)
        if job_repository and job_id:
            await job_repository.update_stage(
                job_id=job_id,
                current_stage=DeletionStage.DELETING_CHUNKS.value,
                stage_progress=0.0,
                overall_progress=0.05,
                status_message=f"Deleting {total_chunks} chunks..."
            )
        
        # Batch delete for efficiency
        deleted_count = await self.repository.delete_chunks_batch(chunk_ids)
        
        if job_repository and job_id:
            await job_repository.update_stage(
                job_id=job_id,
                current_stage=DeletionStage.DELETING_CHUNKS.value,
                stage_progress=1.0,
                overall_progress=0.75,
                status_message=f"Deleted {deleted_count} chunks"
            )
        
        # Stage 3: Search indexes (auto-maintained by PostgreSQL)
        if job_repository and job_id:
            await job_repository.update_stage(
                job_id=job_id,
                current_stage=DeletionStage.UPDATING_INDEX.value,
                stage_progress=1.0,
                overall_progress=0.85,
                status_message="Search indexes auto-updated (PostgreSQL FTS)"
            )
        
        # NOTE: No manual index rebuilding needed!
        # PostgreSQL FTS indexes are automatically maintained on DELETE operations.
        logger.debug(
            "fts_index_auto_maintained",
            tenant_id=tenant_id,
            message="PostgreSQL FTS indexes automatically updated after deletion"
        )
        
        if job_repository and job_id:
            await job_repository.update_stage(
                job_id=job_id,
                current_stage=DeletionStage.UPDATING_INDEX.value,
                stage_progress=1.0,
                overall_progress=0.95,
                status_message="Search index updated"
            )
        
        # Stage 4: Cleanup storage (5% of time)
        if object_storage and storage_key:
            if job_repository and job_id:
                await job_repository.update_stage(
                    job_id=job_id,
                    current_stage=DeletionStage.CLEANUP_STORAGE.value,
                    stage_progress=0.0,
                    overall_progress=0.95,
                    status_message="Cleaning up storage..."
                )
            
            try:
                object_storage.delete_document(storage_key)
                logger.info(f"Deleted original file from storage: {storage_key}")
            except Exception as e:
                logger.warning(f"Failed to delete from storage: {e}")
                # Don't fail deletion if storage cleanup fails
        
        if job_repository and job_id:
            await job_repository.update_stage(
                job_id=job_id,
                current_stage=DeletionStage.CLEANUP_STORAGE.value,
                stage_progress=1.0,
                overall_progress=1.0,
                status_message=f"Deletion complete - {deleted_count} chunks removed"
            )
        
        return deleted_count
    
    async def get_chunk(self, chunk_id: str) -> KnowledgeChunk | None:
        """Get chunk by ID (from database only)"""
        return await self.repository.get_chunk(chunk_id)


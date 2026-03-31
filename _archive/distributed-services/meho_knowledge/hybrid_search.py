"""
Hybrid Search Service combining BM25 and semantic search.

Uses Reciprocal Rank Fusion (RRF) to combine results from multiple search methods.
"""

from typing import List, Dict, Any
from uuid import UUID
import asyncio
import re
import hashlib
import structlog

from .postgres_fts import PostgresFTSService
from .repository import KnowledgeRepository
from .embeddings import EmbeddingProvider
from meho_core.auth_context import UserContext

logger = structlog.get_logger(__name__)


class PostgresFTSHybridService:
    """
    Combines PostgreSQL FTS and semantic search using Reciprocal Rank Fusion.
    
    This service uses PostgreSQL's built-in full-text search (to_tsvector, ts_rank)
    combined with pgvector semantic search for optimal results on unstructured text.
    
    For structured data (endpoints, operations), use BM25HybridService instead.
    """
    
    def __init__(
        self,
        repository: KnowledgeRepository,
        embeddings: EmbeddingProvider
    ):
        """
        Initialize hybrid search service.
        
        Args:
            repository: Knowledge repository (provides DB session)
            embeddings: Embedding provider for semantic search
        
        Note: PostgresFTSService is created per-search using the repository's session.
        This ensures we always use the correct transaction context.
        """
        self.repository = repository
        self.embeddings = embeddings
        
        logger.info("postgres_fts_hybrid_search_service_initialized", search_type="postgres_fts")
    
    async def search(
        self,
        query: str,
        user_context: UserContext,
        filters: Dict[str, Any] | None = None,
        top_k: int = 10,
        score_threshold: float = 0.7,
        bm25_weight: float = 0.5,
        semantic_weight: float = 0.5
    ) -> List[Dict[str, Any]]:
        """
        Hybrid search combining BM25 and semantic search.
        
        Args:
            query: Search query
            user_context: User context for ACL
            filters: Metadata filters (applied to semantic search)
            top_k: Number of final results
            score_threshold: Minimum similarity score for semantic search
            bm25_weight: Weight for BM25 results (0-1)
            semantic_weight: Weight for semantic results (0-1)
        
        Returns:
            Fused and ranked results
        """
        logger.info(
            "hybrid_search_started",
            query=query,
            tenant_id=user_context.tenant_id,
            top_k=top_k,
            bm25_weight=bm25_weight,
            semantic_weight=semantic_weight
        )
        
        # Run both searches in parallel
        # Convert tenant_id to UUID using MD5 hash (same as index building)
        # BM25 indexes are stored using MD5(tenant_id) formatted as UUID
        if not user_context.tenant_id:
            raise ValueError("tenant_id is required for hybrid search")
        
        try:
            # Try parsing as UUID first (in case it's already a UUID string)
            tenant_uuid = UUID(user_context.tenant_id)
        except ValueError:
            # tenant_id is not UUID format (e.g., "demo-tenant")
            # Convert to UUID using MD5 hash (same method used when building indexes)
            tenant_hash = hashlib.md5(user_context.tenant_id.encode()).hexdigest()
            tenant_uuid = UUID(tenant_hash)
            logger.debug(
                "converted_tenant_id_to_uuid",
                original_tenant_id=user_context.tenant_id,
                tenant_uuid=str(tenant_uuid)
            )
        
        # Create FTS service with current session
        fts_service = PostgresFTSService(self.repository.session)
        
        # Generate embedding for semantic search (can run in parallel with FTS)
        # Embedding generation doesn't use DB session
        query_vector = await self.embeddings.embed_text(query)
        
        # IMPORTANT: Run searches SEQUENTIALLY, not in parallel!
        # SQLAlchemy async sessions don't support concurrent operations.
        # Both FTS and semantic search use self.repository.session,
        # so parallel execution with asyncio.gather() causes:
        # "This session is provisioning a new connection; concurrent operations are not permitted"
        
        # Performance impact is minimal:
        # - FTS: ~10-50ms (PostgreSQL GIN index)
        # - Semantic: ~50-200ms (pgvector similarity)
        # - Sequential total: ~100-300ms (still very fast!)
        
        # Run FTS search first (fastest)
        fts_results = await fts_service.search(
            tenant_id=tenant_uuid,
            query=query,
            top_k=100,  # Get more candidates for fusion
            metadata_filters=filters
        )
        
        # Then run semantic search
        semantic_results_tuples = await self.repository.search_by_embedding(
            query_embedding=query_vector,
            user_context=user_context,
            top_k=100,  # Get more candidates for fusion
            score_threshold=score_threshold,
            metadata_filters=filters
        )
        
        # FTS results already have metadata filters applied (done in SQL query)
        # No need for post-filtering like we had with BM25
        
        # Convert semantic results from (chunk, score) tuples to dicts
        semantic_results: List[Dict[str, Any]] = [
            {
                "id": chunk.id,
                "text": chunk.text,
                "metadata": chunk.search_metadata or {},
                "similarity": score,
                "distance": 1 - score  # Convert back to distance for consistency
            }
            for chunk, score in semantic_results_tuples
        ]
        
        logger.debug(
            "search_results_retrieved",
            fts_count=len(fts_results),
            semantic_count=len(semantic_results)
        )
        
        # Fuse results using Reciprocal Rank Fusion
        fused_results = self._reciprocal_rank_fusion(
            fts_results=fts_results,
            semantic_results=semantic_results,
            fts_weight=bm25_weight,  # Keep parameter name for backward compatibility
            semantic_weight=semantic_weight
        )
        
        logger.info(
            "hybrid_search_completed",
            query=query,
            num_results=len(fused_results[:top_k]),
            top_score=fused_results[0]["rrf_score"] if fused_results else 0
        )
        
        return fused_results[:top_k]
    
    def _reciprocal_rank_fusion(
        self,
        fts_results: List[Dict],
        semantic_results: List[Dict],
        fts_weight: float,
        semantic_weight: float,
        k: int = 60
    ) -> List[Dict[str, Any]]:
        """
        Fuse results from multiple sources using RRF.
        
        RRF Formula:
            score(doc) = Σ 1/(k + rank_i)
        
        Where rank_i is the rank of the document in result set i.
        
        Args:
            fts_results: Results from PostgreSQL FTS search
            semantic_results: Results from semantic search
            fts_weight: Weight for FTS scores
            semantic_weight: Weight for semantic scores
            k: Constant for RRF (usually 60)
        
        Returns:
            Fused and sorted results
        """
        # Build document score map
        doc_scores: Dict[str, Dict] = {}
        
        # Process FTS results
        for rank, result in enumerate(fts_results):
            doc_id = result["id"]
            rrf_score = fts_weight / (k + rank + 1)
            
            if doc_id not in doc_scores:
                doc_scores[doc_id] = {
                    "id": doc_id,
                    "text": result["text"],
                    "metadata": result.get("metadata", {}),
                    "rrf_score": 0,
                    "fts_score": result.get("fts_score", 0),
                    "fts_rank": rank + 1,
                    "semantic_score": 0,
                    "semantic_rank": None
                }
            
            doc_scores[doc_id]["rrf_score"] += rrf_score
        
        # Process semantic results
        for rank, result in enumerate(semantic_results):
            doc_id = result["id"]
            rrf_score = semantic_weight / (k + rank + 1)
            
            # Convert distance to similarity score (1 - distance for cosine)
            # pgvector uses cosine distance, so smaller is better
            # Convert to similarity: 1 - distance
            distance = result.get("distance", 0)
            similarity_score = 1 - distance
            
            if doc_id not in doc_scores:
                doc_scores[doc_id] = {
                    "id": doc_id,
                    "text": result["text"],
                    "metadata": result.get("metadata", {}),
                    "rrf_score": 0,
                    "fts_score": 0,
                    "fts_rank": None,
                    "semantic_score": similarity_score,
                    "semantic_rank": rank + 1
                }
            else:
                doc_scores[doc_id]["semantic_score"] = similarity_score
                doc_scores[doc_id]["semantic_rank"] = rank + 1
            
            doc_scores[doc_id]["rrf_score"] += rrf_score
        
        # Sort by RRF score
        sorted_results = sorted(
            doc_scores.values(),
            key=lambda x: x["rrf_score"],
            reverse=True
        )
        
        logger.debug(
            "rrf_fusion_completed",
            num_unique_docs=len(sorted_results),
            bm25_only=sum(1 for d in sorted_results if d["semantic_rank"] is None),
            semantic_only=sum(1 for d in sorted_results if d["fts_rank"] is None),
            both=sum(1 for d in sorted_results if d["semantic_rank"] and d["fts_rank"])
        )
        
        return sorted_results
    
    async def adaptive_search(
        self,
        query: str,
        user_context: UserContext,
        filters: Dict[str, Any] | None = None,
        top_k: int = 10,
        score_threshold: float = 0.7
    ) -> List[Dict[str, Any]]:
        """
        Adaptive hybrid search that automatically adjusts weights.
        
        Uses heuristics to determine if query is more suited for:
        - Keyword matching (technical queries, endpoints)
        - Semantic matching (natural language questions)
        """
        # Analyze query to determine optimal weights
        bm25_weight, semantic_weight = self._analyze_query(query)
        
        logger.debug(
            "adaptive_weights_selected",
            query=query,
            bm25_weight=bm25_weight,
            semantic_weight=semantic_weight
        )
        
        return await self.search(
            query=query,
            user_context=user_context,
            filters=filters,
            top_k=top_k,
            score_threshold=score_threshold,
            bm25_weight=bm25_weight,
            semantic_weight=semantic_weight
        )
    
    def _analyze_query(self, query: str) -> tuple[float, float]:
        """
        Analyze query to determine optimal search weights.
        
        Returns:
            (fts_weight, semantic_weight) tuple
        """
        query_lower = query.lower()
        
        # Technical query indicators (favor FTS/keyword matching)
        technical_indicators = [
            r'/\w+',           # Endpoints
            r'\b(GET|POST|PUT|DELETE|PATCH)\b',  # HTTP methods
            r'\b[A-Z_]{2,}\b', # Constants (ADMIN, OPERATOR)
            r'\b\w+\.\w+\b',   # Dotted notation
            '"',                # Quoted strings
        ]
        
        technical_score = sum(
            1 for pattern in technical_indicators
            if re.search(pattern, query)
        )
        
        # Natural language indicators (favor semantic)
        nlp_indicators = [
            'what', 'how', 'why', 'when', 'where', 'who',
            'explain', 'describe', 'tell me',
            'can you', 'could you', 'would you',
            'difference between', 'compare'
        ]
        
        nlp_score = sum(
            1 for indicator in nlp_indicators
            if indicator in query_lower
        )
        
        # Calculate weights
        if technical_score > nlp_score:
            # Technical query - favor BM25
            return (0.7, 0.3)
        elif nlp_score > technical_score:
            # Natural language - favor semantic
            return (0.3, 0.7)
        else:
            # Balanced
            return (0.5, 0.5)
    
    @staticmethod
    def _matches_filters(metadata: Dict[str, Any], filters: Dict[str, Any]) -> bool:
        """
        Check if document metadata matches the provided filters.
        
        Args:
            metadata: Document metadata dictionary
            filters: Filter criteria dictionary
        
        Returns:
            True if all filters match, False otherwise
        """
        for key, value in filters.items():
            # Get metadata value
            meta_value = metadata.get(key)
            
            # Check match based on type
            if isinstance(value, bool):
                # Boolean fields (has_json_example, has_code_example)
                if meta_value != value:
                    return False
            elif isinstance(value, str):
                # String fields (resource_type, content_type, etc.)
                if str(meta_value) != value:
                    return False
            else:
                # Other types (exact match)
                if meta_value != value:
                    return False
        
        return True


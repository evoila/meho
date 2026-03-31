"""
HTTP routes for Knowledge Service.
"""
# mypy: disable-error-code="no-untyped-def,assignment,attr-defined,index,union-attr,call-overload"
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from meho_knowledge.api_schemas import (
    ChunkCreateRequest,
    ChunkResponse,
    SearchRequest,
    SearchResponse,
    SearchResult,
    IngestTextRequest,
    IngestTextResponse,
    IngestDocumentResponse,
    HealthResponse
)
from meho_knowledge.job_schemas import IngestionJob, IngestionJobFilter
from meho_knowledge.deps import get_knowledge_store, get_ingestion_service, get_hybrid_search, get_job_repository
from meho_knowledge.knowledge_store import KnowledgeStore
from meho_knowledge.ingestion import IngestionService
from meho_knowledge.hybrid_search import PostgresFTSHybridService
from meho_knowledge.job_repository import IngestionJobRepository
from meho_knowledge.schemas import KnowledgeChunkCreate, KnowledgeType
from meho_core.auth_context import UserContext
from meho_core.errors import NotFoundError, ValidationError
from typing import Optional
import json

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("/ping")
async def ping():
    """Simple ping endpoint with no dependencies for testing"""
    return {"status": "pong", "service": "meho-knowledge"}


@router.post("/chunks", response_model=ChunkResponse, status_code=status.HTTP_201_CREATED)
async def create_chunk(
    request: ChunkCreateRequest,
    knowledge_store: KnowledgeStore = Depends(get_knowledge_store)
):
    """
    Create a new knowledge chunk.
    
    The chunk will be stored in both PostgreSQL and Qdrant vector store.
    
    **Knowledge Types:**
    - DOCUMENTATION: Permanent reference material (default)
    - PROCEDURE: Permanent runbooks/guides
    - EVENT: Temporary notices (set expires_at!)
    
    **Examples:**
    
    Permanent documentation:
    ```json
    {
      "text": "my-app architecture...",
      "knowledge_type": "documentation",
      "tags": ["architecture"]
    }
    ```
    
    Temporary notice:
    ```json
    {
      "text": "Berliner marathon tomorrow, all streets closed",
      "knowledge_type": "event",
      "expires_at": "2025-11-17T18:00:00Z",
      "priority": 20,
      "tags": ["notice", "event", "berlin"]
    }
    ```
    """
    try:
        chunk_create = KnowledgeChunkCreate(**request.model_dump())
        chunk = await knowledge_store.add_chunk(chunk_create)
        return ChunkResponse(**chunk.model_dump())
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create chunk: {str(e)}")


@router.get("/chunks/{chunk_id}", response_model=ChunkResponse)
async def get_chunk(
    chunk_id: str,
    knowledge_store: KnowledgeStore = Depends(get_knowledge_store)
):
    """Get a knowledge chunk by ID."""
    chunk = await knowledge_store.get_chunk(chunk_id)
    
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found")
    
    return ChunkResponse(**chunk.model_dump())


@router.post("/search", response_model=SearchResponse)
async def search_knowledge(
    request: SearchRequest,
    knowledge_store: KnowledgeStore = Depends(get_knowledge_store)
):
    """
    Semantic search over knowledge base with ACL filtering.
    
    Returns chunks that:
    1. Match the query semantically (vector similarity)
    2. User has permission to access (ACL filtering)
    """
    try:
        # Build UserContext from request
        user_context = UserContext(
            user_id=request.user_id or "anonymous",
            tenant_id=request.tenant_id,
            system_id=request.system_id,
            roles=request.roles,
            groups=request.groups
        )
        
        # Search with ACL and optional metadata filters
        chunks = await knowledge_store.search(
            query=request.query,
            user_context=user_context,
            top_k=request.top_k,
            score_threshold=request.score_threshold,
            metadata_filters=request.metadata_filters
        )
        
        # Convert to response format
        # Note: We don't have scores here from knowledge_store.search
        # In production, would modify search to return scores
        results = [
            SearchResult(
                id=chunk.id,
                text=chunk.text,
                score=0.9,  # Placeholder - TODO: return actual score
                tags=chunk.tags,
                source_uri=chunk.source_uri
            )
            for chunk in chunks
        ]
        
        return SearchResponse(
            results=results,
            query=request.query,
            count=len(results)
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")


@router.post("/search/hybrid", response_model=SearchResponse)
async def hybrid_search_knowledge(
    request: SearchRequest,
    hybrid_search: PostgresFTSHybridService = Depends(get_hybrid_search)
):
    """
    Hybrid search combining BM25 keyword search with semantic search.
    
    Automatically balances:
    - BM25: Exact keyword matches (great for technical terms, endpoints, constants)
    - Semantic: Conceptual similarity (great for natural language questions)
    
    Uses Reciprocal Rank Fusion (RRF) to merge results from both methods.
    
    Returns chunks that:
    1. Match via BM25 OR semantic search
    2. User has permission to access (ACL filtering)
    3. Are ranked by combined RRF score
    
    Example queries that benefit from hybrid search:
    - "GET /v1/roles" - BM25 finds exact endpoint match
    - "What roles are supported?" - Semantic finds conceptual match
    - "ADMIN role permissions" - Hybrid finds both keyword and concept matches
    """
    try:
        # Build UserContext from request
        user_context = UserContext(
            user_id=request.user_id or "anonymous",
            tenant_id=request.tenant_id,
            system_id=request.system_id,
            roles=request.roles,
            groups=request.groups
        )
        
        # Hybrid search with adaptive weighting
        results_dicts = await hybrid_search.adaptive_search(
            query=request.query,
            user_context=user_context,
            filters=request.metadata_filters,
            top_k=request.top_k,
            score_threshold=request.score_threshold
        )
        
        # Convert to response format
        results = [
            SearchResult(
                id=result["id"],
                text=result["text"],
                score=result["rrf_score"],  # Use RRF score
                tags=[],  # TODO: Include tags from metadata
                source_uri=result.get("metadata", {}).get("source_uri")
            )
            for result in results_dicts
        ]
        
        return SearchResponse(
            results=results,
            query=request.query,
            count=len(results)
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Hybrid search failed: {str(e)}")


@router.post("/ingest/text", response_model=IngestTextResponse)
async def ingest_text(
    request: IngestTextRequest,
    ingestion_service: IngestionService = Depends(get_ingestion_service)
):
    """
    Ingest raw text (e.g., from notes, chat, procedures, temporary notices).
    
    Text will be chunked and stored as multiple knowledge chunks.
    
    **Knowledge Types:**
    - **DOCUMENTATION**: Permanent reference material
    - **PROCEDURE**: Permanent runbooks, guides, lessons learned
    - **EVENT**: Temporary notices (must set expires_at!)
    
    **Examples:**
    
    Lesson learned:
    ```json
    {
      "text": "Lesson learned: Always check ArgoCD sync status before checking K8s pods...",
      "knowledge_type": "procedure",
      "tags": ["lesson-learned", "deployment"]
    }
    ```
    
    Temporary notice:
    ```json
    {
      "text": "Maintenance Window: DC-WEST network upgrade tonight 11 PM - 1 AM. All vCenter APIs will be unavailable.",
      "knowledge_type": "event",
      "expires_at": "2025-11-17T02:00:00Z",
      "priority": 50,
      "tags": ["maintenance", "dc-west", "notice"]
    }
    ```
    """
    try:
        chunk_ids = await ingestion_service.ingest_text(
            text=request.text,
            tenant_id=request.tenant_id,
            system_id=request.system_id,
            user_id=request.user_id,
            roles=request.roles,
            groups=request.groups,
            tags=request.tags,
            source_uri=request.source_uri,
            # Lifecycle fields
            knowledge_type=request.knowledge_type,
            priority=request.priority,
            expires_at=request.expires_at
        )
        
        return IngestTextResponse(
            chunk_ids=chunk_ids,
            count=len(chunk_ids)
        )
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Text ingestion failed: {str(e)}")


@router.post("/ingest/document", response_model=IngestDocumentResponse)
async def ingest_document(
    file: UploadFile = File(...),
    metadata: str = Form(...),
    ingestion_service: IngestionService = Depends(get_ingestion_service)
):
    """
    Ingest a document (PDF, DOCX, HTML, text).
    
    Document will be:
    1. Stored in object storage (MinIO/S3)
    2. Text extracted
    3. Chunked
    4. Embedded and stored in knowledge base
    
    Metadata should be JSON string with optional fields:
    {
        "tenant_id": "...",
        "system_id": "...",
        "user_id": "...",
        "roles": [...],
        "groups": [...],
        "tags": [...]
    }
    """
    try:
        # Parse metadata JSON
        meta = json.loads(metadata)
        
        # Read file
        file_bytes = await file.read()
        
        # Parse knowledge_type if provided
        knowledge_type_str = meta.get("knowledge_type", "documentation")
        try:
            knowledge_type = KnowledgeType(knowledge_type_str)
        except ValueError:
            knowledge_type = KnowledgeType.DOCUMENTATION
        
        # Ingest
        chunk_ids = await ingestion_service.ingest_document(
            file_bytes=file_bytes,
            filename=file.filename or "unknown",
            mime_type=file.content_type or "application/octet-stream",
            tenant_id=meta.get("tenant_id"),
            system_id=meta.get("system_id"),
            user_id=meta.get("user_id"),
            roles=meta.get("roles", []),
            groups=meta.get("groups", []),
            tags=meta.get("tags", []),
            # Lifecycle fields
            knowledge_type=knowledge_type,
            priority=meta.get("priority", 0)
        )
        
        return IngestDocumentResponse(
            chunk_ids=chunk_ids,
            count=len(chunk_ids),
            document_uri=f"ingested:{file.filename or 'unknown'}"
        )
    
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid metadata JSON")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Document ingestion failed: {str(e)}")


@router.get("/jobs/active", response_model=list[IngestionJob])
async def get_active_jobs(
    tenant_id: str = None,
    job_repository: IngestionJobRepository = Depends(get_job_repository)
):
    """
    Get all currently active (processing) jobs (Session 30 - Task 29).
    
    Useful for the global job monitor in the frontend to show upload progress
    even when navigating away from the Knowledge page.
    
    Args:
        tenant_id: Optional tenant filter
        
    Returns:
        List of jobs with status 'processing', with detailed progress information
    """
    try:
        jobs = await job_repository.get_active_jobs(tenant_id=tenant_id)
        
        # Convert to schema objects
        return [IngestionJob.model_validate(job) for job in jobs]
    
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch active jobs: {str(e)}"
        )


@router.get("/jobs/{job_id}", response_model=IngestionJob)
async def get_job(
    job_id: str,
    job_repository: IngestionJobRepository = Depends(get_job_repository)
):
    """
    Get a specific ingestion job by ID.
    
    Returns job status, progress, and error information if any.
    """
    try:
        job = await job_repository.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return IngestionJob.model_validate(job)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch job: {str(e)}"
        )


@router.get("/chunks", response_model=list[ChunkResponse])
async def list_chunks(
    tenant_id: str = None,
    system_id: str = None,
    knowledge_type: str = None,
    limit: int = 50,
    knowledge_store: KnowledgeStore = Depends(get_knowledge_store)
):
    """
    List knowledge chunks with optional filters.
    
    Args:
        tenant_id: Filter by tenant
        system_id: Filter by system
        knowledge_type: Filter by type (documentation, procedure, event)
        limit: Max results to return
    """
    try:
        # Create proper filter object
        from meho_knowledge.schemas import KnowledgeChunkFilter, KnowledgeType
        
        # Convert knowledge_type string to enum if provided
        knowledge_type_enum = None
        if knowledge_type:
            knowledge_type_enum = KnowledgeType(knowledge_type)
        
        filter_obj = KnowledgeChunkFilter(
            tenant_id=tenant_id,
            system_id=system_id,
            knowledge_type=knowledge_type_enum,
            limit=limit
        )
        
        # Get chunks from repository
        from meho_knowledge.repository import KnowledgeRepository
        from meho_knowledge.database import get_session
        
        async for session in get_session():
            repository = KnowledgeRepository(session)
            chunks = await repository.list_chunks(filter_params=filter_obj)
            return [ChunkResponse(**chunk.model_dump()) for chunk in chunks]
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list chunks: {str(e)}"
        )


@router.get("/documents", response_model=list[IngestionJob])
async def list_documents(
    tenant_id: str = None,
    status_filter: str = None,
    limit: int = 50,
    job_repository: IngestionJobRepository = Depends(get_job_repository)
):
    """
    List ingestion jobs (documents) with optional filters.
    
    Args:
        tenant_id: Filter by tenant
        status_filter: Filter by job status (pending, processing, completed, failed)
        limit: Max results to return
    """
    try:
        # Create proper filter object
        filter_obj = IngestionJobFilter(
            tenant_id=tenant_id,
            status=status_filter,
            limit=limit
        )
        
        jobs = await job_repository.list_jobs(filter=filter_obj)
        return [IngestionJob.model_validate(job) for job in jobs]
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list documents: {str(e)}"
        )


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: str,
    job_repository: IngestionJobRepository = Depends(get_job_repository),
    knowledge_store: KnowledgeStore = Depends(get_knowledge_store)
):
    """
    Delete a document and all its associated chunks.
    
    This marks the job as deleted and removes associated chunks.
    """
    try:
        # Get job
        job = await job_repository.get_job(document_id)
        if not job:
            raise HTTPException(status_code=404, detail="Document not found")
        
        # Delete all chunks associated with this job
        # Fixed in TASK-52: KnowledgeChunkFilter now supports source_uri filtering
        if job.chunks_created and job.chunks_created > 0:
            from meho_knowledge.repository import KnowledgeRepository
            from meho_knowledge.database import get_session
            from meho_knowledge.schemas import KnowledgeChunkFilter
            
            async for session in get_session():
                repository = KnowledgeRepository(session)
                # Filter chunks by source_uri (job:document_id format)
                source_uri = f"job:{document_id}"
                filter_params = KnowledgeChunkFilter(
                    source_uri=source_uri,
                    limit=1000  # Reasonable batch size for deletion
                )
                chunks = await repository.list_chunks(filter_params=filter_params)
                
                # Delete each chunk
                for chunk in chunks:
                    await knowledge_store.delete_chunk(chunk.id)
        
        # Mark job as deleted
        await job_repository.mark_deleted(document_id)
        
        return None
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete document: {str(e)}"
        )


@router.delete("/chunks/{chunk_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chunk(
    chunk_id: str,
    knowledge_store: KnowledgeStore = Depends(get_knowledge_store)
):
    """Delete a knowledge chunk."""
    deleted = await knowledge_store.delete_chunk(chunk_id)
    
    if not deleted:
        raise HTTPException(status_code=404, detail="Chunk not found")
    
    return None


# /admin/reconcile endpoint removed - no longer needed with pgvector
# Previously checked PostgreSQL<->Qdrant sync, but we now use pgvector exclusively
# Migration: Session 15 (2025-11-20)


@router.get("/health")
async def health_check():
    """
    Health check endpoint for knowledge service.
    
    Checks:
    - Database connection (PostgreSQL with pgvector)
    - pgvector extension availability
    
    Returns:
        Health status with detailed checks
        
    Note: Simplified after Session 15 pgvector migration.
    No Qdrant, no sync checks - single database architecture.
    """
    from meho_knowledge.database import get_single_session
    from sqlalchemy import text
    
    health = {
        "service": "meho-knowledge",
        "status": "healthy",
        "version": "0.1.0",
        "architecture": "pgvector",  # Single database with vector extension
        "checks": {}
    }
    
    # Check PostgreSQL with pgvector
    try:
        async with get_single_session()() as session:
            # Test basic connection
            await session.execute(text("SELECT 1"))
            
            # Test pgvector extension is available
            result = await session.execute(
                text("SELECT COUNT(*) FROM pg_extension WHERE extname = 'vector'")
            )
            has_pgvector = result.scalar() > 0
            
            # Check knowledge chunks table exists
            result = await session.execute(
                text("""
                    SELECT COUNT(*) 
                    FROM information_schema.tables 
                    WHERE table_name = 'knowledge_chunk'
                """)
            )
            has_table = result.scalar() > 0
            
            health["checks"]["postgres"] = {
                "status": "healthy",
                "message": "Connection successful",
                "pgvector_enabled": has_pgvector,
                "schema_ready": has_table
            }
            
            if not has_pgvector:
                health["status"] = "degraded"
                health["checks"]["postgres"]["warning"] = "pgvector extension not found"
            
            if not has_table:
                health["status"] = "degraded"
                health["checks"]["postgres"]["warning"] = "knowledge_chunk table not found - run migrations"
                
    except Exception as e:
        health["checks"]["postgres"] = {
            "status": "unhealthy",
            "error": str(e)
        }
        health["status"] = "unhealthy"
    
    return health


@router.get("/debug/chunk/{chunk_id}")
async def debug_chunk(
    chunk_id: str,
    knowledge_store: KnowledgeStore = Depends(get_knowledge_store)
):
    """
    Get detailed debug info for a specific chunk.
    
    Shows:
    - PostgreSQL data (including search_metadata and vector embedding)
    
    Note: Simplified after Session 15 pgvector migration.
    Previously checked Qdrant sync, now single-database architecture.
    
    Args:
        chunk_id: UUID of the chunk
    
    Returns:
        Detailed debug information
    """
    from meho_knowledge.database import get_single_session
    from meho_knowledge.models import KnowledgeChunkModel
    from sqlalchemy import select
    from uuid import UUID
    
    debug_info = {"chunk_id": chunk_id}
    
    # Check PostgreSQL
    try:
        async with get_single_session()() as session:
            result = await session.execute(
                select(KnowledgeChunkModel).where(
                    KnowledgeChunkModel.id == UUID(chunk_id)
                )
            )
            db_chunk = result.scalar_one_or_none()
            
            if db_chunk:
                debug_info["postgres"] = {
                    "exists": True,
                    "text_length": len(db_chunk.text),
                    "text_preview": db_chunk.text[:200],
                    "search_metadata": db_chunk.search_metadata,
                    "search_metadata_type": type(db_chunk.search_metadata).__name__,
                    "search_metadata_is_null": db_chunk.search_metadata is None,
                    "tenant_id": db_chunk.tenant_id,
                    "tags": db_chunk.tags,
                    "knowledge_type": db_chunk.knowledge_type,
                    "created_at": db_chunk.created_at.isoformat() if db_chunk.created_at else None
                }
            else:
                debug_info["postgres"] = {"exists": False}
    except Exception as e:
        debug_info["postgres"] = {"error": str(e)}
    
    # Note: Qdrant sync checks removed after Session 15 pgvector migration.
    # Vector embeddings now stored in PostgreSQL pgvector column.
    # All data (text + metadata + vectors) in single database.
    
    return debug_info


@router.get("/debug/recent-chunks")
async def debug_recent_chunks(limit: int = 5):
    """
    Get debug info for recently created chunks.
    
    Helps debug why metadata isn't being stored properly.
    
    Args:
        limit: Number of recent chunks to return (default: 5)
    
    Returns:
        List of recent chunks with metadata info
    """
    from meho_knowledge.database import get_single_session
    from meho_knowledge.models import KnowledgeChunkModel
    from sqlalchemy import select
    
    async with get_single_session()() as session:
        result = await session.execute(
            select(KnowledgeChunkModel)
            .order_by(KnowledgeChunkModel.created_at.desc())
            .limit(limit)
        )
        chunks = result.scalars().all()
        
        return [
            {
                "id": str(chunk.id),
                "text_preview": chunk.text[:100],
                "search_metadata": chunk.search_metadata,
                "search_metadata_type": str(type(chunk.search_metadata)),
                "search_metadata_is_null": chunk.search_metadata is None,
                "has_metadata_keys": list(chunk.search_metadata.keys()) if chunk.search_metadata and isinstance(chunk.search_metadata, dict) else None,
                "created_at": chunk.created_at.isoformat() if chunk.created_at else None
            }
            for chunk in chunks
        ]


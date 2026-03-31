"""
Knowledge management routes for MEHO API.

Proxies requests to knowledge service with authentication.
"""
# mypy: disable-error-code="no-untyped-def,arg-type,var-annotated"
from fastapi import APIRouter, Depends, File, UploadFile, Form, Query, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
import httpx
import json
import os
from meho_core.auth_context import UserContext
from meho_core.structured_logging import get_logger
from meho_api.auth import get_current_user
from meho_api.config import get_api_config

logger = get_logger(__name__)


router = APIRouter(prefix="/knowledge", tags=["knowledge"])

# Document preview length in characters
DOCUMENT_PREVIEW_LENGTH = 600


class SearchRequest(BaseModel):
    """Knowledge search request"""
    query: str
    top_k: int = 10


class UploadResponse(BaseModel):
    """Document upload response"""
    job_id: str
    status: str


class IngestTextRequest(BaseModel):
    """Request to ingest raw text as knowledge"""
    text: str = Field(..., description="Text content to ingest")
    knowledge_type: str = Field(default="procedure", description="Type: documentation, procedure, or event")
    tags: List[str] = Field(default_factory=list, description="Tags for categorization")
    priority: int = Field(default=0, description="Search ranking priority (-100 to +100)")
    expires_at: Optional[datetime] = Field(None, description="Expiration date (required for event type)")
    system_id: Optional[str] = Field(None, description="System ID for system-scoped knowledge")
    scope: str = Field(default="tenant", description="ACL scope: global, tenant, system, team, or private")


class IngestTextResponse(BaseModel):
    """Response from text ingestion"""
    chunk_ids: List[str]
    count: int


class KnowledgeChunkResponse(BaseModel):
    """Response with knowledge chunk details"""
    id: str
    text: str
    tenant_id: Optional[str]
    system_id: Optional[str]
    user_id: Optional[str]
    roles: List[str] = []
    groups: List[str] = []
    tags: List[str]
    knowledge_type: str
    priority: int
    created_at: datetime
    updated_at: datetime
    expires_at: Optional[datetime]
    source_uri: Optional[str]


class ListChunksResponse(BaseModel):
    """Response with list of knowledge chunks"""
    chunks: List[KnowledgeChunkResponse]
    total: int


class KnowledgeDocumentResponse(BaseModel):
    """Document-level view of an ingestion job and its resulting chunks."""
    id: str
    filename: Optional[str]
    knowledge_type: str
    status: str
    tags: List[str] = Field(default_factory=list)
    file_size: Optional[int]
    total_chunks: Optional[int]
    chunks_created: int
    chunks_processed: int
    preview_text: Optional[str] = None
    error: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime]
    progress: Optional[Dict[str, Any]] = None  # Session 44: Real-time progress for UI


class ListDocumentsResponse(BaseModel):
    """Paginated list of knowledge documents."""
    documents: List[KnowledgeDocumentResponse]
    total: int


@router.post("/search")
async def search_knowledge(
    request: SearchRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Search knowledge base via HTTP.
    
    Proxies to knowledge service.
    """
    from meho_api.http_clients import get_knowledge_client
    
    knowledge_client = get_knowledge_client()
    
    try:
        response = await knowledge_client.search(
            query=request.query,
            user_context=user,
            top_k=request.top_k,
            search_mode="semantic"
        )
        return response
    except httpx.HTTPStatusError as e:
        logger.error(f"Knowledge search failed: {e.response.status_code} - {e.response.text}")
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except Exception as e:
        logger.error(f"Knowledge search request failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload", response_model=UploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    knowledge_type: str = Form(default="documentation"),
    tags: str = Form(default="[]"),  # JSON array as string
    user: UserContext = Depends(get_current_user)
):
    """
    Upload document to knowledge base.
    
    **PDF ONLY** - HTML and DOCX files should be converted to PDF first.
    This ensures consistent quality and avoids duplicate content issues.
    
    Returns job_id for progress tracking!
    Frontend should poll GET /knowledge/jobs/{job_id} for progress.
    
    For large documents, processing happens in background to avoid timeout.
    """
    from meho_api.database import create_bff_session_maker
    from meho_knowledge.job_repository import IngestionJobRepository
    from uuid import uuid4
    import asyncio
    
    # Validate file type - PDF only
    if not file.filename or not file.filename.lower().endswith('.pdf'):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are supported. Please convert HTML/DOCX to PDF first."
        )
    
    # Parse tags
    tag_list = json.loads(tags) if tags else []
    
    # Read file content immediately
    file_content = await file.read()
    file_size_mb = len(file_content) / 1024 / 1024
    
    # Validate it's actually a PDF by checking magic bytes and basic structure
    if not file_content.startswith(b'%PDF'):
        raise HTTPException(
            status_code=400,
            detail="File is not a valid PDF. Please upload a PDF file."
        )
    
    # Additional validation: Check for EOF marker (basic PDF structure validation)
    # Note: This catches obviously corrupted PDFs but doesn't replace full PyPDF2 validation
    # which happens during extraction. This is a fast pre-check to fail early.
    if b'%%EOF' not in file_content[-1024:]:  # EOF should be near end of file
        raise HTTPException(
            status_code=400,
            detail="PDF file appears to be corrupted (missing EOF marker). Please check the file."
        )
    
    session_maker = create_bff_session_maker()
    
    async with session_maker() as session:
        # Create job for tracking
        from meho_knowledge.job_schemas import IngestionJobCreate
        
        job_repo = IngestionJobRepository(session)
        job_create = IngestionJobCreate(
            job_type="document",
            tenant_id=user.tenant_id,
            filename=file.filename,
            file_size=len(file_content),
            knowledge_type=knowledge_type,
            tags=tag_list
        )
        job = await job_repo.create_job(job_create)
        job_id = str(job.id)
        await session.commit()
    
    # Start background processing (don't await for large files)
    if file_size_mb > 1.0:
        # For large files (>1MB), process in background task
        asyncio.create_task(_process_document_background(
            job_id=job_id,
            file_content=file_content,
            filename=file.filename,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            tag_list=tag_list,
            knowledge_type=knowledge_type
        ))
        
        return UploadResponse(
            job_id=job_id,
            status="processing"
        )
    else:
        # For small files (<1MB), process immediately
        result = await _process_document_sync(
            job_id=job_id,
            file_content=file_content,
            filename=file.filename,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            tag_list=tag_list,
            knowledge_type=knowledge_type
        )
        
        return UploadResponse(
            job_id=job_id,
            status=result["status"]
        )


async def _process_document_sync(
    job_id: str,
    file_content: bytes,
    filename: str,
    tenant_id: str,
    user_id: str,
    tag_list: list,
    knowledge_type: str
) -> dict:
    """Process document synchronously (for small files)"""
    from meho_api.database import create_bff_session_maker
    from meho_knowledge.repository import KnowledgeRepository
    from meho_knowledge.embeddings import OpenAIEmbeddings
    from meho_knowledge.knowledge_store import KnowledgeStore
    from meho_knowledge.job_repository import IngestionJobRepository
    from meho_knowledge.ingestion import IngestionService
    from meho_knowledge.object_storage import ObjectStorage
    import os
    
    session_maker = create_bff_session_maker()
    
    async with session_maker() as session:
        job_repo = IngestionJobRepository(session)
        
        try:
            # PDF only - MIME type is always application/pdf
            mime_type = 'application/pdf'
            
            # Use the proper IngestionService with metadata extraction
            from meho_knowledge.ingestion import IngestionService
            from meho_knowledge.object_storage import ObjectStorage
            
            # Create dependencies
            repository = KnowledgeRepository(session)
            # No VectorStore needed - using pgvector!
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OPENAI_API_KEY not configured")
            embeddings = OpenAIEmbeddings(api_key=api_key)
            knowledge_store = KnowledgeStore(repository, embeddings)  # pgvector: no separate vector_store
            object_storage = ObjectStorage()  # Gets config from environment
            
            # Create ingestion service with metadata extraction
            ingestion_service = IngestionService(
                knowledge_store=knowledge_store,
                object_storage=object_storage,
                job_repository=job_repo
            )
            
            # Ingest with metadata extraction
            # IngestionService now handles job completion internally
            chunk_ids = await ingestion_service.ingest_document(
                file_bytes=file_content,
                filename=filename,
                mime_type=mime_type,
                tenant_id=tenant_id,
                user_id=user_id,
                tags=tag_list,
                knowledge_type=knowledge_type,
                priority=0,
                job_id=job_id
            )
            
            # Commit session
            await session.commit()
            
            return {"status": "completed", "chunks_created": len(chunk_ids)}
            
        except Exception as e:
            # Mark job as failed
            await job_repo.fail_job(job_id=job_id, error=str(e))
            await session.commit()
            raise


async def _process_document_background(
    job_id: str,
    file_content: bytes,
    filename: str,
    tenant_id: str,
    user_id: str,
    tag_list: list,
    knowledge_type: str
):
    """Process document in background (for large files)"""
    try:
        await _process_document_sync(
            job_id=job_id,
            file_content=file_content,
            filename=filename,
            tenant_id=tenant_id,
            user_id=user_id,
            tag_list=tag_list,
            knowledge_type=knowledge_type
        )
    except Exception as e:
        # Log error but don't fail the request (it already returned)
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Background document processing failed for job {job_id}: {e}", exc_info=True)


@router.get("/jobs/active")
async def get_active_jobs(
    user: UserContext = Depends(get_current_user)
):
    """
    Get all currently active (processing) jobs for the current user's tenant via HTTP.
    
    Used by GlobalJobMonitor to show upload/deletion progress from any page.
    """
    from meho_api.http_clients import get_knowledge_client
    
    knowledge_client = get_knowledge_client()
    
    try:
        jobs = await knowledge_client.list_active_jobs(tenant_id=user.tenant_id)
        return jobs
    except httpx.HTTPStatusError as e:
        logger.error(f"Get active jobs failed: {e.response.status_code}")
        raise HTTPException(status_code=e.response.status_code, detail="Failed to get active jobs")
    except Exception as e:
        logger.error(f"Get active jobs failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get active jobs")


@router.get("/jobs/{job_id}")
async def get_upload_job_status(
    job_id: str,
    user: UserContext = Depends(get_current_user)
):
    """
    Get document upload/ingestion job status via HTTP.
    
    Returns progress information for frontend progress bars.
    """
    from meho_api.http_clients import get_knowledge_client
    
    knowledge_client = get_knowledge_client()
    
    try:
        job = await knowledge_client.get_job(job_id)
        
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        
        # Verify tenant access (BFF-level ACL)
        if job.get("tenant_id") != user.tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        return job
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        logger.error(f"Get job status failed: {e.response.status_code}")
        raise HTTPException(status_code=e.response.status_code, detail="Failed to get job status")
    except Exception as e:
        logger.error(f"Get job status failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get job status")


@router.post("/ingest-text", response_model=IngestTextResponse)
async def ingest_text(
    request: IngestTextRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Ingest raw text as knowledge (procedures, lessons learned, temporary notices).
    
    Examples:
    
    **Lesson Learned (Procedure):**
    ```json
    {
      "text": "Lesson learned: Always check ArgoCD sync status before checking K8s pods...",
      "knowledge_type": "procedure",
      "tags": ["lesson-learned", "deployment"],
      "scope": "team"
    }
    ```
    
    **Temporary Notice (Event):**
    ```json
    {
      "text": "Marathon tomorrow Nov 17th, all streets closed 6 AM - 6 PM",
      "knowledge_type": "event",
      "expires_at": "2025-11-17T18:00:00Z",
      "priority": 50,
      "tags": ["notice", "marathon"],
      "scope": "tenant"
    }
    ```
    """
    from meho_api.http_clients import get_knowledge_client
    
    # Build ACL based on scope
    roles = []
    groups = []
    user_id_override = None
    system_id = request.system_id
    
    if request.scope == "private":
        user_id_override = user.user_id
    elif request.scope == "team":
        groups = user.groups
    elif request.scope == "system":
        if not system_id:
            raise HTTPException(status_code=400, detail="system_id required for system scope")
    
    # Prepare expires_at string if present
    expires_at_str = None
    if request.expires_at:
        expires_at_str = request.expires_at.isoformat()
    
    knowledge_client = get_knowledge_client()
    
    try:
        response = await knowledge_client.ingest_text(
            text=request.text,
            tenant_id=user.tenant_id if request.scope != "global" else None,
            user_id=user_id_override,
            system_id=system_id,
            roles=roles,
            groups=groups,
            tags=request.tags,
            knowledge_type=request.knowledge_type,
            priority=request.priority,
            expires_at=expires_at_str
        )
        return IngestTextResponse(
            chunk_ids=response["chunk_ids"],
            count=response["count"]
        )
    except httpx.HTTPStatusError as e:
        logger.error(f"Text ingestion failed: {e.response.status_code} - {e.response.text}")
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except Exception as e:
        logger.error(f"Text ingestion request failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chunks", response_model=ListChunksResponse)
async def list_chunks(
    knowledge_type: Optional[str] = Query(None, description="Filter by knowledge type"),
    tags: Optional[str] = Query(None, description="Comma-separated tags"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(get_current_user)
):
    """
    List knowledge chunks with filters via HTTP.
    
    Returns chunks accessible to the user based on ACL.
    """
    from meho_api.http_clients import get_knowledge_client
    
    knowledge_client = get_knowledge_client()
    
    try:
        # Get chunks via HTTP
        chunks = await knowledge_client.list_chunks(
            tenant_id=user.tenant_id,
            knowledge_type=knowledge_type,
            limit=limit
        )
        
        # Convert to response format
        chunk_responses = []
        for chunk in chunks:
            chunk_responses.append(KnowledgeChunkResponse(
                id=chunk.get("id"),
                text=chunk.get("text"),
                tenant_id=chunk.get("tenant_id"),
                system_id=chunk.get("system_id"),
                user_id=chunk.get("user_id"),
                roles=chunk.get("roles", []),
                groups=chunk.get("groups", []),
                tags=chunk.get("tags", []),
                knowledge_type=chunk.get("knowledge_type"),
                priority=chunk.get("priority", 0),
                created_at=datetime.fromisoformat(chunk["created_at"]) if isinstance(chunk.get("created_at"), str) else chunk.get("created_at"),
                updated_at=datetime.fromisoformat(chunk["updated_at"]) if isinstance(chunk.get("updated_at"), str) else chunk.get("updated_at"),
                expires_at=datetime.fromisoformat(chunk["expires_at"]) if chunk.get("expires_at") and isinstance(chunk.get("expires_at"), str) else chunk.get("expires_at"),
                source_uri=chunk.get("source_uri")
            ))
        
        return ListChunksResponse(
            chunks=chunk_responses,
            total=len(chunk_responses)
        )
    except httpx.HTTPStatusError as e:
        logger.error(f"List chunks failed: {e.response.status_code}")
        raise HTTPException(status_code=e.response.status_code, detail="Failed to list chunks")
    except Exception as e:
        logger.error(f"List chunks failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to list chunks")


@router.get("/documents", response_model=ListDocumentsResponse)
async def list_documents(
    status: Optional[str] = Query(None, description="Filter by ingestion job status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(get_current_user)
):
    """
    List uploaded documents (ingestion jobs) via HTTP.
    
    Returns metadata and basic info about each document.
    Preview text can be fetched separately if needed.
    """
    from meho_api.http_clients import get_knowledge_client
    
    knowledge_client = get_knowledge_client()
    
    try:
        # Get documents via HTTP
        jobs = await knowledge_client.list_documents(
            tenant_id=user.tenant_id,
            status_filter=status,
            limit=limit
        )
        
        # Convert to response format
        documents = []
        for job in jobs:
            # Session 44: Include progress data for real-time UI updates
            progress = None
            if job.get("status") == "processing":
                progress = {
                    "total_chunks": job.get("total_chunks", 0),
                    "chunks_processed": job.get("chunks_processed", 0),
                    "chunks_created": job.get("chunks_created", 0),
                    "percent": job.get("stage_progress", 0) * 100 if job.get("stage_progress") else 0,
                    "current_stage": job.get("current_stage"),
                    "stage_progress": job.get("stage_progress"),
                    "overall_progress": job.get("overall_progress"),
                    "status_message": job.get("status_message"),
                    "estimated_completion": job.get("estimated_completion")
                }
            
            documents.append(KnowledgeDocumentResponse(
                id=job.get("id"),
                filename=job.get("filename"),
                knowledge_type=job.get("knowledge_type"),
                status=job.get("status"),
                tags=job.get("tags", []),
                file_size=job.get("file_size"),
                total_chunks=job.get("total_chunks"),
                chunks_created=job.get("chunks_created", 0),
                chunks_processed=job.get("chunks_processed", 0),
                preview_text=None,  # Can be fetched separately if needed
                error=job.get("error"),
                started_at=datetime.fromisoformat(job["started_at"]) if job.get("started_at") else datetime.utcnow(),
                completed_at=datetime.fromisoformat(job["completed_at"]) if job.get("completed_at") else None,
                progress=progress  # Session 44: Include progress for UI display
            ))
        
        return ListDocumentsResponse(
            documents=documents,
            total=len(documents)  # Simplified for now, can add count endpoint later
        )
    except httpx.HTTPStatusError as e:
        logger.error(f"List documents failed: {e.response.status_code}")
        raise HTTPException(status_code=e.response.status_code, detail="Failed to list documents")
    except Exception as e:
        logger.error(f"List documents failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to list documents")


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: str,
    user: UserContext = Depends(get_current_user)
):
    """
    Delete an uploaded document and all of its chunks via HTTP.
    
    Delegates deletion to the knowledge service which handles:
    - Deleting chunks from PostgreSQL
    - Rebuilding BM25 index
    - Cleaning up storage
    
    Returns immediately after initiating deletion.
    """
    from meho_api.http_clients import get_knowledge_client
    
    knowledge_client = get_knowledge_client()
    
    try:
        # Get job first to verify ownership
        job = await knowledge_client.get_job(document_id)
        
        if not job:
            raise HTTPException(status_code=404, detail="Document not found")
        
        # Verify tenant access (BFF-level ACL)
        if job.get("tenant_id") != user.tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Delete via HTTP
        await knowledge_client.delete_document(document_id)
        
        return {
            "message": "Document deleted successfully",
            "document_id": document_id
        }
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        logger.error(f"Delete document failed: {e.response.status_code}")
        raise HTTPException(status_code=e.response.status_code, detail="Failed to delete document")
    except Exception as e:
        logger.error(f"Delete document failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete document")


async def _delete_document_background(
    document_id: str,
    deletion_job_id: str,
    chunk_ids: List[str],
    tenant_id: str,
    total_chunks: int
):
    """
    Background task to delete document with progress tracking (Session 30).
    
    Runs asynchronously so the API returns immediately while deletion proceeds.
    
    Stages:
    1. PREPARING - 5%
    2. DELETING_CHUNKS - 5-75% (batch delete)
    3. UPDATING_INDEX - 75-95% (rebuild BM25)
    4. CLEANUP_STORAGE - 95-100% (delete original file)
    """
    from meho_api.database import create_bff_session_maker
    from meho_knowledge.job_repository import IngestionJobRepository
    from meho_knowledge.repository import KnowledgeRepository
    from meho_knowledge.job_models import DeletionStage
    
    session_maker = create_bff_session_maker()
    deletion_succeeded = False
    
    try:
        async with session_maker() as session:
            job_repo = IngestionJobRepository(session)
            knowledge_repo = KnowledgeRepository(session)
            
            # Stage 1: Preparing (5%)
            await job_repo.update_stage(
                job_id=deletion_job_id,
                current_stage=DeletionStage.PREPARING.value,
                stage_progress=1.0,
                overall_progress=0.05,
                status_message=f"Preparing to delete {total_chunks} chunks..."
            )
            
            # Stage 2: Deleting chunks (70% of time) - batch delete for efficiency!
            await job_repo.update_stage(
                job_id=deletion_job_id,
                current_stage=DeletionStage.DELETING_CHUNKS.value,
                stage_progress=0.0,
                overall_progress=0.05,
                status_message=f"Deleting {total_chunks} chunks..."
            )
            
            # Batch delete (much faster than one-by-one!)
            deleted_count = await knowledge_repo.delete_chunks_batch(chunk_ids)
            
            await job_repo.update_stage(
                job_id=deletion_job_id,
                current_stage=DeletionStage.DELETING_CHUNKS.value,
                stage_progress=1.0,
                overall_progress=0.75,
                status_message=f"Deleted {deleted_count} chunks from database"
            )
            
            # Stage 3: Search indexes (auto-maintained by PostgreSQL)
            await job_repo.update_stage(
                job_id=deletion_job_id,
                current_stage=DeletionStage.UPDATING_INDEX.value,
                stage_progress=1.0,
                overall_progress=0.95,
                status_message="Search indexes auto-updated (PostgreSQL FTS)"
            )
            
            # NOTE: No manual index rebuilding needed!
            # PostgreSQL FTS indexes are automatically maintained on DELETE operations.
            logger.info(f"PostgreSQL FTS indexes automatically updated after deleting {deleted_count} chunks")
            
            # Stage 4: Cleanup complete (100%)
            await job_repo.update_stage(
                job_id=deletion_job_id,
                current_stage=DeletionStage.COMPLETED.value,
                stage_progress=1.0,
                overall_progress=1.0,
                status_message=f"Deletion complete - {deleted_count} chunks removed"
            )
            
            # Mark deletion job as complete
            await job_repo.complete_job(deletion_job_id, [])
            
            logger.info(f"Document deletion complete: {deleted_count} chunks deleted")
            
            # Mark deletion as successful - only now is it safe to delete the original job
            deletion_succeeded = True
    
    except Exception as e:
        # Mark deletion job as failed
        try:
            async with session_maker() as session:
                job_repo = IngestionJobRepository(session)
                await job_repo.fail_job(
                    job_id=deletion_job_id,
                    error=str(e),
                    error_stage=DeletionStage.DELETING_CHUNKS.value
                )
        except Exception:
            pass  # Best effort
        
        logger.error(f"Document deletion failed: {e}")
    
    finally:
        # Only delete original ingestion job if deletion succeeded
        # Otherwise, user can see the failed deletion job and retry if needed
        if deletion_succeeded:
            try:
                async with session_maker() as session:
                    job_repo = IngestionJobRepository(session)
                    await job_repo.delete_job(document_id)
            except Exception as e:
                logger.warning(f"Failed to delete original job record: {e}")
                # Best effort - deletion already succeeded


@router.delete("/chunks/{chunk_id}")
async def delete_chunk(
    chunk_id: str,
    user: UserContext = Depends(get_current_user)
):
    """
    Delete a knowledge chunk via HTTP.
    
    Users can only delete their own chunks (user_id match).
    """
    from meho_api.http_clients import get_knowledge_client
    
    knowledge_client = get_knowledge_client()
    
    try:
        # Get chunk to verify ownership
        # Note: For now, we'll delegate ACL to the knowledge service
        # In future, we could add a get_chunk endpoint to verify ownership first
        
        # Delete via HTTP
        await knowledge_client.delete_chunk(chunk_id)
        
        return {"message": "Chunk deleted successfully"}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Chunk not found")
        elif e.response.status_code == 403:
            raise HTTPException(status_code=403, detail="Access denied")
        logger.error(f"Delete chunk failed: {e.response.status_code}")
        raise HTTPException(status_code=e.response.status_code, detail="Failed to delete chunk")
    except Exception as e:
        logger.error(f"Delete chunk failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete chunk")
        
        return {"message": "Chunk deleted successfully"}


async def _get_document_preview(job, repository):
    """
    Fetch a short preview snippet for a document by loading the first chunk text.
    """
    if not job.chunk_ids:
        return None
    
    first_chunk_id = str(job.chunk_ids[0])
    chunk = await repository.get_chunk(first_chunk_id)
    
    if not chunk or not chunk.text:
        return None
    
    return chunk.text[:DOCUMENT_PREVIEW_LENGTH]


def _build_document_response(job, preview_text: Optional[str]) -> KnowledgeDocumentResponse:
    """
    Normalize ingestion job model into API response payload.
    """
    return KnowledgeDocumentResponse(
        id=str(job.id),
        filename=job.filename,
        knowledge_type=job.knowledge_type,
        status=job.status,
        tags=job.tags or [],
        file_size=job.file_size,
        total_chunks=job.total_chunks,
        chunks_created=job.chunks_created,
        chunks_processed=job.chunks_processed,
        preview_text=preview_text,
        error=job.error,
        started_at=job.started_at,
        completed_at=job.completed_at
    )


# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Knowledge management routes for MEHO API.

Direct service calls (modular monolith).
"""

# mypy: disable-error-code="no-untyped-def,arg-type,var-annotated"
import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.api.dependencies import CurrentUser, DbSession
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission
from meho_app.database import get_db_session

logger = get_logger(__name__)

CHUNK_PATTERN = r"#chunk=\d+$"
MSG_ACCESS_DENIED = "Access denied"


router = APIRouter(prefix="/knowledge", tags=["knowledge"])

# Document preview length in characters
DOCUMENT_PREVIEW_LENGTH = 600


class SearchRequest(BaseModel):
    """Knowledge search request"""

    query: str
    top_k: int = 10
    connector_id: str | None = (
        None  # If provided, scope to this connector; if None, cross-connector search
    )


class UploadResponse(BaseModel):
    """Document upload response"""

    job_id: str
    status: str


class IngestTextRequest(BaseModel):
    """Request to ingest raw text as knowledge"""

    text: str = Field(..., description="Text content to ingest")
    connector_id: str = Field(
        ...,
        description="Connector ID — every text ingestion must be scoped to a connector",
    )
    knowledge_type: str = Field(
        default="procedure", description="Type: documentation, procedure, or event"
    )
    tags: list[str] = Field(default_factory=list, description="Tags for categorization")
    priority: int = Field(default=0, description="Search ranking priority (-100 to +100)")
    expires_at: datetime | None = Field(
        None, description="Expiration date (required for event type)"
    )
    scope: str = Field(
        default="tenant",
        description="ACL scope: global, tenant, system, team, or private",
    )


class IngestUrlRequest(BaseModel):
    """Request to ingest content from a web URL"""

    url: str = Field(..., description="Web URL to crawl and ingest")
    connector_id: str | None = Field(
        None, description="Connector ID for scoping (optional for global)"
    )
    connector_type_scope: str | None = Field(
        None, description="Connector type for type-level scoping"
    )
    knowledge_type: str = Field(
        default="documentation", description="Type: documentation, procedure"
    )
    tags: list[str] = Field(default_factory=list, description="Tags for categorization")
    scope: str = Field(
        default="tenant",
        description="ACL scope: global, tenant, system, team, or private",
    )


class IngestTextResponse(BaseModel):
    """Response from text ingestion"""

    chunk_ids: list[str]
    count: int


class KnowledgeChunkResponse(BaseModel):
    """Response with knowledge chunk details"""

    id: str
    text: str
    tenant_id: str | None
    connector_id: str | None = None
    connector_name: str | None = None
    connector_type: str | None = None
    user_id: str | None
    roles: list[str] = []
    groups: list[str] = []
    tags: list[str]
    knowledge_type: str
    priority: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    source_uri: str | None


class ListChunksResponse(BaseModel):
    """Response with list of knowledge chunks"""

    chunks: list[KnowledgeChunkResponse]
    total: int


class KnowledgeDocumentResponse(BaseModel):
    """Document-level view of an ingestion job and its resulting chunks."""

    id: str
    filename: str | None
    knowledge_type: str
    status: str
    tags: list[str] = Field(default_factory=list)
    file_size: int | None
    total_chunks: int | None
    chunks_created: int
    chunks_processed: int
    preview_text: str | None = None
    error: str | None = None
    started_at: datetime
    completed_at: datetime | None
    progress: dict[str, Any] | None = None  # Session 44: Real-time progress for UI


class ListDocumentsResponse(BaseModel):
    """Paginated list of knowledge documents."""

    documents: list[KnowledgeDocumentResponse]
    total: int


@router.post("/search", responses={500: {"description": "Internal server error"}})
async def search_knowledge(
    request: SearchRequest,
    user: CurrentUser,
    session: DbSession,
):
    """
    Search knowledge base.

    If connector_id is provided, scopes search to that connector only (specialist view).
    If connector_id is omitted, searches across all connectors with attribution (KnowledgePage browse).
    """
    from meho_app.core.auth_context import UserContext
    from meho_app.modules.knowledge.embeddings import get_embedding_provider
    from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
    from meho_app.modules.knowledge.repository import KnowledgeRepository

    try:
        repository = KnowledgeRepository(session)
        embeddings = get_embedding_provider()
        hybrid_search = PostgresFTSHybridService(repository, embeddings)
        knowledge_store = KnowledgeStore(repository, embeddings, hybrid_search)

        # Build user context for ACL
        user_ctx = UserContext(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            roles=user.roles,
            groups=user.groups,
        )

        if request.connector_id:
            # Scoped search: single connector only
            chunks = await knowledge_store.search_by_connector(
                query=request.query,
                user_context=user_ctx,
                connector_id=request.connector_id,
                top_k=request.top_k,
            )
            return {
                "chunks": [
                    {
                        "id": str(chunk.id),
                        "text": chunk.text,
                        "score": 0.0,  # Score not available in chunk-level results
                        "tenant_id": chunk.tenant_id,
                        "connector_id": str(getattr(chunk, "connector_id", None) or ""),
                        "tags": chunk.tags,
                        "knowledge_type": chunk.knowledge_type,
                    }
                    for chunk in chunks
                ],
                "total": len(chunks),
            }
        else:
            # Cross-connector search with attribution
            results = await knowledge_store.search_cross_connector(
                query=request.query,
                user_context=user_ctx,
                top_k=request.top_k,
            )
            return {
                "chunks": [
                    {
                        "id": r["id"],
                        "text": r["text"],
                        "score": r["score"],
                        "tenant_id": user.tenant_id,
                        "connector_id": r["connector_id"],
                        "connector_name": r["connector_name"],
                        "connector_type": r["connector_type"],
                        "tags": r["tags"],
                        "knowledge_type": r["knowledge_type"],
                    }
                    for r in results
                ],
                "total": len(results),
            }
    except Exception as e:
        logger.error(f"Knowledge search request failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/tree", responses={500: {"description": "Internal server error"}})
async def get_knowledge_tree(
    user: CurrentUser,
    session: DbSession,
):
    """
    Get hierarchical knowledge tree: Global > Connector Type > Connector Instance.

    Returns document/chunk counts at each level for the Knowledge page tree view.
    """
    from sqlalchemy import func, select

    from meho_app.modules.connectors.models import ConnectorModel, ConnectorType
    from meho_app.modules.knowledge.models import KnowledgeChunkModel

    try:
        # --- Global counts ---
        global_result = await session.execute(
            select(
                func.count(KnowledgeChunkModel.id).label("chunk_count"),
            ).where(
                KnowledgeChunkModel.tenant_id == user.tenant_id,
                KnowledgeChunkModel.scope_type == "global",
            )
        )
        global_row = global_result.one()
        global_chunk_count = global_row.chunk_count or 0

        # Count distinct source_uri prefixes for global docs (approximate doc count)
        global_doc_result = await session.execute(
            select(
                func.count(
                    func.distinct(
                        func.regexp_replace(KnowledgeChunkModel.source_uri, CHUNK_PATTERN, "", "g")
                    )
                ).label("doc_count"),
            ).where(
                KnowledgeChunkModel.tenant_id == user.tenant_id,
                KnowledgeChunkModel.scope_type == "global",
                KnowledgeChunkModel.source_uri.isnot(None),
            )
        )
        global_doc_count = global_doc_result.scalar() or 0

        # --- Type-level counts ---
        type_results = await session.execute(
            select(
                KnowledgeChunkModel.connector_type_scope,
                func.count(KnowledgeChunkModel.id).label("chunk_count"),
                func.count(
                    func.distinct(
                        func.regexp_replace(KnowledgeChunkModel.source_uri, CHUNK_PATTERN, "", "g")
                    )
                ).label("doc_count"),
            )
            .where(
                KnowledgeChunkModel.tenant_id == user.tenant_id,
                KnowledgeChunkModel.scope_type == "type",
                KnowledgeChunkModel.connector_type_scope.isnot(None),
            )
            .group_by(KnowledgeChunkModel.connector_type_scope)
        )
        type_counts = {
            row.connector_type_scope: {
                "chunk_count": row.chunk_count,
                "doc_count": row.doc_count,
            }
            for row in type_results
        }

        # --- Instance-level counts ---
        instance_results = await session.execute(
            select(
                KnowledgeChunkModel.connector_id,
                func.count(KnowledgeChunkModel.id).label("chunk_count"),
                func.count(
                    func.distinct(
                        func.regexp_replace(KnowledgeChunkModel.source_uri, CHUNK_PATTERN, "", "g")
                    )
                ).label("doc_count"),
            )
            .where(
                KnowledgeChunkModel.tenant_id == user.tenant_id,
                KnowledgeChunkModel.scope_type == "instance",
                KnowledgeChunkModel.connector_id.isnot(None),
            )
            .group_by(KnowledgeChunkModel.connector_id)
        )
        instance_counts = {
            str(row.connector_id): {
                "chunk_count": row.chunk_count,
                "doc_count": row.doc_count,
            }
            for row in instance_results
        }

        # --- Fetch connectors to build tree ---
        connector_results = await session.execute(
            select(
                ConnectorModel.id,
                ConnectorModel.name,
                ConnectorModel.connector_type,
            )
            .where(
                ConnectorModel.tenant_id == user.tenant_id,
            )
            .order_by(ConnectorModel.name)
        )
        connectors = connector_results.all()

        # Group connectors by type
        type_map: dict = {}  # connector_type -> list of instances
        for conn in connectors:
            ct = conn.connector_type
            if ct not in type_map:
                type_map[ct] = []
            type_map[ct].append(
                {
                    "connector_id": str(conn.id),
                    "connector_name": conn.name,
                    "document_count": instance_counts.get(str(conn.id), {}).get("doc_count", 0),
                    "chunk_count": instance_counts.get(str(conn.id), {}).get("chunk_count", 0),
                }
            )

        # Build types array
        connector_type_display_names = {
            "rest": "REST",
            "soap": "SOAP",
            "vmware": "VMware",
            "proxmox": "Proxmox",
            "graphql": "GraphQL",
            "grpc": "gRPC",
            "kubernetes": "Kubernetes",
            "email": "Email",
        }
        types_list = []
        for ct, instances in sorted(type_map.items()):
            type_doc_count = type_counts.get(ct, {}).get("doc_count", 0)
            type_chunk_count = type_counts.get(ct, {}).get("chunk_count", 0)
            types_list.append(
                {
                    "connector_type": ct,
                    "display_name": connector_type_display_names.get(ct, ct.upper()),
                    "document_count": type_doc_count,
                    "chunk_count": type_chunk_count,
                    "instances": instances,
                }
            )

        # Also include types with type-level docs but no connector instances
        for ct in type_counts:
            if ct not in type_map:
                types_list.append(
                    {
                        "connector_type": ct,
                        "display_name": connector_type_display_names.get(ct, ct.upper()),
                        "document_count": type_counts[ct]["doc_count"],
                        "chunk_count": type_counts[ct]["chunk_count"],
                        "instances": [],
                    }
                )

        # All known connector types (for "add knowledge for another type" button)
        all_types = [
            {
                "value": t.value,
                "display_name": connector_type_display_names.get(t.value, t.value.upper()),
            }
            for t in ConnectorType
        ]

        return {
            "global": {
                "document_count": global_doc_count,
                "chunk_count": global_chunk_count,
            },
            "types": types_list,
            "all_connector_types": all_types,
        }
    except Exception as e:
        logger.error(f"Knowledge tree request failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/upload",
    response_model=UploadResponse,
    responses={
        400: {"description": "PDF file appears to be corrupted (missing EOF marker). Pl..."},
        413: {"description": "Request entity too large"},
    },
)
async def upload_document(
    file: Annotated[UploadFile, File(...)],
    user: Annotated[UserContext, Depends(RequirePermission(Permission.KNOWLEDGE_INGEST))],
    connector_id: Annotated[
        str | None, Form()
    ] = None,  # Required for instance scope, null for global/type
    knowledge_type: Annotated[str, Form()] = "documentation",
    tags: Annotated[str, Form()] = "[]",  # JSON array as string
    scope_type: Annotated[str, Form()] = "instance",  # global, type, or instance
    connector_type_scope: Annotated[
        str | None, Form()
    ] = None,  # e.g. "kubernetes" for type-scoped docs
):
    """
    Upload document to knowledge base.

    **PDF ONLY** - HTML and DOCX files should be converted to PDF first.
    This ensures consistent quality and avoids duplicate content issues.

    Returns job_id for progress tracking!
    Frontend should poll GET /knowledge/jobs/{job_id} for progress.

    For large documents, processing happens in background to avoid timeout.
    """
    import asyncio

    from meho_app.api.database import create_bff_session_maker
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository

    # Validate file type - PDF only
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are supported. Please convert HTML/DOCX to PDF first.",
        )

    # Parse tags
    tag_list = json.loads(tags) if tags else []

    # Validate scope parameters
    if scope_type not in ("global", "type", "instance"):
        raise HTTPException(
            status_code=400, detail="scope_type must be 'global', 'type', or 'instance'"
        )
    if scope_type == "instance" and not connector_id:
        raise HTTPException(
            status_code=400,
            detail="connector_id is required for instance-scoped uploads",
        )
    if scope_type == "type" and not connector_type_scope:
        raise HTTPException(
            status_code=400,
            detail="connector_type_scope is required for type-scoped uploads",
        )
    if scope_type == "global":
        connector_id = None  # Ensure no connector_id for global scope
        connector_type_scope = None

    # Read file content immediately
    file_content = await file.read()
    file_size_mb = len(file_content) / 1024 / 1024

    # Validate it's actually a PDF by checking magic bytes and basic structure
    if not file_content.startswith(b"%PDF"):
        raise HTTPException(
            status_code=400, detail="File is not a valid PDF. Please upload a PDF file."
        )

    # Additional validation: Check for EOF marker (basic PDF structure validation)
    # Note: This catches obviously corrupted PDFs but doesn't replace full PyPDF2 validation
    # which happens during extraction. This is a fast pre-check to fail early.
    if b"%%EOF" not in file_content[-1024:]:  # EOF should be near end of file
        raise HTTPException(
            status_code=400,
            detail="PDF file appears to be corrupted (missing EOF marker). Please check the file.",
        )

    # D-06 (Phase 90.2): Reject files exceeding configurable size limit
    from meho_app.core.config import get_config

    max_size_mb = get_config().ingestion_max_file_size_mb
    max_size_bytes = max_size_mb * 1024 * 1024
    if len(file_content) > max_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File size ({file_size_mb:.1f} MB) exceeds maximum allowed size "
                f"({max_size_mb} MB). Set MEHO_INGESTION_MAX_FILE_SIZE_MB to increase the limit."
            ),
        )

    session_maker = create_bff_session_maker()

    async with session_maker() as session:
        # Create job for tracking
        from meho_app.modules.knowledge.job_schemas import IngestionJobCreate

        job_repo = IngestionJobRepository(session)
        job_create = IngestionJobCreate(
            job_type="document",
            tenant_id=user.tenant_id,
            connector_id=connector_id,
            filename=file.filename,
            file_size=len(file_content),
            knowledge_type=knowledge_type,
            tags=tag_list,
        )
        job = await job_repo.create_job(job_create)
        job_id = str(job.id)
        await session.commit()

    # Audit: log knowledge upload
    try:
        from meho_app.modules.audit.service import AuditService

        async with session_maker() as audit_session:
            audit = AuditService(audit_session)
            await audit.log_event(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                user_email=getattr(user, "email", None),
                event_type="knowledge.upload",
                action="create",
                resource_type="knowledge_doc",
                resource_id=job_id,
                resource_name=file.filename,
                details={
                    "connector_id": connector_id,
                    "knowledge_type": knowledge_type,
                    "file_size_bytes": len(file_content),
                    "scope_type": scope_type,
                    "connector_type_scope": connector_type_scope,
                },
                result="success",
            )
            await audit_session.commit()
    except Exception as audit_err:
        logger.warning(f"Audit logging failed for knowledge upload: {audit_err}")

    # Start background processing (don't await for large files)
    if file_size_mb > 1.0:
        # For large files (>1MB), process in background task
        asyncio.create_task(  # noqa: RUF006 -- fire-and-forget task pattern
            _process_document_background(
                job_id=job_id,
                file_content=file_content,
                filename=file.filename,
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                tag_list=tag_list,
                knowledge_type=knowledge_type,
                connector_id=connector_id,
                scope_type=scope_type,
                connector_type_scope=connector_type_scope,
            )
        )

        return UploadResponse(job_id=job_id, status="processing")
    else:
        # For small files (<1MB), process immediately
        result = await _process_document_sync(
            job_id=job_id,
            file_content=file_content,
            filename=file.filename,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            tag_list=tag_list,
            knowledge_type=knowledge_type,
            connector_id=connector_id,
            scope_type=scope_type,
            connector_type_scope=connector_type_scope,
        )

        return UploadResponse(job_id=job_id, status=result["status"])


async def _process_document_sync(
    job_id: str,
    file_content: bytes,
    filename: str,
    tenant_id: str,
    user_id: str,
    tag_list: list,
    knowledge_type: str,
    connector_id: str = "",
    scope_type: str = "instance",
    connector_type_scope: str | None = None,
) -> dict:
    """Process document synchronously (for small files)"""
    from meho_app.api.database import create_bff_session_maker
    from meho_app.modules.knowledge.embeddings import get_embedding_provider
    from meho_app.modules.knowledge.ingestion import IngestionService
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
    from meho_app.modules.knowledge.object_storage import ObjectStorage
    from meho_app.modules.knowledge.repository import KnowledgeRepository

    session_maker = create_bff_session_maker()

    async with session_maker() as session:
        job_repo = IngestionJobRepository(session)

        try:
            # PDF only - MIME type is always application/pdf
            mime_type = "application/pdf"

            # Use the proper IngestionService with metadata extraction
            from meho_app.modules.knowledge.hybrid_search import (
                PostgresFTSHybridService,
            )
            from meho_app.modules.knowledge.ingestion import IngestionService
            from meho_app.modules.knowledge.object_storage import ObjectStorage

            # Create dependencies
            repository = KnowledgeRepository(session)
            embeddings = get_embedding_provider()
            hybrid_search = PostgresFTSHybridService(repository, embeddings)
            knowledge_store = KnowledgeStore(repository, embeddings, hybrid_search)
            object_storage = ObjectStorage()  # Gets config from environment

            # Create ingestion service with metadata extraction
            ingestion_service = IngestionService(
                knowledge_store=knowledge_store,
                object_storage=object_storage,
                job_repository=job_repo,
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
                job_id=job_id,
                connector_id=connector_id,
                scope_type=scope_type,
                connector_type_scope=connector_type_scope,
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
    knowledge_type: str,
    connector_id: str = "",
    scope_type: str = "instance",
    connector_type_scope: str | None = None,
) -> None:
    """Process document in background (for large files)"""
    try:
        await _process_document_sync(
            job_id=job_id,
            file_content=file_content,
            filename=filename,
            tenant_id=tenant_id,
            user_id=user_id,
            tag_list=tag_list,
            knowledge_type=knowledge_type,
            connector_id=connector_id,
            scope_type=scope_type,
            connector_type_scope=connector_type_scope,
        )
    except Exception as e:
        # Log error but don't fail the request (it already returned)
        from meho_app.core.otel import get_logger as _get_logger

        logger = _get_logger(__name__)
        logger.error(
            f"Background document processing failed for job {job_id}: {e}",
            exc_info=True,
        )


@router.get("/jobs/active", responses={500: {"description": "Failed to get active jobs"}})
async def get_active_jobs(
    user: CurrentUser,
    session: DbSession,
):
    """
    Get all currently active (processing) jobs for the current user's tenant.

    Used by GlobalJobMonitor to show upload/deletion progress from any page.
    """
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository
    from meho_app.modules.knowledge.job_schemas import IngestionJobFilter

    try:
        job_repo = IngestionJobRepository(session)
        # Note: Filter status as "processing" to get only active jobs
        # For all pending/processing, we'd need to call twice or modify filter
        job_filter = IngestionJobFilter(
            tenant_id=user.tenant_id,
            status="processing",  # Active jobs only
            limit=100,
        )
        jobs = await job_repo.list_jobs(job_filter)

        # Convert to response format (return array directly for frontend compatibility)
        return [
            {
                "id": str(job.id),
                "filename": job.filename,
                "status": job.status,
                "progress": {
                    "total_chunks": job.total_chunks or 0,
                    "chunks_processed": job.chunks_processed or 0,
                    "chunks_created": job.chunks_created or 0,
                    "percent": (job.overall_progress or 0) * 100,
                    "current_stage": job.current_stage,
                    "stage_progress": job.stage_progress,
                    "overall_progress": job.overall_progress,
                    "status_message": job.status_message,
                    "estimated_completion": job.estimated_completion,
                },
                "chunks_created": job.chunks_created,
                "chunks_processed": job.chunks_processed,
            }
            for job in jobs
        ]
    except Exception as e:
        logger.error(f"Get active jobs failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get active jobs") from e


@router.get(
    "/jobs/{job_id}",
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Job not found"},
        500: {"description": "Failed to get job status"},
    },
)
async def get_upload_job_status(
    job_id: str,
    user: CurrentUser,
    session: DbSession,
):
    """
    Get document upload/ingestion job status.

    Returns progress information for frontend progress bars.
    """
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository

    try:
        job_repo = IngestionJobRepository(session)
        job = await job_repo.get_job(job_id)

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        # Verify tenant access
        if job.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail=MSG_ACCESS_DENIED)

        # Build structured progress dict from model columns
        progress = None
        if job.status == "processing":
            progress = {
                "total_chunks": job.total_chunks or 0,
                "chunks_processed": job.chunks_processed or 0,
                "chunks_created": job.chunks_created or 0,
                "percent": (job.overall_progress or 0) * 100,
                "current_stage": job.current_stage,
                "stage_progress": job.stage_progress,
                "overall_progress": job.overall_progress,
                "status_message": job.status_message,
                "estimated_completion": job.estimated_completion,
            }

        return {
            "id": str(job.id),
            "filename": job.filename,
            "status": job.status,
            "progress": progress,
            "chunks_created": job.chunks_created,
            "chunks_processed": job.chunks_processed,
            "total_chunks": job.total_chunks,
            "error": job.error,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get job status failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get job status") from e


@router.post(
    "/ingest-text",
    response_model=IngestTextResponse,
    responses={500: {"description": "Internal server error"}},
)
async def ingest_text(
    request: IngestTextRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.KNOWLEDGE_INGEST))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
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
    # Note: Direct service calls used - no HTTP clients needed

    # Build ACL based on scope
    roles = []
    groups = []
    user_id_override = None
    connector_id = request.connector_id

    if request.scope == "private":
        user_id_override = user.user_id
    elif request.scope == "team":
        groups = user.groups

    # Prepare expires_at string if present
    if request.expires_at:
        request.expires_at.isoformat()

    try:
        from meho_app.modules.knowledge import get_knowledge_service

        knowledge_svc = get_knowledge_service(session)

        result = await knowledge_svc.ingest_text(
            text=request.text,
            tenant_id=user.tenant_id if request.scope != "global" else None,
            user_id=user_id_override,
            connector_id=connector_id,
            roles=roles,
            groups=groups,
            tags=request.tags,
            knowledge_type=request.knowledge_type,
            priority=request.priority,
        )
        return IngestTextResponse(
            chunk_ids=result.get("chunk_ids", []),
            count=len(result.get("chunk_ids", [])),
        )
    except Exception as e:
        logger.error(f"Text ingestion request failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/ingest-url",
    response_model=UploadResponse,
    responses={400: {"description": "Invalid URL. Must start with http:// or https://"}},
)
async def ingest_url(
    request: IngestUrlRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.KNOWLEDGE_INGEST))],
):
    """
    Ingest content from a web URL into the knowledge base.

    Fetches the URL, extracts text (HTML, plain text, or PDF),
    and processes it through the chunking + embedding pipeline.

    Processing happens in background; returns job_id for status tracking.
    Poll GET /knowledge/jobs/{job_id} for progress.
    """
    import asyncio
    import re

    # Validate URL format
    url = request.url.strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(
            status_code=400, detail="Invalid URL. Must start with http:// or https://"
        )

    from meho_app.api.database import create_bff_session_maker
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository

    session_maker = create_bff_session_maker()

    # Create job for tracking
    async with session_maker() as session:
        from meho_app.modules.knowledge.job_schemas import IngestionJobCreate

        job_repo = IngestionJobRepository(session)
        job_create = IngestionJobCreate(
            job_type="url",
            tenant_id=user.tenant_id,
            connector_id=request.connector_id,
            filename=url,  # Use URL as the "filename" for display
            file_size=0,  # Unknown until fetched
            knowledge_type=request.knowledge_type,
            tags=request.tags,
        )
        job = await job_repo.create_job(job_create)
        job_id = str(job.id)
        await session.commit()

    # Audit: log URL ingestion
    try:
        from meho_app.modules.audit.service import AuditService

        async with session_maker() as audit_session:
            audit = AuditService(audit_session)
            await audit.log_event(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                user_email=getattr(user, "email", None),
                event_type="knowledge.ingest_url",
                action="create",
                resource_type="knowledge_url",
                resource_id=job_id,
                resource_name=url,
                details={
                    "connector_id": request.connector_id,
                    "knowledge_type": request.knowledge_type,
                    "url": url,
                },
                result="success",
            )
            await audit_session.commit()
    except Exception as audit_err:
        logger.warning(f"Audit logging failed for URL ingestion: {audit_err}")

    # Determine scope for URL ingestion
    url_scope_type = "instance"
    url_connector_type_scope = request.connector_type_scope
    if not request.connector_id and not request.connector_type_scope:
        url_scope_type = "global"
    elif not request.connector_id and request.connector_type_scope:
        url_scope_type = "type"

    # Process in background (URL fetch can be slow)
    asyncio.create_task(  # noqa: RUF006 -- fire-and-forget task pattern
        _process_url_background(
            job_id=job_id,
            url=url,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            connector_id=request.connector_id,
            tag_list=request.tags,
            knowledge_type=request.knowledge_type,
            scope_type=url_scope_type,
            connector_type_scope=url_connector_type_scope,
        )
    )

    return UploadResponse(
        job_id=job_id,
        status="processing",
    )


async def _process_url_background(
    job_id: str,
    url: str,
    tenant_id: str,
    user_id: str,
    connector_id: str,
    tag_list: list,
    knowledge_type: str,
    scope_type: str = "instance",
    connector_type_scope: str | None = None,
) -> None:
    """Process URL ingestion in background."""
    from meho_app.api.database import create_bff_session_maker
    from meho_app.modules.knowledge.embeddings import get_embedding_provider
    from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
    from meho_app.modules.knowledge.ingestion import IngestionService
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
    from meho_app.modules.knowledge.object_storage import ObjectStorage
    from meho_app.modules.knowledge.repository import KnowledgeRepository

    session_maker = create_bff_session_maker()

    try:
        async with session_maker() as session:
            job_repo = IngestionJobRepository(session)
            repository = KnowledgeRepository(session)
            embeddings = get_embedding_provider()
            hybrid_search = PostgresFTSHybridService(repository, embeddings)
            knowledge_store = KnowledgeStore(repository, embeddings, hybrid_search)
            object_storage = ObjectStorage()

            ingestion_service = IngestionService(
                knowledge_store=knowledge_store,
                object_storage=object_storage,
                job_repository=job_repo,
            )

            await ingestion_service.ingest_url(
                url=url,
                tenant_id=tenant_id,
                connector_id=connector_id,
                user_id=user_id,
                tags=tag_list,
                knowledge_type=knowledge_type,
                job_id=job_id,
                scope_type=scope_type,
                connector_type_scope=connector_type_scope,
            )

            await session.commit()

    except Exception as e:
        logger.error(f"Background URL ingestion failed for job {job_id}: {e}", exc_info=True)


@router.get(
    "/chunks",
    response_model=ListChunksResponse,
    responses={500: {"description": "Failed to list chunks"}},
)
async def list_chunks(
    user: CurrentUser,
    session: DbSession,
    knowledge_type: Annotated[str | None, Query(description="Filter by knowledge type")] = None,
    tags: Annotated[str | None, Query(description="Comma-separated tags")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """
    List knowledge chunks with filters.

    Returns chunks accessible to the user based on ACL.
    """
    try:
        from meho_app.modules.knowledge.repository import KnowledgeRepository
        from meho_app.modules.knowledge.schemas import KnowledgeChunkFilter

        knowledge_repo = KnowledgeRepository(session)
        chunk_filter = KnowledgeChunkFilter(
            tenant_id=user.tenant_id,
            knowledge_type=knowledge_type,
        )
        all_chunks = await knowledge_repo.list_chunks(chunk_filter)

        # Apply limit manually
        chunks = all_chunks[:limit] if len(all_chunks) > limit else all_chunks

        # Convert to response format
        chunk_responses = []
        for chunk in chunks:
            chunk_responses.append(
                KnowledgeChunkResponse(
                    id=str(chunk.id),
                    text=chunk.text,
                    tenant_id=chunk.tenant_id,
                    connector_id=str(getattr(chunk, "connector_id", None) or ""),
                    user_id=chunk.user_id,
                    roles=chunk.roles or [],
                    groups=chunk.groups or [],
                    tags=chunk.tags or [],
                    knowledge_type=chunk.knowledge_type,
                    priority=chunk.priority,
                    created_at=chunk.created_at,
                    updated_at=chunk.updated_at,
                    expires_at=chunk.expires_at,
                    source_uri=chunk.source_uri,
                )
            )

        return ListChunksResponse(chunks=chunk_responses, total=len(chunk_responses))
    except Exception as e:
        logger.error(f"List chunks failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to list chunks") from e


@router.get(
    "/documents",
    response_model=ListDocumentsResponse,
    responses={500: {"description": "Failed to list documents"}},
)
async def list_documents(
    user: CurrentUser,
    session: DbSession,
    status: Annotated[str | None, Query(description="Filter by ingestion job status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """
    List uploaded documents (ingestion jobs).

    Returns metadata and basic info about each document.
    Preview text can be fetched separately if needed.
    """
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository
    from meho_app.modules.knowledge.job_schemas import IngestionJobFilter

    try:
        job_repo = IngestionJobRepository(session)
        job_filter = IngestionJobFilter(
            tenant_id=user.tenant_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        jobs = await job_repo.list_jobs(job_filter)

        # Convert to response format (exclude soft-deleted documents)
        documents = []
        for job in jobs:
            if job.status == "deleted":
                continue

            # Session 44: Include progress data for real-time UI updates
            progress = None
            if job.status == "processing":
                progress = {
                    "total_chunks": job.total_chunks or 0,
                    "chunks_processed": job.chunks_processed or 0,
                    "chunks_created": job.chunks_created or 0,
                    "percent": (job.overall_progress or 0) * 100,
                    "current_stage": job.current_stage,
                    "stage_progress": job.stage_progress,
                    "overall_progress": job.overall_progress,
                    "status_message": job.status_message,
                    "estimated_completion": job.estimated_completion,
                }

            documents.append(
                KnowledgeDocumentResponse(
                    id=str(job.id),
                    filename=job.filename,
                    knowledge_type=job.knowledge_type,
                    status=job.status,
                    tags=job.tags or [],
                    file_size=job.file_size,
                    total_chunks=job.total_chunks,
                    chunks_created=job.chunks_created or 0,
                    chunks_processed=job.chunks_processed or 0,
                    preview_text=None,
                    error=job.error,
                    started_at=job.started_at or datetime.now(tz=UTC),
                    completed_at=job.completed_at,
                    progress=progress,
                )
            )

        return ListDocumentsResponse(
            documents=documents,
            total=len(documents),
        )
    except Exception as e:
        logger.error(f"List documents failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to list documents") from e


@router.get(
    "/connectors/{connector_id}/documents",
    response_model=ListDocumentsResponse,
    responses={500: {"description": "Failed to list connector documents"}},
)
async def list_connector_documents(
    connector_id: str,
    user: CurrentUser,
    session: DbSession,
    status: Annotated[str | None, Query(description="Filter by ingestion job status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """
    List documents (ingestion jobs) for a specific connector.

    Used by the ConnectorDetails Knowledge tab to show only this connector's documents.
    """
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository
    from meho_app.modules.knowledge.job_schemas import IngestionJobFilter

    try:
        job_repo = IngestionJobRepository(session)
        job_filter = IngestionJobFilter(
            tenant_id=user.tenant_id,
            connector_id=connector_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        jobs = await job_repo.list_jobs(job_filter)

        # Exclude soft-deleted documents
        documents = []
        for job in jobs:
            if job.status == "deleted":
                continue

            progress = None
            if job.status == "processing":
                progress = {
                    "total_chunks": job.total_chunks or 0,
                    "chunks_processed": job.chunks_processed or 0,
                    "chunks_created": job.chunks_created or 0,
                    "percent": (job.overall_progress or 0) * 100,
                    "current_stage": job.current_stage,
                    "stage_progress": job.stage_progress,
                    "overall_progress": job.overall_progress,
                    "status_message": job.status_message,
                    "estimated_completion": job.estimated_completion,
                }

            documents.append(
                KnowledgeDocumentResponse(
                    id=str(job.id),
                    filename=job.filename,
                    knowledge_type=job.knowledge_type,
                    status=job.status,
                    tags=job.tags or [],
                    file_size=job.file_size,
                    total_chunks=job.total_chunks,
                    chunks_created=job.chunks_created or 0,
                    chunks_processed=job.chunks_processed or 0,
                    preview_text=None,
                    error=job.error,
                    started_at=job.started_at or datetime.now(tz=UTC),
                    completed_at=job.completed_at,
                    progress=progress,
                )
            )

        return ListDocumentsResponse(documents=documents, total=len(documents))
    except Exception as e:
        logger.error(f"List connector documents failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to list connector documents") from e


@router.delete(
    "/connectors/{connector_id}/documents/{document_id}",
    responses={
        403: {"description": "Document does not belong to this connector"},
        404: {"description": "Document not found"},
        500: {"description": "Failed to delete document"},
    },
)
async def delete_connector_document(
    connector_id: str,
    document_id: str,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.KNOWLEDGE_DELETE))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    """
    Delete a specific document and its chunks from a connector.

    Verifies the document belongs to the specified connector before deletion.
    """
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository
    from meho_app.modules.knowledge.repository import KnowledgeRepository

    try:
        job_repo = IngestionJobRepository(session)
        knowledge_repo = KnowledgeRepository(session)

        # Get job and verify ownership
        job = await job_repo.get_job(document_id)

        if not job:
            raise HTTPException(status_code=404, detail="Document not found")

        if job.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail=MSG_ACCESS_DENIED)

        if str(job.connector_id) != connector_id:
            raise HTTPException(
                status_code=403, detail="Document does not belong to this connector"
            )

        # Atomic: delete chunks then mark job -- single transaction
        chunk_ids = job.chunk_ids or []
        deleted_count = 0
        if chunk_ids:
            deleted_count = await knowledge_repo.delete_chunks_batch(chunk_ids)
            if deleted_count != len(chunk_ids):
                await session.rollback()
                raise HTTPException(
                    status_code=500,
                    detail=f"Partial deletion: {deleted_count}/{len(chunk_ids)} chunks removed. Rolled back.",
                )

        await job_repo.update_status(document_id, "deleted")
        await session.commit()

        # Audit: log connector knowledge document deletion
        try:
            from meho_app.modules.audit.service import AuditService

            audit = AuditService(session)
            await audit.log_event(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                user_email=getattr(user, "email", None),
                event_type="knowledge.delete",
                action="delete",
                resource_type="knowledge_doc",
                resource_id=document_id,
                resource_name=getattr(job, "filename", None),
                details={"connector_id": connector_id},
                result="success",
            )
            await session.commit()
        except Exception as audit_err:
            logger.warning(f"Audit logging failed for connector document delete: {audit_err}")

        return {
            "message": "Document deleted successfully",
            "document_id": document_id,
            "connector_id": connector_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Delete connector document failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete document") from e


@router.delete(
    "/documents/{document_id}",
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Document not found"},
        500: {"description": "Failed to delete document"},
    },
)
async def delete_document(
    document_id: str,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.KNOWLEDGE_DELETE))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    """
    Delete an uploaded document and all of its chunks.

    Delegates deletion to the knowledge service which handles:
    - Deleting chunks from PostgreSQL
    - Rebuilding BM25 index
    - Cleaning up storage

    Returns immediately after initiating deletion.
    """
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository
    from meho_app.modules.knowledge.repository import KnowledgeRepository

    try:
        job_repo = IngestionJobRepository(session)
        knowledge_repo = KnowledgeRepository(session)

        # Get job first to verify ownership
        job = await job_repo.get_job(document_id)

        if not job:
            raise HTTPException(status_code=404, detail="Document not found")

        # Verify tenant access
        if job.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail=MSG_ACCESS_DENIED)

        # Atomic: delete chunks then mark job -- single transaction
        chunk_ids = job.chunk_ids or []
        deleted_count = 0
        if chunk_ids:
            deleted_count = await knowledge_repo.delete_chunks_batch(chunk_ids)
            if deleted_count != len(chunk_ids):
                await session.rollback()
                raise HTTPException(
                    status_code=500,
                    detail=f"Partial deletion: {deleted_count}/{len(chunk_ids)} chunks removed. Rolled back.",
                )

        await job_repo.update_status(document_id, "deleted")
        await session.commit()

        # Audit: log knowledge document deletion
        try:
            from meho_app.modules.audit.service import AuditService

            audit = AuditService(session)
            await audit.log_event(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                user_email=getattr(user, "email", None),
                event_type="knowledge.delete",
                action="delete",
                resource_type="knowledge_doc",
                resource_id=document_id,
                resource_name=getattr(job, "filename", None),
                result="success",
            )
            await session.commit()
        except Exception as audit_err:
            logger.warning(f"Audit logging failed for knowledge delete: {audit_err}")

        return {"message": "Document deleted successfully", "document_id": document_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Delete document failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete document") from e


async def _delete_document_background(
    document_id: str,
    deletion_job_id: str,
    chunk_ids: list[str],
    tenant_id: str,
    total_chunks: int,
) -> None:
    """
    Background task to delete document with progress tracking (Session 30).

    Runs asynchronously so the API returns immediately while deletion proceeds.

    Stages:
    1. PREPARING - 5%
    2. DELETING_CHUNKS - 5-75% (batch delete)
    3. UPDATING_INDEX - 75-95% (rebuild BM25)
    4. CLEANUP_STORAGE - 95-100% (delete original file)
    """
    from meho_app.api.database import create_bff_session_maker
    from meho_app.modules.knowledge.job_models import DeletionStage
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository
    from meho_app.modules.knowledge.repository import KnowledgeRepository

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
                status_message=f"Preparing to delete {total_chunks} chunks...",
            )

            # Stage 2: Deleting chunks (70% of time) - batch delete for efficiency!
            await job_repo.update_stage(
                job_id=deletion_job_id,
                current_stage=DeletionStage.DELETING_CHUNKS.value,
                stage_progress=0.0,
                overall_progress=0.05,
                status_message=f"Deleting {total_chunks} chunks...",
            )

            # Batch delete (much faster than one-by-one!)
            deleted_count = await knowledge_repo.delete_chunks_batch(chunk_ids)

            await job_repo.update_stage(
                job_id=deletion_job_id,
                current_stage=DeletionStage.DELETING_CHUNKS.value,
                stage_progress=1.0,
                overall_progress=0.75,
                status_message=f"Deleted {deleted_count} chunks from database",
            )

            # Stage 3: Search indexes (auto-maintained by PostgreSQL)
            await job_repo.update_stage(
                job_id=deletion_job_id,
                current_stage=DeletionStage.UPDATING_INDEX.value,
                stage_progress=1.0,
                overall_progress=0.95,
                status_message="Search indexes auto-updated (PostgreSQL FTS)",
            )

            # NOTE: No manual index rebuilding needed!
            # PostgreSQL FTS indexes are automatically maintained on DELETE operations.
            logger.info(
                f"PostgreSQL FTS indexes automatically updated after deleting {deleted_count} chunks"
            )

            # Stage 4: Cleanup complete (100%)
            await job_repo.update_stage(
                job_id=deletion_job_id,
                current_stage=DeletionStage.COMPLETED.value,
                stage_progress=1.0,
                overall_progress=1.0,
                status_message=f"Deletion complete - {deleted_count} chunks removed",
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
                    error_stage=DeletionStage.DELETING_CHUNKS.value,
                )
        except Exception:  # noqa: S110 -- intentional silent exception handling
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


@router.delete(
    "/chunks/{chunk_id}",
    responses={
        403: {"description": "Access denied"},
        404: {"description": "Chunk not found"},
        500: {"description": "Failed to delete chunk"},
    },
)
async def delete_chunk(
    chunk_id: str,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.KNOWLEDGE_DELETE))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    """
    Delete a knowledge chunk.

    Users can only delete their own chunks (user_id match).
    """
    try:
        from meho_app.modules.knowledge import get_knowledge_service

        knowledge_svc = get_knowledge_service(session)

        # Get chunk to verify ownership
        chunk = await knowledge_svc.get_chunk(chunk_id)

        if not chunk:
            raise HTTPException(status_code=404, detail="Chunk not found")

        # Verify tenant access
        if chunk.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail=MSG_ACCESS_DENIED)

        # Delete chunk
        await knowledge_svc.delete_chunk(chunk_id)
        await session.commit()

        return {"message": "Chunk deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Delete chunk failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete chunk") from e


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


def _build_document_response(job, preview_text: str | None) -> KnowledgeDocumentResponse:
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
        completed_at=job.completed_at,
    )

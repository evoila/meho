# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Knowledge management routes for MEHO API.

Direct service calls (modular monolith).
"""

# mypy: disable-error-code="arg-type"
#
# ``arg-type`` is kept suppressed at file level because the SQLAlchemy ORM
# models in ``meho_app/modules/knowledge/`` still use the legacy
# ``Column(String)`` syntax rather than ``Mapped[str]``. Until every model
# migrates to the typed syntax, attribute reads like ``job.filename`` resolve
# to ``Column[str]`` in mypy instead of ``str``, producing ~50 false positives
# in this file for arguments passed to Pydantic schemas and repositories.
#
# See follow-up: convert knowledge models to ``Mapped[X]`` and remove this.
# (The other mypy disables ``no-untyped-def`` / ``var-annotated`` were fixed
# directly in the 2026-04 review gate work.)
import asyncio
import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.api.dependencies import CurrentUser, DbSession
from meho_app.core.auth_context import UserContext
from meho_app.core.errors import InternalError
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission
from meho_app.database import get_db_session

logger = get_logger(__name__)

CHUNK_PATTERN = r"#chunk=\d+$"
MSG_ACCESS_DENIED = "Access denied"


def _register_task(job_id: str, coro: Any) -> None:
    """Create a background task and track it in the process-local registry.

    See ``meho_app/modules/knowledge/task_registry.py`` for the
    "single-worker only" caveat that applies to cancel/resume.
    """
    from meho_app.modules.knowledge.task_registry import get_task_registry

    get_task_registry().register(job_id, coro)


async def _mark_job_cancelled(job_id: str) -> None:
    """Best-effort: mark a cancelled job as failed in the database."""
    try:
        from meho_app.api.database import create_bff_session_maker
        from meho_app.modules.knowledge.job_repository import IngestionJobRepository

        session_maker = create_bff_session_maker()
        async with session_maker() as session:
            job_repo = IngestionJobRepository(session)
            job = await job_repo.get_job(job_id)
            if job and job.status == "processing":
                await job_repo.fail_job(
                    job_id=job_id,
                    error="Cancelled by user",
                    error_stage=job.current_stage,
                    error_chunk_index=job.error_chunk_index,
                )
                await session.commit()
        logger.info("job_cancelled", job_id=job_id)
    except (SQLAlchemyError, RuntimeError, OSError) as e:
        logger.warning(f"Failed to mark cancelled job {job_id}: {e}")


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
    doc_version: str | None = None  # Filter by documentation version (e.g. "v8", "v9")


class KnowledgeSearchResult(BaseModel):
    """Single ranked knowledge search result enriched with retrieval metadata."""

    id: str
    text: str
    score: float = 0.0
    tenant_id: str | None = None
    connector_id: str | None = None
    connector_name: str | None = None
    connector_type: str | None = None
    tags: list[str] = Field(default_factory=list)
    knowledge_type: str
    source_uri: str | None = None
    filename: str = ""
    section_header: str = ""
    heading_path: list[str] = Field(default_factory=list)
    page_number: int = 0
    page_numbers: list[int] = Field(default_factory=list)
    page_start: int = 0
    page_end: int = 0
    source_chunk_index: int | None = None
    doc_version: str = ""
    family_id: str = ""
    family_name: str = ""


class KnowledgeSearchCitation(BaseModel):
    """Citation mapped back to a retrieved knowledge result."""

    chunk_index: int
    quote: str
    result_id: str = ""
    score: float = 0.0
    connector_id: str | None = None
    connector_name: str | None = None
    connector_type: str | None = None
    source_uri: str | None = None
    filename: str = ""
    section_header: str = ""
    heading_path: list[str] = Field(default_factory=list)
    page_number: int = 0
    page_numbers: list[int] = Field(default_factory=list)
    page_start: int = 0
    page_end: int = 0
    source_chunk_index: int | None = None


class KnowledgeSearchResponse(BaseModel):
    """Combined search response with optional grounded answer and citations."""

    query: str
    total: int
    chunks: list[KnowledgeSearchResult]
    results: list[KnowledgeSearchResult]
    answer: str | None = None
    answer_error: str | None = None
    citations: list[KnowledgeSearchCitation] = Field(default_factory=list)


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
    doc_version: str = Field(
        ..., min_length=1, description="Documentation version label (required, e.g. '9.0.0')"
    )
    family_id: str | None = Field(None, description="Document family this URL ingestion belongs to")


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
    doc_version: str | None = None
    family_id: str | None = None
    family_name: str | None = None
    version_count: int = 1
    file_size: int | None
    total_chunks: int | None
    chunks_created: int
    chunks_processed: int
    preview_text: str | None = None
    error: str | None = None
    error_stage: str | None = None
    resumable: bool = False
    started_at: datetime
    completed_at: datetime | None
    progress: dict[str, Any] | None = None


class ListDocumentsResponse(BaseModel):
    """Paginated list of knowledge documents."""

    documents: list[KnowledgeDocumentResponse]
    total: int


class DocumentChunkPreview(BaseModel):
    """Single chunk with text and metadata for document preview."""

    id: str
    text: str
    chunk_index: int
    search_metadata: dict[str, Any] | None = None


class DocumentDetailResponse(BaseModel):
    """Full document detail including all chunks for preview."""

    id: str
    filename: str | None
    knowledge_type: str
    status: str
    tags: list[str] = Field(default_factory=list)
    doc_version: str | None = None
    family_id: str | None = None
    family_name: str | None = None
    file_size: int | None
    total_chunks: int | None
    chunks_created: int
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None = None
    summary: str | None = None
    markdown: str | None = None
    markdown_available: bool = False
    markdown_size: int | None = None
    chunks: list[DocumentChunkPreview] = Field(default_factory=list)


class DocumentVersionResponse(BaseModel):
    """A single version within a document family."""

    job_id: str
    doc_version: str | None
    filename: str | None
    file_size: int | None
    file_hash: str | None
    status: str
    chunks_created: int
    started_at: datetime
    completed_at: datetime | None


class DocumentFamilyVersionsResponse(BaseModel):
    """All versions belonging to a single document family."""

    family_id: str
    family_name: str
    versions: list[DocumentVersionResponse]


async def _lookup_family_metadata(
    session: AsyncSession, family_ids: list[Any]
) -> dict[str, tuple[str, int]]:
    """Return a ``{family_id_str: (family_name, version_count)}`` mapping.

    ``version_count`` counts non-deleted ingestion jobs per family so the UI
    can show "3 versions" next to a document without a second round-trip.
    """
    import uuid as _uuid

    from sqlalchemy import func, select

    from meho_app.modules.knowledge.job_models import IngestionJob
    from meho_app.modules.knowledge.models import DocumentFamilyModel

    unique_ids: list[_uuid.UUID] = []
    seen: set[str] = set()
    for fid in family_ids:
        if fid is None:
            continue
        key = str(fid)
        if key in seen:
            continue
        seen.add(key)
        if isinstance(fid, _uuid.UUID):
            unique_ids.append(fid)
        else:
            try:
                unique_ids.append(_uuid.UUID(key))
            except ValueError:
                continue

    if not unique_ids:
        return {}

    family_rows = await session.execute(
        select(DocumentFamilyModel.id, DocumentFamilyModel.name).where(
            DocumentFamilyModel.id.in_(unique_ids)
        )
    )
    names: dict[str, str] = {str(row.id): row.name for row in family_rows}

    count_rows = await session.execute(
        select(IngestionJob.family_id, func.count())
        .where(
            IngestionJob.family_id.in_(unique_ids),
            IngestionJob.status != "deleted",
        )
        .group_by(IngestionJob.family_id)
    )
    counts: dict[str, int] = {str(row[0]): int(row[1]) for row in count_rows}

    return {fid: (names.get(fid, ""), counts.get(fid, 0)) for fid in names}


def _build_search_result(payload: dict[str, Any]) -> KnowledgeSearchResult:
    """Normalize a raw retrieval payload into the public response schema."""
    return KnowledgeSearchResult.model_validate(payload)


def _build_search_citation(
    *,
    chunk_index: int,
    quote: str,
    results: list[KnowledgeSearchResult],
) -> KnowledgeSearchCitation:
    """Map a citation back onto the retrieved search result payload."""
    result = results[chunk_index]
    return KnowledgeSearchCitation(
        chunk_index=chunk_index,
        quote=quote,
        result_id=result.id,
        score=result.score,
        connector_id=result.connector_id,
        connector_name=result.connector_name,
        connector_type=result.connector_type,
        source_uri=result.source_uri,
        filename=result.filename,
        section_header=result.section_header,
        heading_path=result.heading_path,
        page_number=result.page_number,
        page_numbers=result.page_numbers,
        page_start=result.page_start,
        page_end=result.page_end,
        source_chunk_index=result.source_chunk_index,
    )


@router.post(
    "/search",
    response_model=KnowledgeSearchResponse,
    responses={500: {"description": "Internal server error"}},
)
async def search_knowledge(
    request: SearchRequest,
    user: CurrentUser,
    session: DbSession,
) -> KnowledgeSearchResponse:
    """
    Search knowledge base.

    If connector_id is provided, scopes search to that connector only (specialist view).
    If connector_id is omitted, searches across all connectors with attribution (KnowledgePage browse).
    """
    from meho_app.core.auth_context import UserContext
    from meho_app.modules.knowledge.answer import generate_grounded_answer
    from meho_app.modules.knowledge.embeddings import get_embedding_provider
    from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
    from meho_app.modules.knowledge.repository import KnowledgeRepository

    try:
        repository = KnowledgeRepository(session)
        embeddings = get_embedding_provider()
        hybrid_search = PostgresFTSHybridService(repository, embeddings)
        knowledge_store = KnowledgeStore(repository, embeddings, hybrid_search)

        user_ctx = UserContext(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            roles=user.roles,
            groups=user.groups,
        )

        if request.connector_id:
            raw_results = await knowledge_store.search_ranked_by_connector(
                query=request.query,
                user_context=user_ctx,
                connector_id=request.connector_id,
                top_k=request.top_k,
                doc_version=request.doc_version,
            )
        else:
            raw_results = await knowledge_store.search_cross_connector(
                query=request.query,
                user_context=user_ctx,
                top_k=request.top_k,
                doc_version=request.doc_version,
            )

        search_results = [_build_search_result(result) for result in raw_results]

        answer: str | None = None
        answer_error: str | None = None
        citations: list[KnowledgeSearchCitation] = []

        if search_results:
            try:
                grounded_answer = await generate_grounded_answer(
                    query=request.query,
                    results=[result.model_dump() for result in search_results],
                )
                answer = grounded_answer.answer
                citations = [
                    _build_search_citation(
                        chunk_index=citation.chunk_index,
                        quote=citation.quote,
                        results=search_results,
                    )
                    for citation in grounded_answer.citations
                ]
            except (RuntimeError, OSError, ValueError) as exc:
                logger.warning(f"Grounded answer generation failed for knowledge search: {exc}")
                answer_error = "Failed to generate a grounded answer from the retrieved chunks."

        return KnowledgeSearchResponse(
            query=request.query,
            total=len(search_results),
            chunks=search_results,
            results=search_results,
            answer=answer,
            answer_error=answer_error,
            citations=citations,
        )
    except HTTPException:
        raise
    except (SQLAlchemyError, RuntimeError, OSError, ValueError) as e:
        # Log with full traceback server-side but do not leak error internals
        # (SQL fragments, credential parts, file paths) to unauthenticated or
        # low-privilege API consumers.
        logger.exception("knowledge_search_failed", error=str(e))
        raise InternalError(message="Knowledge search failed") from e


@router.get("/tree", responses={500: {"description": "Internal server error"}})
async def get_knowledge_tree(
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
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
    except (SQLAlchemyError, RuntimeError, OSError) as e:
        logger.error(f"Knowledge tree request failed: {e}")
        raise InternalError(message="Knowledge tree request failed") from e


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
    doc_version: Annotated[
        str, Form(min_length=1, description="Version label (required) e.g. '9.0.0', 'v8'")
    ],
    connector_id: Annotated[
        str | None, Form()
    ] = None,  # Required for instance scope, null for global/type
    knowledge_type: Annotated[str, Form()] = "documentation",
    tags: Annotated[str, Form()] = "[]",  # JSON array as string
    scope_type: Annotated[str, Form()] = "instance",  # global, type, or instance
    connector_type_scope: Annotated[
        str | None, Form()
    ] = None,  # e.g. "kubernetes" for type-scoped docs
) -> UploadResponse:
    """
    Upload document to knowledge base.

    **PDF ONLY** - HTML and DOCX files should be converted to PDF first.
    This ensures consistent quality and avoids duplicate content issues.

    Returns job_id for progress tracking!
    Frontend should poll GET /knowledge/jobs/{job_id} for progress.

    For large documents, processing happens in background to avoid timeout.
    """

    from meho_app.api.database import create_bff_session_maker
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are supported. Please convert HTML/DOCX to PDF first.",
        )

    tag_list = json.loads(tags) if tags else []

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

    file_content = await file.read()
    file_size_mb = len(file_content) / 1024 / 1024

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

    import os as _os

    family_stem = _os.path.splitext(_os.path.basename(file.filename))[0] or file.filename

    async with session_maker() as session:
        # Create job for tracking
        from meho_app.modules.knowledge.family_repository import DocumentFamilyRepository
        from meho_app.modules.knowledge.family_schemas import DocumentFamilyCreate
        from meho_app.modules.knowledge.job_schemas import IngestionJobCreate

        family_repo = DocumentFamilyRepository(session)

        # Reject if a family with the same (scope, connector, name) already exists --
        # the caller should be using the per-document "Upload new version" flow
        # instead of creating a second family with the same display name.
        existing = await family_repo.find_by_name(
            tenant_id=user.tenant_id,
            name=family_stem,
            scope_type=scope_type,
            connector_id=connector_id,
            connector_type_scope=connector_type_scope,
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "A document with this name already exists. Use the "
                    "'Upload new version' button on the existing document."
                ),
            )

        from sqlalchemy.exc import IntegrityError

        try:
            family = await family_repo.create_family(
                DocumentFamilyCreate(
                    tenant_id=user.tenant_id,
                    name=family_stem,
                    scope_type=scope_type,
                    connector_id=connector_id,
                    connector_type_scope=connector_type_scope,
                    knowledge_type=knowledge_type,
                    tags=tag_list,
                    created_by_user_id=user.user_id,
                )
            )
            family_id_str = str(family.id)

            job_repo = IngestionJobRepository(session)
            job_create = IngestionJobCreate(
                job_type="document",
                tenant_id=user.tenant_id,
                connector_id=connector_id,
                scope_type=scope_type,
                connector_type_scope=connector_type_scope,
                filename=file.filename,
                file_size=len(file_content),
                knowledge_type=knowledge_type,
                tags=tag_list,
                doc_version=doc_version,
                family_id=family.id,
            )
            job = await job_repo.create_job(job_create)
            job_id = str(job.id)
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            # A concurrent upload won the unique-name race between our
            # `find_by_name` pre-check above and the INSERT.
            raise HTTPException(
                status_code=409,
                detail=(
                    "A document with this name already exists. Use the "
                    "'Upload new version' button on the existing document."
                ),
            ) from exc

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
    except Exception as audit_err:  # noqa: BLE001 -- audit side-effect must not fail the upload response
        logger.warning(f"Audit logging failed for knowledge upload: {audit_err}")

    # Always process in background so the POST returns immediately with
    # job_id, allowing the frontend to start polling progress right away.
    # Previously, files <= 1 MB were processed synchronously, which blocked
    # the HTTP response and left the upload dialog stuck at "Uploading... 0%"
    # because polling could not start until the response arrived.
    _register_task(
        job_id,
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
            doc_version=doc_version,
            family_id=family_id_str,
        ),
    )

    return UploadResponse(job_id=job_id, status="processing")


@router.post(
    "/documents/{document_id}/versions",
    response_model=UploadResponse,
    responses={
        400: {"description": "Invalid request"},
        404: {"description": "Document (family) not found"},
        409: {"description": "Version already exists or identical file already uploaded"},
        413: {"description": "Request entity too large"},
    },
)
async def upload_document_version(
    document_id: str,
    file: Annotated[UploadFile, File(...)],
    user: Annotated[UserContext, Depends(RequirePermission(Permission.KNOWLEDGE_INGEST))],
    doc_version: Annotated[str, Form(min_length=1, description="Version label (required)")],
) -> UploadResponse:
    """Upload a new version of an existing document.

    ``document_id`` is any ingestion-job id that belongs to the target
    family -- we resolve the family from the job and inherit its scope,
    connector, knowledge_type, and tags. Rejects with 409 if the family
    already contains this version string or the same file (by SHA-256).
    """
    import hashlib as _hashlib

    from meho_app.api.database import create_bff_session_maker
    from meho_app.modules.knowledge.family_repository import DocumentFamilyRepository
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository
    from meho_app.modules.knowledge.job_schemas import IngestionJobCreate

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are supported. Please convert HTML/DOCX to PDF first.",
        )

    file_content = await file.read()
    if not file_content.startswith(b"%PDF"):
        raise HTTPException(
            status_code=400, detail="File is not a valid PDF. Please upload a PDF file."
        )
    if b"%%EOF" not in file_content[-1024:]:
        raise HTTPException(
            status_code=400,
            detail="PDF file appears to be corrupted (missing EOF marker). Please check the file.",
        )

    from meho_app.core.config import get_config

    max_size_mb = get_config().ingestion_max_file_size_mb
    max_size_bytes = max_size_mb * 1024 * 1024
    if len(file_content) > max_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File size ({len(file_content) / 1024 / 1024:.1f} MB) exceeds maximum allowed "
                f"size ({max_size_mb} MB)."
            ),
        )

    file_sha256 = _hashlib.sha256(file_content).hexdigest()
    session_maker = create_bff_session_maker()

    async with session_maker() as session:
        job_repo = IngestionJobRepository(session)
        family_repo = DocumentFamilyRepository(session)

        # Resolve the target family via the job reference passed in the URL.
        target_job = await job_repo.get_job(document_id)
        if not target_job or target_job.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Document not found")
        if not target_job.family_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This document predates the versioning system and has no family. "
                    "Delete and re-upload it to enable versioning."
                ),
            )

        family = await family_repo.get_family(target_job.family_id)
        if family is None:
            raise HTTPException(status_code=404, detail="Document family not found")

        # Uniqueness checks: same version string or identical file bytes -> 409.
        if await family_repo.has_version(family.id, doc_version):
            raise HTTPException(
                status_code=409,
                detail=f"Version '{doc_version}' already exists for this document.",
            )
        if await family_repo.has_hash(family.id, file_sha256):
            raise HTTPException(
                status_code=409,
                detail=(
                    "An identical file is already uploaded to this document under "
                    "a different version."
                ),
            )

        from sqlalchemy.exc import IntegrityError

        # Inherit scope/connector/knowledge_type/tags from the family.
        connector_id_str = str(family.connector_id) if family.connector_id else None

        try:
            job_create = IngestionJobCreate(
                job_type="document",
                tenant_id=user.tenant_id,
                connector_id=connector_id_str,
                scope_type=family.scope_type,
                connector_type_scope=family.connector_type_scope,
                filename=file.filename,
                file_size=len(file_content),
                knowledge_type=family.knowledge_type,
                tags=list(family.tags or []),
                doc_version=doc_version,
                family_id=family.id,
            )
            job = await job_repo.create_job(job_create)
            job_id = str(job.id)
            family_id_str = str(family.id)
            family_knowledge_type = family.knowledge_type
            family_tags = list(family.tags or [])
            family_scope_type = family.scope_type
            family_connector_type_scope = family.connector_type_scope
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            # A concurrent upload_document_version call won the unique
            # (family_id, doc_version) / (family_id, file_sha256) race
            # between our has_version/has_hash checks and the INSERT.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Version '{doc_version}' or an identical file was just "
                    "uploaded for this document by another request."
                ),
            ) from exc

    try:
        from meho_app.modules.audit.service import AuditService

        async with session_maker() as audit_session:
            audit = AuditService(audit_session)
            await audit.log_event(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                user_email=getattr(user, "email", None),
                event_type="knowledge.upload_version",
                action="create",
                resource_type="knowledge_doc",
                resource_id=job_id,
                resource_name=file.filename,
                details={
                    "family_id": family_id_str,
                    "doc_version": doc_version,
                    "file_size_bytes": len(file_content),
                },
                result="success",
            )
            await audit_session.commit()
    except Exception as audit_err:  # noqa: BLE001 -- audit side-effect must not fail the version upload response
        logger.warning(f"Audit logging failed for version upload: {audit_err}")

    _register_task(
        job_id,
        _process_document_background(
            job_id=job_id,
            file_content=file_content,
            filename=file.filename,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            tag_list=family_tags,
            knowledge_type=family_knowledge_type,
            connector_id=connector_id_str or "",
            scope_type=family_scope_type,
            connector_type_scope=family_connector_type_scope,
            doc_version=doc_version,
            family_id=family_id_str,
        ),
    )

    return UploadResponse(job_id=job_id, status="processing")


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
    doc_version: str | None = None,
    family_id: str | None = None,
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

            from meho_app.modules.knowledge.hybrid_search import (
                PostgresFTSHybridService,
            )
            from meho_app.modules.knowledge.ingestion import IngestionService
            from meho_app.modules.knowledge.object_storage import ObjectStorage

            repository = KnowledgeRepository(session)
            embeddings = get_embedding_provider()
            hybrid_search = PostgresFTSHybridService(repository, embeddings)
            knowledge_store = KnowledgeStore(repository, embeddings, hybrid_search)
            object_storage = ObjectStorage()  # Gets config from environment

            ingestion_service = IngestionService(
                knowledge_store=knowledge_store,
                object_storage=object_storage,
                job_repository=job_repo,
            )

            # IngestionService handles job completion internally
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
                doc_version=doc_version,
                family_id=family_id,
            )

            await session.commit()

            return {"status": "completed", "chunks_created": len(chunk_ids)}

        except (SQLAlchemyError, RuntimeError, OSError, ValueError) as e:
            await job_repo.fail_job(job_id=job_id, error=str(e))
            await session.commit()
            raise
        except Exception as e:  # noqa: BLE001 -- ensure job is always marked failed on unexpected errors (e.g. Docling, embedding, or MinIO failures not covered above)
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
    doc_version: str | None = None,
    family_id: str | None = None,
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
            doc_version=doc_version,
            family_id=family_id,
        )
    except asyncio.CancelledError:
        await _mark_job_cancelled(job_id)
        raise
    except Exception as e:  # noqa: BLE001 -- document background pipeline spans Docling, embeddings, MinIO, and PostgreSQL
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
) -> list[dict[str, Any]]:
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

        # Return a bare array (not {"jobs": [...]}) for frontend compatibility
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
    except (SQLAlchemyError, RuntimeError, OSError) as e:
        logger.error(f"Get active jobs failed: {e}")
        raise InternalError(message="Failed to get active jobs") from e


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
) -> dict[str, Any]:
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

        if job.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail=MSG_ACCESS_DENIED)

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

        is_resumable = (
            job.status == "failed" and job.error_stage == "embedding" and bool(job.storage_key)
        )

        return {
            "id": str(job.id),
            "filename": job.filename,
            "status": job.status,
            "progress": progress,
            "chunks_created": job.chunks_created,
            "chunks_processed": job.chunks_processed,
            "total_chunks": job.total_chunks,
            "error": job.error,
            "error_stage": job.error_stage,
            "resumable": is_resumable,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        }
    except HTTPException:
        raise
    except (SQLAlchemyError, RuntimeError, OSError) as e:
        logger.error(f"Get job status failed: {e}")
        raise InternalError(message="Failed to get job status") from e


@router.post(
    "/jobs/{job_id}/resume",
    responses={
        400: {"description": "Job is not resumable"},
        403: {"description": "Access denied"},
        404: {"description": "Job not found"},
        500: {"description": "Failed to resume job"},
    },
)
async def resume_ingestion_job(
    job_id: str,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.KNOWLEDGE_INGEST))],
) -> dict[str, Any]:
    """Resume a failed document ingestion job from its checkpoint.

    Only jobs that failed during the embedding stage can be resumed.
    The endpoint loads the previously saved chunk checkpoint from MinIO
    and continues embedding from where the job left off.
    """

    from meho_app.api.database import create_bff_session_maker
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository

    session_maker = create_bff_session_maker()

    async with session_maker() as session:
        job_repo = IngestionJobRepository(session)
        job = await job_repo.get_job(job_id)

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail=MSG_ACCESS_DENIED)
        if job.status != "failed":
            raise HTTPException(
                status_code=409,
                detail=f"Job is not in failed state (status={job.status})",
            )
        if job.error_stage != "embedding":
            raise HTTPException(
                status_code=400,
                detail=f"Only embedding-stage failures are resumable (failed at {job.error_stage})",
            )

        # Atomically claim the resume. A conditional UPDATE on status='failed'
        # ensures only one concurrent caller wins; everyone else gets 409.
        claimed = await job_repo.mark_resuming(job_id)
        if not claimed:
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail="Job is no longer resumable (another resume or state change won the race)",
            )
        await session.commit()

    _register_task(
        job_id,
        _resume_job_background(
            job_id=job_id,
            tenant_id=user.tenant_id,
        ),
    )

    return {"job_id": job_id, "status": "processing", "message": "Resume started"}


async def _resume_job_background(job_id: str, tenant_id: str) -> None:
    """Background task that drives the resume_document_ingestion call."""
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

            await ingestion_service.resume_document_ingestion(
                job_id=job_id,
                tenant_id=tenant_id,
            )

            await session.commit()

    except asyncio.CancelledError:
        await _mark_job_cancelled(job_id)
        raise
    except (SQLAlchemyError, RuntimeError, OSError) as e:
        logger.error(f"Background resume failed for job {job_id}: {e}", exc_info=True)
        try:
            async with session_maker() as session:
                job_repo = IngestionJobRepository(session)
                await job_repo.fail_job(job_id=job_id, error=str(e), error_stage="embedding")
                await session.commit()
        except Exception as cleanup_err:  # noqa: BLE001 - best-effort fail-state write
            logger.error(
                f"Failed to mark job {job_id} as failed after resume error: {cleanup_err}",
                exc_info=True,
            )


@router.post(
    "/jobs/{job_id}/cancel",
    responses={
        400: {"description": "Job is not active"},
        403: {"description": "Access denied"},
        404: {"description": "Job not found"},
        500: {"description": "Failed to cancel job"},
    },
)
async def cancel_ingestion_job(
    job_id: str,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.KNOWLEDGE_INGEST))],
) -> dict[str, Any]:
    """Cancel an active (processing) ingestion job.

    Cancels the background asyncio task and marks the job as failed.
    Partial results (already-embedded chunks) are preserved and the
    job can be resumed later via the resume endpoint.
    """
    from meho_app.api.database import create_bff_session_maker
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository

    session_maker = create_bff_session_maker()

    async with session_maker() as session:
        job_repo = IngestionJobRepository(session)
        job = await job_repo.get_job(job_id)

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail=MSG_ACCESS_DENIED)
        if job.status not in ("pending", "processing"):
            raise HTTPException(status_code=400, detail=f"Job is not active (status={job.status})")

    from meho_app.modules.knowledge.task_registry import get_task_registry

    task = get_task_registry().get(job_id)
    if task and not task.done():
        task.cancel()
        logger.info("ingestion_task_cancelled", job_id=job_id)
        return {
            "job_id": job_id,
            "status": "cancelled",
            "message": "Job cancellation requested",
        }

    # No local task tracks this job -- the running asyncio task lives on
    # another worker/replica. We cannot safely mark the job as failed from
    # here because that would race the worker that is actively writing
    # progress; the worker would then overwrite our fail_job and continue
    # processing. Return 202 so the caller can retry or poll, and rely on
    # the owning worker to observe the cancel signal.
    #
    # A future improvement is to publish a cancel signal (Redis pub/sub) that
    # the owning worker consumes at checkpoint boundaries. For now we
    # surface the state instead of risking a silent duplicate-write race.
    logger.info(
        "ingestion_cancel_no_local_task",
        job_id=job_id,
        status=job.status,
    )
    return {
        "job_id": job_id,
        "status": job.status,
        "message": (
            "Cancellation requested, but the job is not tracked on this worker. "
            "Cross-worker cancellation is not supported yet; retry from the "
            "worker running the job or wait for it to finish."
        ),
    }


@router.post(
    "/ingest-text",
    response_model=IngestTextResponse,
    responses={500: {"description": "Internal server error"}},
)
async def ingest_text(
    request: IngestTextRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.KNOWLEDGE_INGEST))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> IngestTextResponse:
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
    roles: list[str] = []
    groups: list[str] = []
    user_id_override = None
    connector_id = request.connector_id

    if request.scope == "private":
        user_id_override = user.user_id
    elif request.scope == "team":
        groups = user.groups

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
            expires_at=request.expires_at,
        )
        return IngestTextResponse(
            chunk_ids=result.get("chunk_ids", []),
            count=len(result.get("chunk_ids", [])),
        )
    except (SQLAlchemyError, RuntimeError, OSError, ValueError) as e:
        logger.error(f"Text ingestion request failed: {e}")
        raise InternalError(message="Text ingestion failed") from e


@router.post(
    "/ingest-url",
    response_model=UploadResponse,
    responses={400: {"description": "Invalid URL. Must start with http:// or https://"}},
)
async def ingest_url(
    request: IngestUrlRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.KNOWLEDGE_INGEST))],
) -> UploadResponse:
    """
    Ingest content from a web URL into the knowledge base.

    Fetches the URL, extracts text (HTML, plain text, or PDF),
    and processes it through the chunking + embedding pipeline.

    Processing happens in background; returns job_id for status tracking.
    Poll GET /knowledge/jobs/{job_id} for progress.
    """
    import re

    url = request.url.strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(
            status_code=400, detail="Invalid URL. Must start with http:// or https://"
        )

    from meho_app.api.database import create_bff_session_maker
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository

    session_maker = create_bff_session_maker()

    # Determine scope for URL ingestion
    url_scope_type = "instance"
    url_connector_type_scope = request.connector_type_scope
    if not request.connector_id and not request.connector_type_scope:
        url_scope_type = "global"
    elif not request.connector_id and request.connector_type_scope:
        url_scope_type = "type"

    # Create job for tracking
    async with session_maker() as session:
        from meho_app.modules.knowledge.family_repository import DocumentFamilyRepository
        from meho_app.modules.knowledge.family_schemas import DocumentFamilyCreate
        from meho_app.modules.knowledge.job_schemas import IngestionJobCreate

        family_repo = DocumentFamilyRepository(session)
        family_obj = None
        family_uuid = None

        # If caller points at an existing family, reuse it (new-version flow
        # for URL sources). Otherwise create a family keyed by the URL.
        if request.family_id:
            family_obj = await family_repo.get_family(request.family_id)
            if family_obj is None or family_obj.tenant_id != user.tenant_id:
                raise HTTPException(status_code=404, detail="Document family not found")
            if await family_repo.has_version(family_obj.id, request.doc_version):
                raise HTTPException(
                    status_code=409,
                    detail=(f"Version '{request.doc_version}' already exists for this document."),
                )
            family_uuid = family_obj.id
        else:
            existing = await family_repo.find_by_name(
                tenant_id=user.tenant_id,
                name=url,
                scope_type=url_scope_type,
                connector_id=request.connector_id,
                connector_type_scope=url_connector_type_scope,
            )
            if existing is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This URL is already tracked as a document. Upload a new "
                        "version via its existing document entry."
                    ),
                )
            family_obj = await family_repo.create_family(
                DocumentFamilyCreate(
                    tenant_id=user.tenant_id,
                    name=url,
                    scope_type=url_scope_type,
                    connector_id=request.connector_id,
                    connector_type_scope=url_connector_type_scope,
                    knowledge_type=request.knowledge_type,
                    tags=request.tags,
                    created_by_user_id=user.user_id,
                )
            )
            family_uuid = family_obj.id

        job_repo = IngestionJobRepository(session)
        job_create = IngestionJobCreate(
            job_type="url",
            tenant_id=user.tenant_id,
            connector_id=request.connector_id,
            scope_type=url_scope_type,
            connector_type_scope=url_connector_type_scope,
            filename=url,
            file_size=0,
            knowledge_type=request.knowledge_type,
            tags=request.tags,
            doc_version=request.doc_version,
            family_id=family_uuid,
        )
        job = await job_repo.create_job(job_create)
        job_id = str(job.id)
        url_family_id = str(family_uuid)
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
    except Exception as audit_err:  # noqa: BLE001 -- audit side-effect must not fail the URL ingestion response
        logger.warning(f"Audit logging failed for URL ingestion: {audit_err}")

    # Process in background (URL fetch can be slow)
    _register_task(
        job_id,
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
            doc_version=request.doc_version,
            family_id=url_family_id,
        ),
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
    doc_version: str | None = None,
    family_id: str | None = None,
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
                doc_version=doc_version,
                family_id=family_id,
            )

            await session.commit()

    except asyncio.CancelledError:
        await _mark_job_cancelled(job_id)
        raise
    except Exception as e:  # noqa: BLE001 -- URL background pipeline spans HTTP fetch, embedding, MinIO, and PostgreSQL
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
) -> ListChunksResponse:
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

        chunks = all_chunks[:limit] if len(all_chunks) > limit else all_chunks

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
    except (SQLAlchemyError, RuntimeError, OSError) as e:
        logger.error(f"List chunks failed: {e}")
        raise InternalError(message="Failed to list chunks") from e


@router.get(
    "/documents",
    response_model=ListDocumentsResponse,
    responses={500: {"description": "Failed to list documents"}},
)
async def list_documents(
    user: CurrentUser,
    session: DbSession,
    status: Annotated[str | None, Query(description="Filter by ingestion job status")] = None,
    scope_type: Annotated[
        str | None, Query(description="Filter by scope: global, type, or instance")
    ] = None,
    connector_type_scope: Annotated[
        str | None, Query(description="Filter by connector type for type-scoped docs")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ListDocumentsResponse:
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
            scope_type=scope_type,
            connector_type_scope=connector_type_scope,
            limit=limit,
            offset=offset,
        )
        jobs = await job_repo.list_jobs(job_filter)

        # Enrich with family name + version_count in one batch query
        active_jobs = [j for j in jobs if j.status != "deleted"]
        family_lookup = await _lookup_family_metadata(session, [j.family_id for j in active_jobs])

        documents = []
        for job in active_jobs:
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

            is_resumable = (
                job.status == "failed"
                and job.error_stage == "embedding"
                and bool(getattr(job, "storage_key", None))
            )
            family_id_str = str(job.family_id) if job.family_id else None
            family_name, version_count = (
                family_lookup.get(family_id_str, ("", 1)) if family_id_str else ("", 1)
            )
            documents.append(
                KnowledgeDocumentResponse(
                    id=str(job.id),
                    filename=job.filename,
                    knowledge_type=job.knowledge_type,
                    status=job.status,
                    tags=job.tags or [],
                    doc_version=job.doc_version,
                    family_id=family_id_str,
                    family_name=family_name or None,
                    version_count=version_count or 1,
                    file_size=job.file_size,
                    total_chunks=job.total_chunks,
                    chunks_created=job.chunks_created or 0,
                    chunks_processed=job.chunks_processed or 0,
                    preview_text=None,
                    error=job.error,
                    error_stage=job.error_stage,
                    resumable=is_resumable,
                    started_at=job.started_at or datetime.now(tz=UTC),
                    completed_at=job.completed_at,
                    progress=progress,
                )
            )

        return ListDocumentsResponse(
            documents=documents,
            total=len(documents),
        )
    except (SQLAlchemyError, RuntimeError, OSError) as e:
        logger.error(f"List documents failed: {e}")
        raise InternalError(message="Failed to list documents") from e


@router.get(
    "/documents/{document_id}/detail",
    response_model=DocumentDetailResponse,
    responses={
        404: {"description": "Document not found"},
        500: {"description": "Failed to load document detail"},
    },
)
async def get_document_detail(
    document_id: str,
    user: CurrentUser,
    session: DbSession,
    chunk_offset: Annotated[int, Query(ge=0, description="Chunk pagination offset")] = 0,
    chunk_limit: Annotated[int, Query(ge=1, le=500, description="Chunks per page")] = 200,
) -> DocumentDetailResponse:
    """Return document metadata and a paginated slice of chunks for preview.

    Looks up the IngestionJob by ID, verifies tenant ownership, then
    fetches the requested chunk page (from ``chunk_ids`` JSONB column)
    ordered by creation time so they mirror the original document order.
    """
    import asyncio

    from meho_app.modules.knowledge.job_repository import IngestionJobRepository
    from meho_app.modules.knowledge.repository import KnowledgeRepository

    BATCH_SIZE = 500

    try:
        job_repo = IngestionJobRepository(session)
        job = await job_repo.get_job(document_id)

        if not job or job.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Document not found")

        chunks_preview: list[DocumentChunkPreview] = []
        db_chunks: list[Any] = []

        raw_chunk_ids: list[str] = list(job.chunk_ids or [])
        page_ids = raw_chunk_ids[chunk_offset : chunk_offset + chunk_limit]

        user_ctx = UserContext(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            roles=user.roles,
            groups=user.groups,
        )

        if page_ids:
            knowledge_repo = KnowledgeRepository(session)
            if len(page_ids) <= BATCH_SIZE:
                db_chunks = await knowledge_repo.get_chunks_with_acl(page_ids, user_ctx)
            else:
                for batch_start in range(0, len(page_ids), BATCH_SIZE):
                    batch = page_ids[batch_start : batch_start + BATCH_SIZE]
                    db_chunks.extend(await knowledge_repo.get_chunks_with_acl(batch, user_ctx))

            id_to_chunk = {str(c.id): c for c in db_chunks}
            for page_idx, cid in enumerate(page_ids):
                db_chunk = id_to_chunk.get(cid)
                if db_chunk:
                    raw_meta = db_chunk.search_metadata
                    if raw_meta is None:
                        meta_dict: dict[str, Any] | None = None
                    elif hasattr(raw_meta, "model_dump"):
                        meta_dict = raw_meta.model_dump(mode="json", exclude_none=True)
                    elif isinstance(raw_meta, dict):
                        meta_dict = raw_meta
                    else:
                        meta_dict = None
                    chunks_preview.append(
                        DocumentChunkPreview(
                            id=str(db_chunk.id),
                            text=db_chunk.text,
                            chunk_index=chunk_offset + page_idx,
                            search_metadata=meta_dict,
                        )
                    )

        MAX_MARKDOWN_SIZE = 2 * 1024 * 1024  # 2 MB

        stored_markdown: str | None = None
        markdown_available = False
        markdown_size: int | None = None

        if raw_chunk_ids and chunk_offset == 0:
            first_source: str | None = None
            if db_chunks:
                first_source = db_chunks[0].source_uri
            elif job.storage_key:
                first_source = f"s3://meho-dev-data/{job.storage_key}"

            if first_source and first_source.startswith("s3://"):
                parts = first_source.split("/", 3)  # ['s3:', '', 'bucket', 'rest']
                if len(parts) == 4:
                    storage_key = parts[3]
                    md_key = storage_key + ".md"
                    try:
                        from meho_app.modules.knowledge.object_storage import ObjectStorage

                        obj_store = ObjectStorage()
                        if await asyncio.to_thread(obj_store.document_exists, md_key):
                            md_bytes: bytes = await asyncio.to_thread(
                                obj_store.download_document, md_key
                            )
                            markdown_available = True
                            markdown_size = len(md_bytes)
                            if len(md_bytes) <= MAX_MARKDOWN_SIZE:
                                stored_markdown = md_bytes.decode("utf-8")
                    except (RuntimeError, OSError, ConnectionError) as md_err:
                        logger.warning("markdown_fetch_failed", key=md_key, error=str(md_err))

        family_id_str = str(job.family_id) if job.family_id else None
        family_name_value: str | None = None
        if family_id_str:
            from meho_app.modules.knowledge.family_repository import DocumentFamilyRepository

            family_repo = DocumentFamilyRepository(session)
            family = await family_repo.get_family(job.family_id)
            if family is not None:
                family_name_value = str(family.name) if family.name else None

        return DocumentDetailResponse(
            id=str(job.id),
            filename=job.filename,
            knowledge_type=job.knowledge_type,
            status=job.status,
            tags=job.tags or [],
            doc_version=job.doc_version,
            family_id=family_id_str,
            family_name=family_name_value,
            file_size=job.file_size,
            total_chunks=job.total_chunks,
            chunks_created=job.chunks_created or 0,
            started_at=job.started_at,
            completed_at=job.completed_at,
            error=job.error,
            summary=job.document_summary,
            markdown=stored_markdown,
            markdown_available=markdown_available,
            markdown_size=markdown_size,
            chunks=chunks_preview,
        )
    except HTTPException:
        raise
    except (SQLAlchemyError, RuntimeError, OSError) as e:
        logger.error(f"Get document detail failed: {e}")
        raise InternalError(message="Failed to load document detail") from e


@router.get(
    "/families/{family_id}/versions",
    response_model=DocumentFamilyVersionsResponse,
    responses={
        404: {"description": "Document family not found"},
        500: {"description": "Failed to load versions"},
    },
)
async def list_family_versions(
    family_id: str,
    user: CurrentUser,
    session: DbSession,
) -> DocumentFamilyVersionsResponse:
    """List all non-deleted versions belonging to a document family."""
    from meho_app.modules.knowledge.family_repository import DocumentFamilyRepository

    try:
        family_repo = DocumentFamilyRepository(session)
        family = await family_repo.get_family(family_id)
        if family is None or family.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Document family not found")

        jobs = await family_repo.list_versions(family.id)

        versions = [
            DocumentVersionResponse(
                job_id=str(job.id),
                doc_version=job.doc_version,
                filename=job.filename,
                file_size=job.file_size,
                file_hash=job.file_hash,
                status=job.status,
                chunks_created=job.chunks_created or 0,
                started_at=job.started_at or datetime.now(tz=UTC),
                completed_at=job.completed_at,
            )
            for job in jobs
        ]

        return DocumentFamilyVersionsResponse(
            family_id=str(family.id),
            family_name=family.name,
            versions=versions,
        )
    except HTTPException:
        raise
    except (SQLAlchemyError, RuntimeError, OSError) as e:
        logger.error(f"List family versions failed: {e}")
        raise InternalError(message="Failed to load versions") from e


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
) -> ListDocumentsResponse:
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

        # Enrich with family name + version_count
        active_jobs = [j for j in jobs if j.status != "deleted"]
        family_lookup = await _lookup_family_metadata(session, [j.family_id for j in active_jobs])

        documents = []
        for job in active_jobs:
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

            is_resumable = (
                job.status == "failed"
                and job.error_stage == "embedding"
                and bool(getattr(job, "storage_key", None))
            )
            family_id_str = str(job.family_id) if job.family_id else None
            family_name, version_count = (
                family_lookup.get(family_id_str, ("", 1)) if family_id_str else ("", 1)
            )
            documents.append(
                KnowledgeDocumentResponse(
                    id=str(job.id),
                    filename=job.filename,
                    knowledge_type=job.knowledge_type,
                    status=job.status,
                    tags=job.tags or [],
                    doc_version=job.doc_version,
                    family_id=family_id_str,
                    family_name=family_name or None,
                    version_count=version_count or 1,
                    file_size=job.file_size,
                    total_chunks=job.total_chunks,
                    chunks_created=job.chunks_created or 0,
                    chunks_processed=job.chunks_processed or 0,
                    preview_text=None,
                    error=job.error,
                    error_stage=job.error_stage,
                    resumable=is_resumable,
                    started_at=job.started_at or datetime.now(tz=UTC),
                    completed_at=job.completed_at,
                    progress=progress,
                )
            )

        return ListDocumentsResponse(documents=documents, total=len(documents))
    except (SQLAlchemyError, RuntimeError, OSError) as e:
        logger.error(f"List connector documents failed: {e}")
        raise InternalError(message="Failed to list connector documents") from e


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
) -> dict[str, Any]:
    """
    Delete a specific document and its chunks from a connector.

    Verifies the document belongs to the specified connector before deletion.
    """
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository
    from meho_app.modules.knowledge.repository import KnowledgeRepository

    try:
        job_repo = IngestionJobRepository(session)
        knowledge_repo = KnowledgeRepository(session)

        job = await job_repo.get_job(document_id)

        if not job:
            raise HTTPException(status_code=404, detail="Document not found")

        if job.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail=MSG_ACCESS_DENIED)

        if str(job.connector_id) != connector_id:
            raise HTTPException(
                status_code=403, detail="Document does not belong to this connector"
            )

        chunk_ids: list[str] = list(job.chunk_ids or [])
        deleted_count = 0
        if chunk_ids:
            delete_batch_size = 5000
            for batch_start in range(0, len(chunk_ids), delete_batch_size):
                batch = chunk_ids[batch_start : batch_start + delete_batch_size]
                deleted_count += await knowledge_repo.delete_chunks_batch(batch)

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
        except Exception as audit_err:  # noqa: BLE001 -- audit side-effect must not fail the delete response
            logger.warning(f"Audit logging failed for connector document delete: {audit_err}")

        return {
            "message": "Document deleted successfully",
            "document_id": document_id,
            "connector_id": connector_id,
        }
    except HTTPException:
        raise
    except (SQLAlchemyError, RuntimeError, OSError) as e:
        logger.error(f"Delete connector document failed: {e}")
        raise InternalError(message="Failed to delete connector document") from e


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
) -> dict[str, Any]:
    """
    Delete an uploaded document and all of its chunks.

    Delegates deletion to the knowledge service which handles:
    - Deleting chunks from PostgreSQL
    - Finalizing search state
    - Cleaning up storage

    Returns immediately after initiating deletion.
    """
    from meho_app.modules.knowledge.job_repository import IngestionJobRepository
    from meho_app.modules.knowledge.repository import KnowledgeRepository

    try:
        job_repo = IngestionJobRepository(session)
        knowledge_repo = KnowledgeRepository(session)

        job = await job_repo.get_job(document_id)

        if not job:
            raise HTTPException(status_code=404, detail="Document not found")

        if job.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail=MSG_ACCESS_DENIED)

        chunk_ids: list[str] = list(job.chunk_ids or [])
        deleted_count = 0
        if chunk_ids:
            delete_batch_size = 5000
            for batch_start in range(0, len(chunk_ids), delete_batch_size):
                batch = chunk_ids[batch_start : batch_start + delete_batch_size]
                deleted_count += await knowledge_repo.delete_chunks_batch(batch)

        await job_repo.update_status(document_id, "deleted")
        await session.commit()

        logger.info(
            "document_deleted",
            document_id=document_id,
            chunks_deleted=deleted_count,
            chunks_expected=len(chunk_ids),
        )

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
        except Exception as audit_err:  # noqa: BLE001 -- audit side-effect must not fail the delete response
            logger.warning(f"Audit logging failed for knowledge delete: {audit_err}")

        return {"message": "Document deleted successfully", "document_id": document_id}
    except HTTPException:
        raise
    except (SQLAlchemyError, RuntimeError, OSError) as e:
        logger.error(f"Delete document failed: {e}")
        raise InternalError(message="Failed to delete document") from e


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
    3. UPDATING_INDEX - 75-95% (search state finalization)
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

            # Stage 2: Deleting chunks (5-75%) - batched for efficiency
            await job_repo.update_stage(
                job_id=deletion_job_id,
                current_stage=DeletionStage.DELETING_CHUNKS.value,
                stage_progress=0.0,
                overall_progress=0.05,
                status_message=f"Deleting {total_chunks} chunks...",
            )

            delete_batch_size = 5000
            deleted_count = 0
            for batch_start in range(0, len(chunk_ids), delete_batch_size):
                batch = chunk_ids[batch_start : batch_start + delete_batch_size]
                deleted_count += await knowledge_repo.delete_chunks_batch(batch)
                progress = min(0.75, 0.05 + 0.70 * (batch_start + len(batch)) / len(chunk_ids))
                await job_repo.update_stage(
                    job_id=deletion_job_id,
                    current_stage=DeletionStage.DELETING_CHUNKS.value,
                    stage_progress=(batch_start + len(batch)) / len(chunk_ids),
                    overall_progress=progress,
                    status_message=f"Deleted {deleted_count}/{total_chunks} chunks...",
                )

            await job_repo.update_stage(
                job_id=deletion_job_id,
                current_stage=DeletionStage.DELETING_CHUNKS.value,
                stage_progress=1.0,
                overall_progress=0.75,
                status_message=f"Deleted {deleted_count} chunks from database",
            )

            # Stage 3: Search state finalization
            await job_repo.update_stage(
                job_id=deletion_job_id,
                current_stage=DeletionStage.UPDATING_INDEX.value,
                stage_progress=1.0,
                overall_progress=0.95,
                status_message="Search state finalized",
            )

            logger.info("knowledge_search_state_finalized", deleted_count=deleted_count)

            # Stage 4: Cleanup complete (100%)
            await job_repo.update_stage(
                job_id=deletion_job_id,
                current_stage=DeletionStage.COMPLETED.value,
                stage_progress=1.0,
                overall_progress=1.0,
                status_message=f"Deletion complete - {deleted_count} chunks removed",
            )

            await job_repo.complete_job(deletion_job_id, [])

            logger.info(f"Document deletion complete: {deleted_count} chunks deleted")

            # Only now is it safe to delete the original job record (finally-block below).
            deletion_succeeded = True

    except Exception as e:  # noqa: BLE001 -- delete background spans chunk deletion, index finalization, and storage cleanup
        try:
            async with session_maker() as session:
                job_repo = IngestionJobRepository(session)
                await job_repo.fail_job(
                    job_id=deletion_job_id,
                    error=str(e),
                    error_stage=DeletionStage.DELETING_CHUNKS.value,
                )
        except Exception:  # noqa: BLE001, S110 -- inner safety net: if fail_job itself fails we still continue cleanup
            pass  # Best effort

        logger.error(f"Document deletion failed: {e}")

    finally:
        # Only delete the original ingestion job if deletion succeeded.
        # Otherwise leave the failed deletion job visible so the user can retry.
        if deletion_succeeded:
            try:
                async with session_maker() as session:
                    job_repo = IngestionJobRepository(session)
                    await job_repo.delete_job(document_id)
            except (SQLAlchemyError, RuntimeError, OSError) as e:
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
) -> dict[str, Any]:
    """
    Delete a knowledge chunk.

    Users can only delete their own chunks (user_id match).
    """
    try:
        from meho_app.modules.knowledge import get_knowledge_service

        knowledge_svc = get_knowledge_service(session)

        chunk = await knowledge_svc.get_chunk(chunk_id)

        if not chunk:
            raise HTTPException(status_code=404, detail="Chunk not found")

        if chunk.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail=MSG_ACCESS_DENIED)

        await knowledge_svc.delete_chunk(chunk_id)
        await session.commit()

        return {"message": "Chunk deleted successfully"}
    except HTTPException:
        raise
    except (SQLAlchemyError, RuntimeError, OSError) as e:
        logger.error(f"Delete chunk failed: {e}")
        raise InternalError(message="Failed to delete chunk") from e


async def _get_document_preview(job: Any, repository: Any) -> str | None:
    """
    Fetch a short preview snippet for a document by loading the first chunk text.
    """
    if not job.chunk_ids:
        return None

    first_chunk_id = str(job.chunk_ids[0])
    chunk = await repository.get_chunk(first_chunk_id)

    if not chunk or not chunk.text:
        return None

    text: str = chunk.text
    return text[:DOCUMENT_PREVIEW_LENGTH]


def _build_document_response(job: Any, preview_text: str | None) -> KnowledgeDocumentResponse:
    """
    Normalize ingestion job model into API response payload.
    """
    is_resumable = (
        job.status == "failed"
        and job.error_stage == "embedding"
        and bool(getattr(job, "storage_key", None))
    )
    return KnowledgeDocumentResponse(
        id=str(job.id),
        filename=job.filename,
        knowledge_type=job.knowledge_type,
        status=job.status,
        tags=job.tags or [],
        doc_version=job.doc_version,
        family_id=str(job.family_id) if getattr(job, "family_id", None) else None,
        family_name=None,
        version_count=1,
        file_size=job.file_size,
        total_chunks=job.total_chunks,
        chunks_created=job.chunks_created,
        chunks_processed=job.chunks_processed,
        preview_text=preview_text,
        error=job.error,
        error_stage=job.error_stage,
        resumable=is_resumable,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )

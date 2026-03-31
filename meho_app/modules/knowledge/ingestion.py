# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Knowledge ingestion service.

Orchestrates document processing: extraction, chunking, embedding, and storage.
NOW with job tracking for progress visibility!
"""

# mypy: disable-error-code="no-untyped-def,var-annotated"
import asyncio
import contextlib
import traceback
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from meho_app.core.config import get_config
from meho_app.core.feature_flags import get_feature_flags
from meho_app.core.otel import get_logger
from meho_app.modules.knowledge.chunking import TextChunker  # Still needed for ingest_text()
from meho_app.modules.knowledge.job_models import IngestionStage
from meho_app.modules.knowledge.job_repository import IngestionJobRepository
from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.metadata_extraction import MetadataExtractor
from meho_app.modules.knowledge.object_storage import ObjectStorage
from meho_app.modules.knowledge.schemas import ChunkMetadata, KnowledgeChunkCreate, KnowledgeType

# Flag-based converter selection: when use_docling=false, avoid importing
# document_converter.py (which imports Docling/PyTorch at module level).
_use_docling = get_feature_flags().use_docling

if _use_docling:
    from meho_app.modules.knowledge.document_converter import (
        SUPPORTED_MIME_TYPES,
        DoclingDocumentConverter,
        _get_pdf_page_count,
        build_chunk_prefix,
        generate_document_summary,
    )
    from meho_app.modules.knowledge.subprocess_converter import (
        convert_file_in_subprocess,
        convert_pdf_batched_in_subprocesses,
    )
else:
    from meho_app.modules.knowledge.lightweight_converter import (  # type: ignore[no-redef]
        SUPPORTED_MIME_TYPES,
        LightweightDocumentConverter,
        build_chunk_prefix,
        generate_document_summary,
    )

if TYPE_CHECKING:
    import pyarrow as pa

logger = get_logger(__name__)


class IngestionService:
    """Orchestrates document ingestion into knowledge base with job tracking"""

    def __init__(
        self,
        knowledge_store: KnowledgeStore,
        object_storage: ObjectStorage,
        job_repository: IngestionJobRepository | None = None,
        chunker: TextChunker | None = None,
    ):
        """
        Initialize ingestion service.

        Args:
            knowledge_store: Store for chunks
            object_storage: Storage for original documents
            job_repository: Repository for job tracking (optional for backward compatibility)
            chunker: Text chunker (creates default if not provided)

        Note: No bm25_manager needed - PostgreSQL FTS indexes are automatic
        """
        self.knowledge_store = knowledge_store
        self.object_storage = object_storage
        self.job_repository = job_repository
        self.chunker = chunker or TextChunker()  # Used by ingest_text()
        # Named docling_converter for backward compat; may be LightweightDocumentConverter
        if _use_docling:
            self.docling_converter = DoclingDocumentConverter()
        else:
            self.docling_converter = LightweightDocumentConverter()  # type: ignore[assignment]
        self.metadata_extractor = MetadataExtractor()

    async def _update_job_stage(
        self,
        job_id: str | None,
        stage: IngestionStage,
        progress: float,
        message: str = "",
        overall_progress: float | None = None,
        **kwargs,
    ):
        """
        Update job with current stage and progress (Session 30 - Task 29).

        Calculates overall progress based on stage weights if not provided.

        Stage weights:
        - UPLOADING: 0-5%
        - EXTRACTING: 5-15%
        - CHUNKING: 15-20%
        - EMBEDDING: 20-95% (slowest stage!)
        - STORING: 95-100%
        """
        if job_id and self.job_repository:
            # Calculate overall progress from stage if not explicit
            if overall_progress is None:
                stage_weights = {
                    IngestionStage.UPLOADING: (0.00, 0.05),
                    IngestionStage.EXTRACTING: (0.05, 0.15),
                    IngestionStage.CHUNKING: (0.15, 0.20),
                    IngestionStage.EMBEDDING: (0.20, 0.95),
                    IngestionStage.STORING: (0.95, 1.00),
                }

                start_pct, end_pct = stage_weights.get(stage, (0.0, 1.0))
                overall_progress = start_pct + (progress * (end_pct - start_pct))

            await self.job_repository.update_stage(
                job_id=job_id,
                current_stage=stage.value,
                stage_progress=progress,
                overall_progress=overall_progress,
                status_message=message,
                stage_started_at=datetime.now(tz=UTC),
                **kwargs,
            )

    def _calculate_eta(
        self, total_chunks: int, processed_chunks: int, stage_start_time: datetime
    ) -> datetime | None:
        """Calculate estimated completion time for embedding stage"""
        if processed_chunks == 0:
            return None

        elapsed = (datetime.now(tz=UTC) - stage_start_time).total_seconds()
        chunks_per_second = processed_chunks / elapsed
        remaining_chunks = total_chunks - processed_chunks
        remaining_seconds = remaining_chunks / chunks_per_second if chunks_per_second > 0 else 0

        return datetime.now(tz=UTC) + timedelta(seconds=remaining_seconds)

    async def _resolve_connector_context(
        self, connector_id: str | None
    ) -> tuple[str | None, str | None]:
        """Resolve connector type and name for chunk enrichment (D-05)."""
        if not connector_id:
            return None, None
        try:
            from sqlalchemy import select

            from meho_app.database import get_session_maker
            from meho_app.modules.connectors.models import ConnectorModel

            session_maker = get_session_maker()
            async with session_maker() as session:
                stmt = select(ConnectorModel.connector_type, ConnectorModel.name).where(
                    ConnectorModel.id == connector_id
                )
                result = await session.execute(stmt)
                row = result.one_or_none()
                if row:
                    return row[0], row[1]
        except Exception as e:
            logger.warning(
                "connector_context_resolution_failed", connector_id=connector_id, error=str(e)
            )
        return None, None

    def _should_offload(self, mime_type: str, file_bytes: bytes) -> tuple[bool, int]:
        """Check if document should be offloaded to ephemeral worker.

        Returns:
            Tuple of (should_offload, page_count).
        """
        flags = get_feature_flags()
        if not flags.ephemeral_ingestion:
            return False, 0

        config = get_config()
        if config.ingestion_backend == "local":
            return False, 0  # Local backend uses existing subprocess path

        if mime_type != "application/pdf":
            return False, 0  # Only PDFs need offloading (non-PDF is lightweight)

        page_count = _get_pdf_page_count(file_bytes)

        if page_count <= config.ingestion_offload_threshold_pages:
            return False, page_count

        return True, page_count

    async def _dispatch_to_worker(
        self,
        file_bytes: bytes,
        filename: str,
        mime_type: str,
        page_count: int,
        storage_key: str,
        job_id: str | None,
        tenant_id: str | None,
        connector_id: str | None,
        user_id: str | None,
        roles: list[str],
        groups: list[str],
        tags: list[str],
        knowledge_type: KnowledgeType | None,
        priority: int,
        scope_type: str,
        connector_type_scope: str | None,
        chunk_prefix: str,
    ) -> list[str]:
        """Offload document processing to ephemeral worker backend.

        Flow:
        1. Generate presigned URLs for worker access (file already uploaded)
        2. Dispatch job via IngestionDispatcher
        3. Poll for completion with exponential backoff
        4. Download Arrow IPC results
        5. Import chunks with pre-computed embeddings into pgvector
        """
        from meho_app.worker.backends.protocol import JobState
        from meho_app.worker.dispatcher import IngestionDispatcher
        from meho_app.worker.resource_estimator import estimate_resources

        dispatcher = IngestionDispatcher()

        # Generate presigned URLs for worker
        input_url = self.object_storage.generate_presigned_download_url(storage_key)
        output_key = f"worker-results/{tenant_id or 'global'}/{job_id or 'unknown'}/results.arrow"
        output_url = self.object_storage.generate_presigned_upload_url(
            output_key, content_type="application/octet-stream"
        )

        # Dispatch job
        env_overrides: dict[str, str] = {}
        if chunk_prefix:
            env_overrides["WORKER_CHUNK_PREFIX"] = chunk_prefix

        await self._update_job_stage(
            job_id,
            stage=IngestionStage.EXTRACTING,
            progress=0.1,
            message="Dispatching to ephemeral worker...",
        )

        execution_id = await dispatcher.dispatch(
            job_id=job_id or str(uuid.uuid4()),
            input_url=input_url,
            output_url=output_url,
            page_count=page_count,
            env_overrides=env_overrides or None,
        )

        # Poll for completion with exponential backoff
        poll_interval = 5.0  # Start at 5 seconds
        max_poll_interval = 60.0  # Cap at 60 seconds
        total_wait = 0.0
        max_wait = float(estimate_resources(page_count).timeout_seconds) * 1.2  # 20% buffer

        while total_wait < max_wait:
            await asyncio.sleep(poll_interval)
            total_wait += poll_interval

            status = await dispatcher.get_status(execution_id)

            if status.state == JobState.SUCCEEDED:
                break
            elif status.state in (JobState.FAILED, JobState.CANCELLED, JobState.TIMEOUT):
                raise ValueError(f"Worker failed: {status.error_message or status.state.value}")

            # Update progress
            progress = min(0.9, total_wait / max_wait)
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.EMBEDDING,
                progress=progress,
                message=f"Worker processing ({status.state.value})...",
            )

            # Exponential backoff capped at max_poll_interval
            poll_interval = min(poll_interval * 1.5, max_poll_interval)
        else:
            # Timeout
            await dispatcher.cancel(execution_id)
            raise ValueError(f"Worker timed out after {total_wait:.0f}s")

        # Download and import results
        await self._update_job_stage(
            job_id,
            stage=IngestionStage.STORING,
            progress=0.0,
            message="Importing results from worker...",
        )

        # Download Arrow results from object storage
        from meho_app.worker.arrow_codec import deserialize_chunks

        arrow_bytes = self.object_storage.download_document(output_key)
        table = deserialize_chunks(arrow_bytes)

        # Import chunks with pre-computed embeddings
        chunk_ids = await self._import_arrow_chunks(
            table=table,
            tenant_id=tenant_id,
            connector_id=connector_id,
            user_id=user_id,
            roles=roles,
            groups=groups,
            tags=tags,
            knowledge_type=knowledge_type,
            priority=priority,
            scope_type=scope_type,
            connector_type_scope=connector_type_scope,
            source_uri=f"s3://{self.object_storage.bucket}/{storage_key}",
        )

        return chunk_ids

    async def _import_arrow_chunks(
        self,
        table: "pa.Table",
        tenant_id: str | None,
        connector_id: str | None,
        user_id: str | None,
        roles: list[str],
        groups: list[str],
        tags: list[str],
        knowledge_type: KnowledgeType | None,
        priority: int,
        scope_type: str,
        connector_type_scope: str | None,
        source_uri: str,
    ) -> list[str]:
        """Import Arrow IPC chunks with pre-computed embeddings into knowledge store.

        Bypasses the normal add_chunk() path (which generates embeddings)
        and writes directly to the repository with pre-computed embeddings.
        Batches inserts for performance on large documents (10K+ chunks).
        """
        # Pre-extract columns once (avoid per-row column lookup)
        col_text = table.column("chunk_text")
        col_embedding = table.column("embedding")
        col_heading = table.column("heading_stack")
        col_chapter = table.column("chapter")
        col_section = table.column("section")
        col_subsection = table.column("subsection")
        col_content_type = table.column("content_type")
        col_has_table = table.column("has_table")
        col_has_code = table.column("has_code_example")
        col_has_json = table.column("has_json_example")
        col_keywords = table.column("keywords")
        col_resource_type = table.column("resource_type")

        chunk_ids: list[str] = []
        batch: list[tuple["KnowledgeChunkCreate", list[float] | None]] = []
        batch_size = 100

        for i in range(table.num_rows):
            embedding = col_embedding[i].as_py()
            metadata = ChunkMetadata(
                chapter=col_chapter[i].as_py() or None,
                section=col_section[i].as_py() or None,
                subsection=col_subsection[i].as_py() or None,
                content_type=col_content_type[i].as_py() or "description",
                has_table=col_has_table[i].as_py(),
                has_code_example=col_has_code[i].as_py(),
                has_json_example=col_has_json[i].as_py(),
                keywords=col_keywords[i].as_py() or [],
                resource_type=col_resource_type[i].as_py() or None,
                heading_hierarchy=col_heading[i].as_py() or [],
            )

            chunk_create = KnowledgeChunkCreate(
                text=col_text[i].as_py(),
                tenant_id=tenant_id,
                connector_id=connector_id,
                user_id=user_id,
                roles=roles,
                groups=groups,
                tags=tags,
                source_uri=source_uri,
                scope_type=scope_type,
                connector_type_scope=connector_type_scope,
                knowledge_type=knowledge_type or KnowledgeType.DOCUMENTATION,
                priority=priority,
                search_metadata=metadata,
            )

            batch.append((chunk_create, embedding))

            if len(batch) >= batch_size:
                ids = await self.knowledge_store.repository.create_chunks_batch(
                    batch
                )
                chunk_ids.extend(ids)
                batch.clear()

        # Flush remaining
        if batch:
            ids = await self.knowledge_store.repository.create_chunks_batch(batch)
            chunk_ids.extend(ids)

        return chunk_ids

    async def ingest_document(
        self,
        file_bytes: bytes,
        filename: str,
        mime_type: str,
        tenant_id: str | None = None,
        connector_id: str | None = None,
        user_id: str | None = None,
        roles: list[str] | None = None,
        groups: list[str] | None = None,
        tags: list[str] | None = None,
        knowledge_type: KnowledgeType | None = None,
        priority: int = 0,
        job_id: str | None = None,  # If provided, update existing job instead of creating new one
        system_id: str
        | None = None,  # Deprecated — kept for backward compat with connector operation code
        scope_type: str = "instance",  # Three-tier scope: global, type, instance
        connector_type_scope: str | None = None,  # e.g. "kubernetes" for type-scoped docs
    ) -> list[str]:
        """
        Ingest a document.

        Process:
        1. Store original in object storage
        2. Extract text
        3. Chunk text
        4. Create knowledge chunks with embeddings

        Args:
            file_bytes: Document content
            filename: Original filename
            mime_type: Document MIME type
            tenant_id: Tenant ID (None for global)
            connector_id: Connector ID (required for new uploads)
            user_id: User ID (None for non-user-specific)
            roles: Required roles for access
            groups: Required groups for access
            tags: Tags for categorization
            knowledge_type: Type of knowledge (DOCUMENTATION, PROCEDURE)
            priority: Search ranking priority

        Returns:
            List of created chunk IDs
        """
        roles = roles or []
        groups = groups or []
        tags = tags or []

        # Track job if repository available
        if self.job_repository and job_id:
            await self.job_repository.update_status(job_id, "processing")
            logger.info(f"Job {job_id}: Starting document processing")

        # Stage 1: Uploading to object storage (5% of total time)
        await self._update_job_stage(
            job_id,
            stage=IngestionStage.UPLOADING,
            progress=0.0,
            message="Uploading file to storage...",
        )

        doc_id = str(uuid.uuid4())
        storage_key = f"documents/{tenant_id or 'global'}/{doc_id}/{filename}"
        storage_uri = self.object_storage.upload_document(file_bytes, storage_key, mime_type)

        await self._update_job_stage(
            job_id, stage=IngestionStage.UPLOADING, progress=1.0, message="Upload complete"
        )

        chunk_ids = []  # Track created chunks for cleanup on failure

        try:
            # Stage 2: Extracting text (10% of total time)
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.EXTRACTING,
                progress=0.0,
                message="Extracting text from document...",
            )

            is_structured = mime_type in SUPPORTED_MIME_TYPES

            if is_structured and _use_docling:
                # --- Docling path: subprocess isolation + ephemeral worker offloading ---

                # Check if this document should be dispatched to an ephemeral worker
                # (feature flag on, non-local backend, PDF above page threshold).
                should_offload, offload_page_count = self._should_offload(mime_type, file_bytes)
                if should_offload:
                    try:
                        connector_type, connector_name = await self._resolve_connector_context(
                            connector_id
                        )
                        chunk_prefix = build_chunk_prefix(connector_type, connector_name, "")

                        offload_chunk_ids = await self._dispatch_to_worker(
                            file_bytes=file_bytes,
                            filename=filename,
                            mime_type=mime_type,
                            page_count=offload_page_count,
                            storage_key=storage_key,
                            job_id=job_id,
                            tenant_id=tenant_id,
                            connector_id=connector_id,
                            user_id=user_id,
                            roles=roles,
                            groups=groups,
                            tags=tags,
                            knowledge_type=knowledge_type,
                            priority=priority,
                            scope_type=scope_type,
                            connector_type_scope=connector_type_scope,
                            chunk_prefix=chunk_prefix,
                        )

                        # Finalize and return
                        await self._update_job_stage(
                            job_id,
                            stage=IngestionStage.STORING,
                            progress=1.0,
                            message=f"Imported {len(offload_chunk_ids)} chunks from worker",
                            total_chunks=len(offload_chunk_ids),
                            chunks_created=len(offload_chunk_ids),
                        )

                        if self.job_repository and job_id:
                            await self.job_repository.complete_job(
                                job_id, [str(cid) for cid in offload_chunk_ids]
                            )

                        return [str(cid) for cid in offload_chunk_ids]

                    except Exception as offload_error:
                        logger.warning(
                            "ephemeral_worker_dispatch_failed",
                            error=str(offload_error),
                            filename=filename,
                            page_count=offload_page_count,
                        )
                        # Fall through to existing subprocess path

                config = get_config()

                # D-05: Heartbeat callback to update job progress during long conversions
                async def _heartbeat(message: str) -> None:
                    await self._update_job_stage(
                        job_id,
                        stage=IngestionStage.EXTRACTING,
                        progress=0.5,
                        message=message,
                    )

                # Create sync wrapper for the async heartbeat
                # (on_heartbeat is called from a thread, not async context)
                _loop = asyncio.get_event_loop()

                def _sync_heartbeat(message: str) -> None:
                    future = asyncio.run_coroutine_threadsafe(_heartbeat(message), _loop)
                    # Log heartbeat failures without propagating -- DB session timeout
                    # during conversion (Pitfall 6) should not kill the conversion
                    future.add_done_callback(
                        lambda f: (
                            logger.warning("heartbeat_failed", error=str(f.exception()))
                            if f.exception()
                            else None
                        )
                    )

                # D-01: Run Docling in subprocess to prevent OOM killing uvicorn.
                # Large PDFs use subprocess-per-batch to bound memory:
                # PyTorch's C++ allocator never returns memory to the OS
                # within a single process, so each batch must run in its
                # own short-lived subprocess that exits after converting.
                is_large_pdf = (
                    mime_type == "application/pdf"
                    and _get_pdf_page_count(file_bytes) > config.ingestion_page_batch_size
                )

                if is_large_pdf:
                    doc = await convert_pdf_batched_in_subprocesses(
                        file_bytes=file_bytes,
                        filename=filename,
                        mime_type=mime_type,
                        memory_limit_mb=config.ingestion_memory_limit_mb,
                        page_batch_size=config.ingestion_page_batch_size,
                        ocr_enabled=config.ingestion_ocr_enabled,
                        timeout_seconds_per_batch=600,
                        on_heartbeat=_sync_heartbeat,
                    )
                else:
                    doc = await convert_file_in_subprocess(
                        file_bytes=file_bytes,
                        filename=filename,
                        mime_type=mime_type,
                        memory_limit_mb=config.ingestion_memory_limit_mb,
                        ocr_enabled=config.ingestion_ocr_enabled,
                        timeout_seconds=600,
                        on_heartbeat=_sync_heartbeat,
                    )
                document_text = self.docling_converter.get_full_text(doc)

            elif is_structured:
                # --- Lightweight path: direct in-process conversion ---
                # No subprocess isolation needed (no PyTorch memory leak concern).
                doc = self.docling_converter.convert_file(file_bytes, filename, mime_type)
                document_text = self.docling_converter.get_full_text(doc)
            else:
                # Plain text / unsupported: decode and use TextChunker path
                doc = None
                try:
                    document_text = file_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    document_text = file_bytes.decode("latin-1")

            await self._update_job_stage(
                job_id,
                stage=IngestionStage.EXTRACTING,
                progress=1.0,
                message=f"Extracted {len(document_text)} characters",
            )

            # D-05: Generate document-level summary + connector context
            connector_type, connector_name = await self._resolve_connector_context(connector_id)
            document_summary = await generate_document_summary(
                document_text, connector_type, connector_name
            )
            chunk_prefix = build_chunk_prefix(connector_type, connector_name, document_summary)
            logger.info(
                "document_summary_generated",
                has_summary=bool(document_summary),
                prefix_length=len(chunk_prefix),
                connector_type=connector_type,
            )

            # Stage 3: Chunking (5% of total time)
            await self._update_job_stage(
                job_id, stage=IngestionStage.CHUNKING, progress=0.0, message="Chunking document..."
            )

            if doc is not None:
                # Structured: Docling HybridChunker with prefix enrichment
                chunks_with_context = self.docling_converter.chunk_document(
                    doc, chunk_prefix=chunk_prefix
                )
            else:
                # Plain text: TextChunker fallback with manual prefix
                raw_chunks = self.chunker.chunk_document_with_structure(
                    text=document_text, document_name=filename, detect_headings=True
                )
                if chunk_prefix:
                    chunks_with_context = [
                        (chunk_prefix + "\n\n" + text, ctx) for text, ctx in raw_chunks
                    ]
                else:
                    chunks_with_context = raw_chunks

            # Validate we got some chunks
            if not chunks_with_context:
                raise ValueError(f"No text extracted from document {filename}")

            total_chunks = len(chunks_with_context)

            await self._update_job_stage(
                job_id,
                stage=IngestionStage.CHUNKING,
                progress=1.0,
                message=f"Created {total_chunks} chunks",
                total_chunks=total_chunks,
            )

            # Stage 4: Metadata Extraction + Embedding (75% of total time - SLOWEST!)
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.EMBEDDING,
                progress=0.0,
                message=f"Processing chunk 0 of {total_chunks}...",
                total_chunks=total_chunks,
            )

            embedding_start_time = datetime.now(tz=UTC)

            # 4. Create knowledge chunks with metadata
            # Note: If chunk creation fails midway, we'll clean up partial chunks
            for i, (chunk_text, context) in enumerate(chunks_with_context):
                # Extract rich metadata for this chunk
                metadata = self.metadata_extractor.extract_metadata(
                    text=chunk_text,
                    document_name=filename,
                    _chunk_index=i,
                    document_context=context,
                )

                # Debug logging for first few chunks
                logger.info(
                    "metadata_extracted",
                    chunk_index=i,
                    chapter=metadata.chapter,
                    section=metadata.section,
                    resource_type=metadata.resource_type,
                    content_type=metadata.content_type.value if metadata.content_type else None,
                    endpoint_path=metadata.endpoint_path,
                    has_json_example=metadata.has_json_example,
                    keywords_count=len(metadata.keywords),
                    metadata_is_none=metadata is None,
                    metadata_type=type(metadata).__name__,
                )

                chunk_create = KnowledgeChunkCreate(
                    text=chunk_text,
                    tenant_id=tenant_id,
                    connector_id=connector_id,
                    user_id=user_id,
                    roles=roles,
                    groups=groups,
                    tags=tags,
                    source_uri=storage_uri,
                    # Three-tier scoping
                    scope_type=scope_type,
                    connector_type_scope=connector_type_scope,
                    # Lifecycle fields
                    knowledge_type=knowledge_type or KnowledgeType.DOCUMENTATION,
                    priority=priority,
                    expires_at=None,  # Documents never expire (use EVENT for temporary)
                    # Rich metadata for enhanced retrieval
                    search_metadata=metadata,
                )

                # Verify metadata is in the create object
                logger.info(
                    "chunk_create_object_ready",
                    chunk_index=i,
                    has_search_metadata=chunk_create.search_metadata is not None,
                    search_metadata_type=type(chunk_create.search_metadata).__name__
                    if chunk_create.search_metadata
                    else None,
                    search_metadata_chapter=chunk_create.search_metadata.chapter
                    if chunk_create.search_metadata
                    else None,
                )

                try:
                    chunk = await self.knowledge_store.add_chunk(chunk_create)
                    chunk_ids.append(chunk.id)

                    # Update progress every 10 chunks (don't spam database)
                    if (i + 1) % 10 == 0 or (i + 1) == total_chunks:
                        stage_progress = (i + 1) / total_chunks
                        overall_progress = 0.20 + (
                            0.75 * stage_progress
                        )  # 20% done before embedding

                        # Calculate ETA
                        eta = self._calculate_eta(total_chunks, i + 1, embedding_start_time)

                        await self._update_job_stage(
                            job_id,
                            stage=IngestionStage.EMBEDDING,
                            progress=stage_progress,
                            overall_progress=overall_progress,
                            message=f"Processing chunk {i + 1} of {total_chunks}...",
                            chunks_processed=i + 1,
                            estimated_completion=eta,
                        )

                except Exception as chunk_error:
                    # If chunk creation fails, we have partial ingestion
                    # Clean up what we created so far
                    raise ValueError(
                        f"Failed to create chunk {len(chunk_ids) + 1} of {len(chunks_with_context)}: {chunk_error}"
                    ) from chunk_error

            # Stage 5: Storing/Finalizing (5% of total time)
            await self._update_job_stage(
                job_id, stage=IngestionStage.STORING, progress=0.5, message="Finalizing..."
            )

            # NOTE: No manual index building needed!
            # PostgreSQL FTS indexes are automatically maintained by the database.
            # The GIN index on knowledge_chunk.text is updated on every INSERT/UPDATE.
            logger.info(
                "fts_index_auto_maintained",
                tenant_id=tenant_id,
                num_chunks=len(chunk_ids),
                detail="PostgreSQL FTS indexes automatically updated",
            )

            # Mark job as complete
            if self.job_repository and job_id:
                await self.job_repository.complete_job(job_id, chunk_ids)
                logger.info(f"Job {job_id}: Completed successfully with {len(chunk_ids)} chunks")

            return chunk_ids

        except Exception as e:
            # Capture detailed error information (Session 30 - Task 29)
            error_details = {
                "exception_type": type(e).__name__,
                "exception_message": str(e),
                "traceback": traceback.format_exc(),
                "file_info": {
                    "filename": filename,
                    "mime_type": mime_type,
                    "size_bytes": len(file_bytes),
                },
            }

            # Determine which stage failed by finding the NEXT stage after the last
            # completed checkpoint variable. E.g. if storage_uri is set but
            # document_text is not, the failure is in EXTRACTING (not uploading).
            current_stage = None
            error_chunk_index = None

            if "embedding_start_time" in locals():
                current_stage = IngestionStage.EMBEDDING.value
                error_chunk_index = len(chunk_ids)
            elif "chunks_with_context" in locals():
                current_stage = IngestionStage.EMBEDDING.value
            elif "document_text" in locals():
                current_stage = IngestionStage.CHUNKING.value
            elif "storage_uri" in locals():
                current_stage = IngestionStage.EXTRACTING.value
            else:
                current_stage = IngestionStage.UPLOADING.value

            # Build a meaningful error string even when str(e) is empty
            # (e.g. OOM-killed subprocess on macOS sends no message)
            error_msg = str(e) or f"{type(e).__name__}: {e!r}"

            # Mark job as failed with detailed error
            if self.job_repository and job_id:
                await self.job_repository.fail_job(
                    job_id=job_id,
                    error=error_msg,
                    error_stage=current_stage,
                    error_chunk_index=error_chunk_index,
                    error_details=error_details,
                )
                logger.error(
                    f"Job {job_id}: Failed at stage {current_stage} - "
                    f"[{type(e).__name__}] {error_msg}",
                    exc_info=True,
                )

            # Clean up on any failure
            # 1. Delete partially created chunks
            for chunk_id in chunk_ids:
                try:  # noqa: SIM105 -- explicit error handling preferred
                    await self.knowledge_store.delete_chunk(chunk_id)
                except Exception:  # noqa: S110 -- intentional silent exception handling
                    # Best effort cleanup
                    pass

            # 2. Delete uploaded document
            try:  # noqa: SIM105 -- explicit error handling preferred
                self.object_storage.delete_document(storage_key)
            except Exception:  # noqa: S110 -- intentional silent exception handling
                # Best effort cleanup
                pass

            # Re-raise original error with context
            raise ValueError(
                f"Failed to ingest document {filename}: [{type(e).__name__}] {error_msg}"
            ) from e

    async def ingest_text(
        self,
        text: str,
        tenant_id: str | None = None,
        connector_id: str | None = None,
        user_id: str | None = None,
        roles: list[str] | None = None,
        groups: list[str] | None = None,
        tags: list[str] | None = None,
        source_uri: str | None = None,
        knowledge_type: KnowledgeType | None = None,
        priority: int = 0,
        expires_at: datetime | None = None,
        system_id: str | None = None,  # Deprecated — kept for backward compat
        scope_type: str = "instance",  # Three-tier scope: global, type, instance
        connector_type_scope: str | None = None,  # e.g. "kubernetes" for type-scoped docs
    ) -> list[str]:
        """
        Ingest raw text (e.g., from notes, procedures, temporary notices).

        Args:
            text: Text to ingest
            tenant_id: Tenant ID
            connector_id: Connector ID (required for new ingestions)
            user_id: User ID
            roles: Required roles
            groups: Required groups
            tags: Tags
            source_uri: Optional source URI
            knowledge_type: Type of knowledge (DOCUMENTATION, PROCEDURE, EVENT)
            priority: Search ranking priority
            expires_at: Expiration time for temporary knowledge

        Returns:
            List of created chunk IDs
        """
        roles = roles or []
        groups = groups or []
        tags = tags or []

        # Update job status if tracking
        if self.job_repository and hasattr(self, "_current_job_id"):
            job_id = self._current_job_id
            await self.job_repository.update_status(job_id, "processing")

        chunk_ids = []  # Track created chunks for cleanup on failure

        try:
            # Chunk the text with structure tracking
            chunks_with_context = self.chunker.chunk_document_with_structure(
                text=text, document_name=source_uri or "text-input", detect_headings=True
            )

            if not chunks_with_context:
                raise ValueError("No chunks created from text (text may be empty)")

            # Update job with total chunks
            if self.job_repository and hasattr(self, "_current_job_id"):
                await self.job_repository.update_progress(
                    self._current_job_id, total_chunks=len(chunks_with_context)
                )

            # Create knowledge chunks with metadata
            for i, (chunk_text, context) in enumerate(chunks_with_context):
                chunk_source_uri = f"{source_uri}#chunk={i}" if source_uri else None

                # Extract metadata
                metadata = self.metadata_extractor.extract_metadata(
                    text=chunk_text,
                    document_name=source_uri or "text-input",
                    chunk_index=i,
                    document_context=context,
                )

                chunk_create = KnowledgeChunkCreate(
                    text=chunk_text,
                    tenant_id=tenant_id,
                    connector_id=connector_id,
                    user_id=user_id,
                    roles=roles,
                    groups=groups,
                    tags=tags,
                    source_uri=chunk_source_uri,
                    # Three-tier scoping
                    scope_type=scope_type,
                    connector_type_scope=connector_type_scope,
                    # Lifecycle fields
                    knowledge_type=knowledge_type or KnowledgeType.DOCUMENTATION,
                    priority=priority,
                    expires_at=expires_at,
                    # Rich metadata for enhanced retrieval
                    search_metadata=metadata,
                )

                try:
                    chunk = await self.knowledge_store.add_chunk(chunk_create)
                    chunk_ids.append(chunk.id)

                    # Update job progress
                    if self.job_repository and hasattr(self, "_current_job_id"):
                        await self.job_repository.update_progress(
                            self._current_job_id,
                            chunks_processed=i + 1,
                            chunks_created=len(chunk_ids),
                        )

                except Exception as chunk_error:
                    # Chunk creation failed
                    raise ValueError(
                        f"Failed to create text chunk {i + 1} of {len(chunks_with_context)}: {chunk_error}"
                    ) from chunk_error

            # Mark job complete if tracking
            if self.job_repository and hasattr(self, "_current_job_id"):
                await self.job_repository.complete_job(self._current_job_id, chunk_ids)

            return chunk_ids

        except Exception as e:
            # Mark job failed if tracking
            if self.job_repository and hasattr(self, "_current_job_id"):
                await self.job_repository.fail_job(self._current_job_id, str(e))

            # Clean up partially created chunks on failure
            for chunk_id in chunk_ids:
                try:  # noqa: SIM105 -- explicit error handling preferred
                    await self.knowledge_store.delete_chunk(chunk_id)
                except Exception:  # noqa: S110 -- intentional silent exception handling
                    # Best effort cleanup
                    pass

            # Re-raise with context
            raise ValueError(f"Failed to ingest text: {e}") from e

    async def ingest_url(
        self,
        url: str,
        tenant_id: str | None = None,
        connector_id: str | None = None,
        user_id: str | None = None,
        roles: list[str] | None = None,
        groups: list[str] | None = None,
        tags: list[str] | None = None,
        knowledge_type: KnowledgeType | None = None,
        priority: int = 0,
        job_id: str | None = None,
        scope_type: str = "instance",  # Three-tier scope: global, type, instance
        connector_type_scope: str | None = None,  # e.g. "kubernetes" for type-scoped docs
    ) -> list[str]:
        """
        Ingest content from a web URL.

        Fetches the URL, extracts text (HTML -> markdown, PDF, or plain text),
        then processes through the standard chunking + embedding pipeline.

        Args:
            url: Web URL to crawl and ingest
            tenant_id: Tenant ID
            connector_id: Connector ID for scoping
            user_id: User ID
            roles: Required roles for access
            groups: Required groups for access
            tags: Tags for categorization
            knowledge_type: Type of knowledge
            priority: Search ranking priority
            job_id: Optional job ID for progress tracking

        Returns:
            List of created chunk IDs
        """
        roles = roles or []
        groups = groups or []
        tags = tags or []

        # Track job if repository available
        if self.job_repository and job_id:
            await self.job_repository.update_status(job_id, "processing")
            logger.info("url_ingestion_started", job_id=job_id, url=url)

        chunk_ids: list[str] = []

        try:
            # Stage 1: Fetching + extracting content from URL
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.EXTRACTING,
                progress=0.0,
                message=f"Fetching content from {url}...",
            )

            import httpx

            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(30.0),
            ) as client:
                response = await client.get(
                    url,
                    headers={
                        "User-Agent": "MEHO-Knowledge-Crawler/1.0 (documentation ingestion)",
                        "Accept": "text/html,application/xhtml+xml,text/plain,application/pdf",
                    },
                )
                response.raise_for_status()

            content_type = response.headers.get("content-type", "").lower()
            is_structured = (
                "text/html" in content_type
                or "application/xhtml" in content_type
                or "application/pdf" in content_type
            )

            if is_structured:
                # HTML/PDF: Use converter for structure-aware conversion + chunking
                if "application/pdf" in content_type:
                    mime = "application/pdf"
                else:
                    mime = "text/html"

                if _use_docling:
                    # Docling path: subprocess isolation for PyTorch memory management
                    import asyncio

                    from meho_app.core.config import get_config

                    config = get_config()

                    # D-05: Heartbeat callback for URL ingestion progress
                    async def _heartbeat_url(message: str) -> None:
                        await self._update_job_stage(
                            job_id,
                            stage=IngestionStage.EXTRACTING,
                            progress=0.5,
                            message=message,
                        )

                    _loop = asyncio.get_event_loop()

                    def _sync_heartbeat_url(message: str) -> None:
                        future = asyncio.run_coroutine_threadsafe(_heartbeat_url(message), _loop)
                        future.add_done_callback(
                            lambda f: (
                                logger.warning("heartbeat_failed", error=str(f.exception()))
                                if f.exception()
                                else None
                            )
                        )

                    from meho_app.modules.knowledge.document_converter import (
                        _get_pdf_page_count,
                    )

                    is_large_url_pdf = (
                        mime == "application/pdf"
                        and _get_pdf_page_count(response.content) > config.ingestion_page_batch_size
                    )

                    if is_large_url_pdf:
                        doc = await convert_pdf_batched_in_subprocesses(
                            file_bytes=response.content,
                            filename=url,
                            mime_type=mime,
                            memory_limit_mb=config.ingestion_memory_limit_mb,
                            page_batch_size=config.ingestion_page_batch_size,
                            ocr_enabled=config.ingestion_ocr_enabled,
                            timeout_seconds_per_batch=600,
                            on_heartbeat=_sync_heartbeat_url,
                        )
                    else:
                        doc = await convert_file_in_subprocess(
                            file_bytes=response.content,
                            filename=url,
                            mime_type=mime,
                            memory_limit_mb=config.ingestion_memory_limit_mb,
                            ocr_enabled=config.ingestion_ocr_enabled,
                            timeout_seconds=600,
                            on_heartbeat=_sync_heartbeat_url,
                        )
                else:
                    # Lightweight path: direct in-process conversion (no PyTorch, no subprocess)
                    doc = self.docling_converter.convert_file(
                        response.content, url, mime
                    )
                document_text = self.docling_converter.get_full_text(doc)
            else:
                # Plain text: no Docling needed
                document_text = response.text
                doc = None

            await self._update_job_stage(
                job_id,
                stage=IngestionStage.EXTRACTING,
                progress=1.0,
                message=f"Extracted {len(document_text)} characters",
            )

            # D-05: Generate document-level summary + connector context
            connector_type, connector_name = await self._resolve_connector_context(connector_id)
            document_summary = await generate_document_summary(
                document_text, connector_type, connector_name
            )
            chunk_prefix = build_chunk_prefix(connector_type, connector_name, document_summary)
            logger.info(
                "url_document_summary_generated",
                has_summary=bool(document_summary),
                prefix_length=len(chunk_prefix),
                connector_type=connector_type,
                url=url,
            )

            # Stage 2: Chunking
            await self._update_job_stage(
                job_id, stage=IngestionStage.CHUNKING, progress=0.0, message="Chunking content..."
            )

            if doc is not None:
                # Structured content: use Docling HybridChunker with prefix enrichment
                chunks_with_context = self.docling_converter.chunk_document(
                    doc, chunk_prefix=chunk_prefix
                )
            else:
                # Plain text: use TextChunker, prepend prefix manually
                raw_chunks = self.chunker.chunk_document_with_structure(
                    text=document_text, document_name=url, detect_headings=True
                )
                if chunk_prefix:
                    chunks_with_context = [
                        (chunk_prefix + "\n\n" + text, ctx) for text, ctx in raw_chunks
                    ]
                else:
                    chunks_with_context = raw_chunks

            if not chunks_with_context:
                raise ValueError(f"No text extracted from URL {url}")

            total_chunks = len(chunks_with_context)

            await self._update_job_stage(
                job_id,
                stage=IngestionStage.CHUNKING,
                progress=1.0,
                message=f"Created {total_chunks} chunks",
                total_chunks=total_chunks,
            )

            # Stage 3: Embedding + storing
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.EMBEDDING,
                progress=0.0,
                message=f"Processing chunk 0 of {total_chunks}...",
                total_chunks=total_chunks,
            )

            embedding_start_time = datetime.now(tz=UTC)

            for i, (chunk_text, context) in enumerate(chunks_with_context):
                metadata = self.metadata_extractor.extract_metadata(
                    text=chunk_text, document_name=url, _chunk_index=i, document_context=context
                )

                chunk_create = KnowledgeChunkCreate(
                    text=chunk_text,
                    tenant_id=tenant_id,
                    connector_id=connector_id,
                    user_id=user_id,
                    roles=roles,
                    groups=groups,
                    tags=tags,
                    source_uri=url,
                    # Three-tier scoping
                    scope_type=scope_type,
                    connector_type_scope=connector_type_scope,
                    knowledge_type=knowledge_type or KnowledgeType.DOCUMENTATION,
                    priority=priority,
                    expires_at=None,
                    search_metadata=metadata,
                )

                try:
                    chunk = await self.knowledge_store.add_chunk(chunk_create)
                    chunk_ids.append(chunk.id)

                    if (i + 1) % 10 == 0 or (i + 1) == total_chunks:
                        stage_progress = (i + 1) / total_chunks
                        overall_progress = 0.20 + (0.75 * stage_progress)
                        eta = self._calculate_eta(total_chunks, i + 1, embedding_start_time)

                        await self._update_job_stage(
                            job_id,
                            stage=IngestionStage.EMBEDDING,
                            progress=stage_progress,
                            overall_progress=overall_progress,
                            message=f"Processing chunk {i + 1} of {total_chunks}...",
                            chunks_processed=i + 1,
                            estimated_completion=eta,
                        )

                except Exception as chunk_error:
                    raise ValueError(
                        f"Failed to create URL chunk {len(chunk_ids) + 1} of {total_chunks}: {chunk_error}"
                    ) from chunk_error

            # Finalize
            await self._update_job_stage(
                job_id, stage=IngestionStage.STORING, progress=1.0, message="Complete"
            )

            if self.job_repository and job_id:
                await self.job_repository.complete_job(job_id, chunk_ids)
                logger.info(
                    "url_ingestion_completed",
                    job_id=job_id,
                    url=url,
                    num_chunks=len(chunk_ids),
                )

            return chunk_ids

        except Exception as e:
            error_msg = str(e) or f"{type(e).__name__}: {e!r}"
            # Mark job as failed
            if self.job_repository and job_id:
                error_details = {
                    "exception_type": type(e).__name__,
                    "exception_message": error_msg,
                    "traceback": traceback.format_exc(),
                    "url": url,
                }
                await self.job_repository.fail_job(
                    job_id=job_id,
                    error=error_msg,
                    error_details=error_details,
                )
                logger.error(
                    "url_ingestion_failed",
                    job_id=job_id,
                    url=url,
                    error=f"[{type(e).__name__}] {error_msg}",
                    exc_info=True,
                )

            # Clean up partially created chunks
            for chunk_id in chunk_ids:
                with contextlib.suppress(Exception):
                    await self.knowledge_store.delete_chunk(chunk_id)

            raise ValueError(f"Failed to ingest URL {url}: {e}") from e

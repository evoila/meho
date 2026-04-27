# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Knowledge ingestion service.

Orchestrates document processing: extraction, chunking, embedding, and storage.
NOW with job tracking for progress visibility!
"""

import asyncio
import contextlib
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from meho_app.core.config import get_config
from meho_app.core.feature_flags import get_feature_flags
from meho_app.core.otel import get_logger
from meho_app.modules.knowledge.chunking import (
    TextChunker,
)  # Still needed for ingest_text()
from meho_app.modules.knowledge.job_models import IngestionStage
from meho_app.modules.knowledge.job_repository import IngestionJobRepository
from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.metadata_extraction import MetadataExtractor
from meho_app.modules.knowledge.object_storage import ObjectStorage
from meho_app.modules.knowledge.schemas import (
    ChunkMetadata,
    KnowledgeChunkCreate,
    KnowledgeType,
)

CHUNK_MAX_RETRIES = 3

# Flag-based converter selection: when use_docling=false, avoid importing
# docling_adapter.py (which imports Docling/PyTorch at module level).
_use_docling = get_feature_flags().use_docling

if _use_docling:
    from meho_app.modules.knowledge.docling_adapter import (
        SUPPORTED_MIME_TYPES,
        DoclingWrapperAdapter,
    )
    from meho_app.modules.knowledge.document_converter import (
        build_chunk_prefix,
        generate_document_summary,
    )
else:
    from meho_app.modules.knowledge.lightweight_converter import (
        SUPPORTED_MIME_TYPES,
        LightweightDocumentConverter,
        build_chunk_prefix,
        generate_document_summary,
    )

if TYPE_CHECKING:
    import pyarrow as pa

logger = get_logger(__name__)

CONTENT_TYPE_PDF = "application/pdf"


@dataclass
class _EmbeddingProgress:
    """Shared progress tracker between the embedding loop and its caller.

    Callers read all three attributes after :meth:`IngestionService._embed_chunks`
    completes (on success) or raises (in the except block) to build resume
    state or persist ``error_chunk_index``. ``last_processed_index`` starts at
    ``start_index - 1`` so that the first iteration moves it to ``start_index``.
    """

    chunk_ids: list[str] = field(default_factory=list)
    skipped_chunks: int = 0
    last_processed_index: int = -1


class IngestionService:
    """Orchestrates document ingestion into knowledge base with job tracking"""

    def __init__(
        self,
        knowledge_store: KnowledgeStore,
        object_storage: ObjectStorage,
        job_repository: IngestionJobRepository | None = None,
        chunker: TextChunker | None = None,
    ) -> None:
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
        if _use_docling:
            config = get_config()
            self.docling_converter = DoclingWrapperAdapter(
                ocr_enabled=config.ingestion_ocr_enabled,
                device=config.ingestion_device,
                num_threads=config.ingestion_num_threads,
                pdf_chunk_pages=config.ingestion_page_batch_size,
                max_workers=config.ingestion_max_workers,
            )
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
        **kwargs: Any,
    ) -> None:
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
            # Commit immediately so the polling endpoint (which uses a separate
            # DB session) can see the progress update. Without this, all stage
            # updates remain uncommitted until ingest_document finishes, causing
            # the frontend progress bar to stay at 0%.
            await self.job_repository.session.commit()

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

    def _persist_markdown(self, storage_key: str, markdown_text: str) -> None:
        """Store the rendered markdown alongside the original file in S3.

        The key is derived from the original storage key by appending ``.md``.
        Best-effort: failures are logged but do not abort ingestion.
        """
        md_key = storage_key + ".md"
        try:
            self.object_storage.upload_document(
                markdown_text.encode("utf-8"), md_key, "text/markdown"
            )
            logger.info("markdown_persisted", key=md_key, chars=len(markdown_text))
        except Exception as e:
            logger.warning("markdown_persist_failed", key=md_key, error=str(e))

    @staticmethod
    def _compute_file_hash(file_bytes: bytes) -> str:
        """Compute SHA-256 hex digest for file identity tracking."""
        import hashlib

        return hashlib.sha256(file_bytes).hexdigest()

    def _checkpoint_key(self, storage_key: str) -> str:
        """Derive the MinIO key for the chunk checkpoint file."""
        return storage_key + ".chunks.json"

    def _save_checkpoint(
        self,
        storage_key: str,
        chunks_with_context: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Serialize chunks_with_context to object storage after conversion."""
        import json

        key = self._checkpoint_key(storage_key)
        payload = json.dumps(
            [{"text": text, "context": ctx} for text, ctx in chunks_with_context],
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            self.object_storage.upload_document(payload, key, "application/json")
            logger.info("checkpoint_saved", key=key, chunks=len(chunks_with_context))
        except Exception as e:
            logger.warning("checkpoint_save_failed", key=key, error=str(e))

    def _load_checkpoint(self, storage_key: str) -> list[tuple[str, dict[str, Any]]] | None:
        """Load a previously saved chunk checkpoint from object storage."""
        import json

        key = self._checkpoint_key(storage_key)
        try:
            data = self.object_storage.download_document(key)
            items = json.loads(data.decode("utf-8"))
            return [(item["text"], item["context"]) for item in items]
        except Exception:
            return None

    def _delete_checkpoint(self, storage_key: str) -> None:
        """Remove the checkpoint file after successful completion."""
        key = self._checkpoint_key(storage_key)
        try:
            self.object_storage.delete_document(key)
            logger.info("checkpoint_deleted", key=key)
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logger.debug("checkpoint_delete_failed", key=key, error=str(exc))

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
                "connector_context_resolution_failed",
                connector_id=connector_id,
                error=str(e),
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

        if mime_type != CONTENT_TYPE_PDF:
            return False, 0  # Only PDFs need offloading (non-PDF is lightweight)

        page_count = self._get_pdf_page_count(file_bytes)

        if page_count <= config.ingestion_offload_threshold_pages:
            return False, page_count

        return True, page_count

    @staticmethod
    def _get_pdf_page_count(file_bytes: bytes) -> int:
        """Get page count from PDF bytes using pypdfium2 (fast, no Docling)."""
        import io

        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(io.BytesIO(file_bytes))
        n = len(doc)
        doc.close()
        return n

    async def _dispatch_to_worker(
        self,
        file_bytes: bytes,  # noqa: ARG002 -- kept for interface compat
        filename: str,  # noqa: ARG002 -- kept for interface compat
        mime_type: str,  # noqa: ARG002 -- kept for interface compat
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
        doc_version: str | None = None,
        family_id: str | None = None,
        chunk_prefix: str = "",
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
            elif status.state in (
                JobState.FAILED,
                JobState.CANCELLED,
                JobState.TIMEOUT,
            ):
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
            doc_version=doc_version,
            family_id=family_id,
        )

        return chunk_ids

    async def _import_arrow_chunks(  # NOSONAR (cognitive complexity)
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
        doc_version: str | None = None,
        family_id: str | None = None,
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
        col_page_numbers = table.column("page_numbers")
        col_document_name = table.column("document_name")
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
        batch: list[tuple[KnowledgeChunkCreate, list[float] | None]] = []
        batch_size = 100

        for i in range(table.num_rows):
            embedding = col_embedding[i].as_py()
            page_numbers = col_page_numbers[i].as_py() or []
            page_start = page_numbers[0] if page_numbers else 0
            page_end = page_numbers[-1] if page_numbers else 0
            metadata = ChunkMetadata(
                chapter=col_chapter[i].as_py() or None,
                section=col_section[i].as_py() or None,
                subsection=col_subsection[i].as_py() or None,
                document_name=col_document_name[i].as_py() or None,
                page_number=page_start,
                page_numbers=page_numbers,
                page_start=page_start,
                page_end=page_end,
                content_type=col_content_type[i].as_py() or "description",  # type: ignore[arg-type]
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
                doc_version=doc_version,
                family_id=family_id,
                knowledge_type=knowledge_type or KnowledgeType.DOCUMENTATION,
                priority=priority,
                search_metadata=metadata,
            )

            batch.append((chunk_create, embedding))

            if len(batch) >= batch_size:
                ids = await self.knowledge_store.repository.create_chunks_batch(batch)  # type: ignore[attr-defined]
                chunk_ids.extend(ids)
                batch.clear()

        # Flush remaining
        if batch:
            ids = await self.knowledge_store.repository.create_chunks_batch(batch)  # type: ignore[attr-defined]
            chunk_ids.extend(ids)

        return chunk_ids

    async def _maybe_offload_to_worker(
        self,
        *,
        file_bytes: bytes,
        filename: str,
        mime_type: str,
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
        doc_version: str | None,
        family_id: str | None,
    ) -> list[str] | None:
        """If the document qualifies for an ephemeral worker, dispatch it.

        Returns the list of chunk ids when the offload path succeeded and the
        job has been finalized, or ``None`` when the document should continue
        through the in-process pipeline (either because offloading is disabled
        or because the dispatch itself failed).
        """
        should_offload, offload_page_count = self._should_offload(mime_type, file_bytes)
        if not should_offload:
            return None

        try:
            connector_type, connector_name = await self._resolve_connector_context(connector_id)
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
                doc_version=doc_version,
                family_id=family_id,
                chunk_prefix=chunk_prefix,
            )
        except Exception as offload_error:
            logger.warning(
                "ephemeral_worker_dispatch_failed",
                error=str(offload_error),
                filename=filename,
                page_count=offload_page_count,
            )
            # Fall through to the local conversion path.
            return None

        await self._update_job_stage(
            job_id,
            stage=IngestionStage.STORING,
            progress=1.0,
            message=f"Imported {len(offload_chunk_ids)} chunks from worker",
            total_chunks=len(offload_chunk_ids),
            chunks_created=len(offload_chunk_ids),
        )

        str_chunk_ids = [str(cid) for cid in offload_chunk_ids]
        if self.job_repository and job_id:
            await self.job_repository.complete_job(job_id, str_chunk_ids)
            await self.job_repository.session.commit()

        return str_chunk_ids

    async def _prepare_conversion(
        self,
        *,
        file_bytes: bytes,
        filename: str,
        mime_type: str,
    ) -> tuple[str, Any | None, Any | None, bool]:
        """Run the appropriate text-extraction path for this document.

        Returns ``(document_text, docling_result, lightweight_doc, is_structured)``.
        Exactly one of ``docling_result`` / ``lightweight_doc`` is populated for
        structured content; both are ``None`` for plain text.
        """
        is_structured = mime_type in SUPPORTED_MIME_TYPES
        docling_result: Any | None = None
        lightweight_doc: Any | None = None

        if is_structured and _use_docling:
            docling_result = await self.docling_converter.convert_file_async(
                file_bytes, filename, mime_type
            )
            document_text = self.docling_converter.get_full_text(docling_result)
        elif is_structured:
            lightweight_doc = self.docling_converter.convert_file(file_bytes, filename, mime_type)
            document_text = self.docling_converter.get_full_text(lightweight_doc)
        else:
            try:
                document_text = file_bytes.decode("utf-8")
            except UnicodeDecodeError:
                document_text = file_bytes.decode("latin-1")

        return document_text, docling_result, lightweight_doc, is_structured

    def _chunk_document_text(
        self,
        *,
        document_text: str,
        docling_result: Any | None,
        lightweight_doc: Any | None,
        document_name: str,
        chunk_prefix: str,
        is_structured: bool,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Chunk the extracted text with the appropriate chunker.

        Raises ``ValueError`` if no chunks were produced.
        """
        if is_structured and _use_docling and docling_result is not None:
            chunks_with_context = self.docling_converter.chunk_document(
                docling_result,
                chunk_prefix=chunk_prefix,
            )
        elif lightweight_doc is not None:
            chunks_with_context = self.docling_converter.chunk_document(
                lightweight_doc, chunk_prefix=chunk_prefix
            )
        else:
            chunks_with_context = self.chunker.chunk_document_with_structure(
                text=document_text, document_name=document_name, detect_headings=True
            )

        if not chunks_with_context:
            raise ValueError(f"No text extracted from document {document_name}")

        return chunks_with_context

    async def _finalize_ingest(
        self,
        *,
        job_id: str | None,
        storage_key: str | None,
        chunk_ids: list[str],
        skipped_chunks: int,
        tenant_id: str | None,
    ) -> None:
        """Mark the job complete, log success, and delete the checkpoint.

        ``storage_key`` may be ``None`` for paths that do not use a checkpoint
        (e.g. URL ingestion). In that case the checkpoint cleanup is skipped.
        """
        await self._update_job_stage(
            job_id,
            stage=IngestionStage.STORING,
            progress=0.5,
            message="Finalizing...",
        )

        logger.info(
            "knowledge_search_state_finalized",
            tenant_id=tenant_id,
            num_chunks=len(chunk_ids),
            detail="Chunk embeddings and metadata stored",
        )

        if self.job_repository and job_id:
            await self.job_repository.complete_job(job_id, chunk_ids)
            await self.job_repository.session.commit()
            skipped_msg = f" ({skipped_chunks} skipped)" if skipped_chunks else ""
            logger.info(
                f"Job {job_id}: Completed successfully with {len(chunk_ids)} chunks{skipped_msg}"
            )

        if storage_key:
            self._delete_checkpoint(storage_key)

    async def _embed_chunks(
        self,
        *,
        chunks_with_context: list[tuple[str, dict[str, Any]]],
        start_index: int,
        progress: _EmbeddingProgress,
        job_id: str | None,
        document_name: str,
        source_uri: str | None,
        tenant_id: str | None,
        connector_id: str | None,
        user_id: str | None,
        roles: list[str],
        groups: list[str],
        tags: list[str],
        scope_type: str,
        connector_type_scope: str | None,
        doc_version: str | None,
        family_id: str | None,
        knowledge_type: KnowledgeType | None,
        priority: int,
        expires_at: datetime | None,
        embedding_start_time: datetime,
    ) -> None:
        """Shared per-chunk embedding loop used by both initial ingest and resume.

        Mutates ``progress`` in place so that callers (both the happy-path
        ``ingest_document`` and its ``except`` block, and ``resume_document_ingestion``)
        can read ``chunk_ids``, ``skipped_chunks``, and ``last_processed_index``
        even when this coroutine raises ``ValueError`` on too many chunk failures.

        ``progress.last_processed_index`` should be initialized by the caller to
        ``start_index - 1`` so the first iteration advances it to ``start_index``.
        This matches how the initial-ingest except block maps the value to
        ``error_chunk_index`` for resume.
        """
        total_chunks = len(chunks_with_context)

        for i in range(start_index, total_chunks):
            progress.last_processed_index = i
            chunk_text, context = chunks_with_context[i]

            metadata = self.metadata_extractor.extract_metadata(
                text=chunk_text,
                document_name=document_name,
                _chunk_index=i,
                document_context=context,
            )

            logger.debug(
                "metadata_extracted",
                chunk_index=i,
                chapter=metadata.chapter,
                section=metadata.section,
                resource_type=metadata.resource_type,
                content_type=(metadata.content_type.value if metadata.content_type else None),
                endpoint_path=metadata.endpoint_path,
                has_json_example=metadata.has_json_example,
                keywords_count=len(metadata.keywords),
            )

            chunk_create = KnowledgeChunkCreate(
                text=chunk_text,
                tenant_id=tenant_id,
                connector_id=connector_id,
                user_id=user_id,
                roles=roles,
                groups=groups,
                tags=tags,
                source_uri=source_uri,
                scope_type=scope_type,
                connector_type_scope=connector_type_scope,
                doc_version=doc_version,
                family_id=family_id,
                knowledge_type=knowledge_type or KnowledgeType.DOCUMENTATION,
                priority=priority,
                expires_at=expires_at,
                search_metadata=metadata,
            )

            chunk_created = False
            for attempt in range(CHUNK_MAX_RETRIES):
                try:
                    chunk = await self.knowledge_store.add_chunk(chunk_create)
                    # M6: always store chunk ids as ``str`` so downstream code
                    # that writes to ``ingestion_jobs.chunk_ids`` (JSONB) and
                    # reads it back on resume sees a single consistent type.
                    progress.chunk_ids.append(str(chunk.id))
                    chunk_created = True
                    break
                except Exception as exc:
                    if attempt < CHUNK_MAX_RETRIES - 1:
                        logger.warning(
                            "chunk_creation_retry",
                            chunk_index=i + 1,
                            attempt=attempt + 1,
                            error=str(exc),
                        )
                        await asyncio.sleep(1.0 * (attempt + 1))
                    else:
                        progress.skipped_chunks += 1
                        logger.warning(
                            "chunk_creation_failed_skipping",
                            chunk_index=i + 1,
                            total_chunks=total_chunks,
                            error=str(exc),
                        )
                        if progress.skipped_chunks > max(10, total_chunks // 10):
                            raise ValueError(
                                f"Too many chunk failures "
                                f"({progress.skipped_chunks}/{total_chunks}), aborting"
                            ) from exc

            if chunk_created and ((i + 1) % 10 == 0 or (i + 1) == total_chunks):
                stage_progress = (i + 1) / total_chunks
                overall_progress = 0.20 + (0.75 * stage_progress)
                # For resume, ETA is relative to the resumed window so the
                # reported rate isn't diluted by the time before the failure.
                # For initial ingest (``start_index == 0``) this is identical
                # to a full-range ETA.
                eta = self._calculate_eta(
                    total_chunks - start_index,
                    i + 1 - start_index,
                    embedding_start_time,
                )

                await self._update_job_stage(
                    job_id,
                    stage=IngestionStage.EMBEDDING,
                    progress=stage_progress,
                    overall_progress=overall_progress,
                    message=f"Processing chunk {i + 1} of {total_chunks}...",
                    chunks_processed=i + 1,
                    estimated_completion=eta,
                )

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
        job_id: (str | None) = None,  # If provided, update existing job instead of creating new one
        system_id: (
            str | None
        ) = None,  # Deprecated — kept for backward compat with connector operation code
        scope_type: str = "instance",  # Three-tier scope: global, type, instance
        connector_type_scope: (str | None) = None,  # e.g. "kubernetes" for type-scoped docs
        doc_version: (str | None) = None,  # Documentation version label (e.g. "v8")
        family_id: (str | None) = None,  # Document family identifier (groups all versions)
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

        if self.job_repository and job_id:
            await self.job_repository.update_status(job_id, "processing")
            await self.job_repository.session.commit()
            logger.info(f"Job {job_id}: Starting document processing")

        # Stage 1: upload to object storage (5% of total time).
        await self._update_job_stage(
            job_id,
            stage=IngestionStage.UPLOADING,
            progress=0.0,
            message="Uploading file to storage...",
        )

        doc_id = str(uuid.uuid4())
        storage_key = f"documents/{tenant_id or 'global'}/{doc_id}/{filename}"
        storage_uri = self.object_storage.upload_document(file_bytes, storage_key, mime_type)
        file_hash = self._compute_file_hash(file_bytes)

        if self.job_repository and job_id:
            await self.job_repository.update_file_hash(job_id, file_hash, storage_key)
            await self.job_repository.session.commit()

        await self._update_job_stage(
            job_id,
            stage=IngestionStage.UPLOADING,
            progress=1.0,
            message="Upload complete",
        )

        progress = _EmbeddingProgress()
        # Track which stage we're in so the except block can attribute failure
        # without scanning ``locals()``.
        current_stage: str = IngestionStage.UPLOADING.value

        try:
            current_stage = IngestionStage.EXTRACTING.value
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.EXTRACTING,
                progress=0.0,
                message="Extracting text from document...",
            )

            is_structured = mime_type in SUPPORTED_MIME_TYPES

            # Ephemeral-worker offload: short-circuit for large PDFs when the
            # feature flag + non-local backend + page-count heuristic agree.
            if is_structured and _use_docling:
                offloaded_ids = await self._maybe_offload_to_worker(
                    file_bytes=file_bytes,
                    filename=filename,
                    mime_type=mime_type,
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
                    doc_version=doc_version,
                    family_id=family_id,
                )
                if offloaded_ids is not None:
                    return offloaded_ids

            (
                document_text,
                docling_result,
                lightweight_doc,
                is_structured,
            ) = await self._prepare_conversion(
                file_bytes=file_bytes, filename=filename, mime_type=mime_type
            )

            await self._update_job_stage(
                job_id,
                stage=IngestionStage.EXTRACTING,
                progress=1.0,
                message=f"Extracted {len(document_text)} characters",
            )

            self._persist_markdown(storage_key, document_text)

            # D-05: document-level summary + connector context used as chunk prefix.
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

            if document_summary and job_id and self.job_repository:
                await self.job_repository.save_document_summary(job_id, document_summary)
                await self.job_repository.session.commit()

            current_stage = IngestionStage.CHUNKING.value
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.CHUNKING,
                progress=0.0,
                message="Chunking document...",
            )

            chunks_with_context = self._chunk_document_text(
                document_text=document_text,
                docling_result=docling_result,
                lightweight_doc=lightweight_doc,
                document_name=filename,
                chunk_prefix=chunk_prefix,
                is_structured=is_structured,
            )
            total_chunks = len(chunks_with_context)

            await self._update_job_stage(
                job_id,
                stage=IngestionStage.CHUNKING,
                progress=1.0,
                message=f"Created {total_chunks} chunks",
                total_chunks=total_chunks,
            )

            self._save_checkpoint(storage_key, chunks_with_context)

            current_stage = IngestionStage.EMBEDDING.value
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.EMBEDDING,
                progress=0.0,
                message=f"Processing chunk 0 of {total_chunks}...",
                total_chunks=total_chunks,
            )

            embedding_start_time = datetime.now(tz=UTC)
            # The shared loop starts from index 0 on initial ingest. Seeding
            # ``last_processed_index`` at -1 matches the "no chunk yet" state
            # so an immediate failure attributes ``error_chunk_index`` to the
            # correct (first) chunk.
            progress.last_processed_index = -1

            await self._embed_chunks(
                chunks_with_context=chunks_with_context,
                start_index=0,
                progress=progress,
                job_id=job_id,
                document_name=filename,
                source_uri=storage_uri,
                tenant_id=tenant_id,
                connector_id=connector_id,
                user_id=user_id,
                roles=roles,
                groups=groups,
                tags=tags,
                scope_type=scope_type,
                connector_type_scope=connector_type_scope,
                doc_version=doc_version,
                family_id=family_id,
                knowledge_type=knowledge_type,
                priority=priority,
                expires_at=None,  # Documents never expire (use EVENT for temporary).
                embedding_start_time=embedding_start_time,
            )

            current_stage = IngestionStage.STORING.value
            await self._finalize_ingest(
                job_id=job_id,
                storage_key=storage_key,
                chunk_ids=progress.chunk_ids,
                skipped_chunks=progress.skipped_chunks,
                tenant_id=tenant_id,
            )
            return progress.chunk_ids

        except Exception as e:
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

            # ``error_chunk_index`` only has meaning inside the EMBEDDING stage.
            error_chunk_index = (
                progress.last_processed_index
                if current_stage == IngestionStage.EMBEDDING.value
                and progress.last_processed_index >= 0
                else None
            )

            # Fall back to a helpful repr when the exception has an empty str
            # (e.g. OOM-killed subprocess on macOS sends no message).
            error_msg = str(e) or f"{type(e).__name__}: {e!r}"

            if self.job_repository and job_id:
                await self.job_repository.fail_job(
                    job_id=job_id,
                    error=error_msg,
                    error_stage=current_stage,
                    error_chunk_index=error_chunk_index,
                    error_details=error_details,
                )
                await self.job_repository.session.commit()
                logger.error(
                    f"Job {job_id}: Failed at stage {current_stage} - "
                    f"[{type(e).__name__}] {error_msg}",
                    exc_info=True,
                )

                # Keep partial results so the job can be resumed later.
                await self.job_repository.save_partial_chunk_ids(job_id, progress.chunk_ids)
                await self.job_repository.session.commit()

            logger.info(
                "partial_results_kept",
                job_id=job_id,
                chunks_created=len(progress.chunk_ids),
                storage_key=storage_key,
            )

            raise ValueError(
                f"Failed to ingest document {filename}: [{type(e).__name__}] {error_msg}"
            ) from e

    async def resume_document_ingestion(
        self,
        job_id: str,
        tenant_id: str,
    ) -> list[str]:
        """Resume a failed document ingestion job from its checkpoint.

        Loads the previously saved chunk checkpoint from MinIO and re-enters
        the shared embedding loop via :meth:`_embed_chunks` starting at the
        recorded ``error_chunk_index`` (or ``len(chunk_ids)`` as a fallback).
        """
        if not self.job_repository:
            raise ValueError("Job repository required for resume")

        job = await self.job_repository.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        if str(job.tenant_id) != tenant_id:
            raise ValueError("Tenant mismatch")
        if job.status != "failed":
            raise ValueError(f"Job is not in failed state (status={job.status})")
        if job.error_stage != IngestionStage.EMBEDDING.value:
            raise ValueError(
                f"Only embedding-stage failures are resumable (failed at {job.error_stage})"
            )
        if not job.storage_key:
            raise ValueError("Job has no storage_key -- cannot locate checkpoint")

        # Resolve ORM attributes up-front so the shared helper sees plain
        # strings rather than ``Column[str]`` (mypy) / SQLAlchemy attributes.
        storage_key: str = str(job.storage_key)
        job_filename: str = str(job.filename) if job.filename else "unknown"
        job_scope_type: str = str(job.scope_type) if job.scope_type else "global"
        job_connector_type_scope: str | None = (
            str(job.connector_type_scope) if job.connector_type_scope else None
        )
        job_doc_version: str | None = str(job.doc_version) if job.doc_version else None
        job_family_id: str | None = str(job.family_id) if job.family_id else None
        job_connector_id: str | None = str(job.connector_id) if job.connector_id else None
        job_knowledge_type_raw: str | None = str(job.knowledge_type) if job.knowledge_type else None
        job_knowledge_type = (
            KnowledgeType(job_knowledge_type_raw)
            if job_knowledge_type_raw
            else KnowledgeType.DOCUMENTATION
        )
        job_tags: list[str] = list(job.tags or [])

        chunks_with_context = self._load_checkpoint(storage_key)
        if chunks_with_context is None:
            raise ValueError(f"Checkpoint not found at {self._checkpoint_key(storage_key)}")

        # Blocker #7: use the recorded chunk index as the high-water-mark
        # instead of ``len(existing_chunk_ids)``. The count-based form silently
        # re-processes chunks that were skipped before the failure.
        raw_chunk_ids: list[Any] = list(job.chunk_ids or [])
        existing_chunk_ids: list[str] = [str(cid) for cid in raw_chunk_ids]
        recorded_index = job.error_chunk_index
        start_index = int(recorded_index) if recorded_index is not None else len(existing_chunk_ids)
        total_chunks = len(chunks_with_context)

        if start_index >= total_chunks:
            raise ValueError(f"All {total_chunks} chunks already created -- nothing to resume")

        logger.info(
            "resume_ingestion_started",
            job_id=job_id,
            start_index=start_index,
            total_chunks=total_chunks,
        )

        # The resume route has already claimed this job with an atomic
        # ``mark_resuming`` UPDATE before enqueuing the background task, so we
        # no longer flip the status here.

        progress = _EmbeddingProgress(
            chunk_ids=list(existing_chunk_ids),
            last_processed_index=start_index - 1,
        )
        embedding_start_time = datetime.now(tz=UTC)

        await self._update_job_stage(
            job_id,
            stage=IngestionStage.EMBEDDING,
            progress=start_index / total_chunks,
            overall_progress=0.20 + (0.75 * (start_index / total_chunks)),
            message=f"Resuming from chunk {start_index + 1} of {total_chunks}...",
            total_chunks=total_chunks,
        )

        try:
            await self._embed_chunks(
                chunks_with_context=chunks_with_context,
                start_index=start_index,
                progress=progress,
                job_id=job_id,
                document_name=job_filename,
                source_uri=f"s3://{self.object_storage.bucket}/{storage_key}",
                tenant_id=tenant_id,
                connector_id=job_connector_id,
                user_id=None,
                roles=[],
                groups=[],
                tags=job_tags,
                scope_type=job_scope_type,
                connector_type_scope=job_connector_type_scope,
                doc_version=job_doc_version,
                family_id=job_family_id,
                knowledge_type=job_knowledge_type,
                priority=0,
                expires_at=None,
                embedding_start_time=embedding_start_time,
            )
        except Exception as exc:
            # The resume route has no outer try/except, so we own the fail_job
            # bookkeeping here (matches pre-refactor behaviour).
            error_index = (
                progress.last_processed_index if progress.last_processed_index >= 0 else None
            )
            await self.job_repository.save_partial_chunk_ids(job_id, progress.chunk_ids)
            await self.job_repository.fail_job(
                job_id=job_id,
                error=str(exc),
                error_stage=IngestionStage.EMBEDDING.value,
                error_chunk_index=error_index,
            )
            await self.job_repository.session.commit()
            raise

        await self._finalize_ingest(
            job_id=job_id,
            storage_key=storage_key,
            chunk_ids=progress.chunk_ids,
            skipped_chunks=progress.skipped_chunks,
            tenant_id=tenant_id,
        )

        skipped_msg = f" ({progress.skipped_chunks} skipped)" if progress.skipped_chunks else ""
        logger.info(
            f"Job {job_id}: Resume completed with {len(progress.chunk_ids)} chunks{skipped_msg}"
        )

        return progress.chunk_ids

    async def ingest_text(  # NOSONAR (cognitive complexity)
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
        connector_type_scope: (str | None) = None,  # e.g. "kubernetes" for type-scoped docs
        doc_version: (str | None) = None,  # Documentation version label
        family_id: (str | None) = None,  # Document family identifier
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
        skipped_chunks = 0

        try:
            # Chunk the text with structure tracking
            chunks_with_context = self.chunker.chunk_document_with_structure(
                text=text,
                document_name=source_uri or "text-input",
                detect_headings=True,
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
                    chunk_index=i,  # type: ignore[call-arg]
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
                    doc_version=doc_version,
                    family_id=family_id,
                    # Lifecycle fields
                    knowledge_type=knowledge_type or KnowledgeType.DOCUMENTATION,
                    priority=priority,
                    expires_at=expires_at,
                    # Rich metadata for enhanced retrieval
                    search_metadata=metadata,
                )

                chunk_created = False
                for attempt in range(CHUNK_MAX_RETRIES):
                    try:
                        chunk = await self.knowledge_store.add_chunk(chunk_create)
                        chunk_ids.append(str(chunk.id))
                        chunk_created = True
                        break
                    except Exception as exc:
                        if attempt < CHUNK_MAX_RETRIES - 1:
                            logger.warning(
                                "chunk_creation_retry",
                                chunk_index=i + 1,
                                attempt=attempt + 1,
                                error=str(exc),
                            )
                            await asyncio.sleep(1.0 * (attempt + 1))
                        else:
                            skipped_chunks += 1
                            logger.warning(
                                "chunk_creation_failed_skipping",
                                chunk_index=i + 1,
                                total_chunks=len(chunks_with_context),
                                error=str(exc),
                            )
                            if skipped_chunks > max(10, len(chunks_with_context) // 10):
                                raise ValueError(
                                    f"Too many chunk failures ({skipped_chunks}/{len(chunks_with_context)}), aborting"
                                ) from exc

                if chunk_created and self.job_repository and hasattr(self, "_current_job_id"):
                    await self.job_repository.update_progress(
                        self._current_job_id,
                        chunks_processed=i + 1,
                        chunks_created=len(chunk_ids),
                    )

            # Mark job complete if tracking
            if self.job_repository and hasattr(self, "_current_job_id"):
                await self.job_repository.complete_job(self._current_job_id, chunk_ids)
                await self.job_repository.session.commit()

            return chunk_ids

        except Exception as e:
            # Mark job failed if tracking
            if self.job_repository and hasattr(self, "_current_job_id"):
                await self.job_repository.fail_job(self._current_job_id, str(e))
                await self.job_repository.session.commit()

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
        connector_type_scope: (str | None) = None,  # e.g. "kubernetes" for type-scoped docs
        doc_version: (str | None) = None,  # Documentation version label
        family_id: (str | None) = None,  # Document family identifier
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
            await self.job_repository.session.commit()
            logger.info("url_ingestion_started", job_id=job_id, url=url)

        chunk_ids: list[str] = []
        skipped_chunks = 0

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
                or CONTENT_TYPE_PDF in content_type
            )

            if is_structured:
                # HTML/PDF: Use converter for structure-aware conversion + chunking
                mime = CONTENT_TYPE_PDF if CONTENT_TYPE_PDF in content_type else "text/html"

                if _use_docling:
                    # DoclingWrapper handles subprocess isolation internally
                    url_result = await self.docling_converter.convert_file_async(
                        response.content, url, mime
                    )
                    document_text = self.docling_converter.get_full_text(url_result)
                else:
                    # Lightweight path: direct in-process conversion (no PyTorch, no subprocess)
                    doc = self.docling_converter.convert_file(response.content, url, mime)
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

            url_md_key = f"documents/{tenant_id or 'global'}/{job_id or 'unknown'}/url.md"
            self._persist_markdown(url_md_key, document_text)

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

            if document_summary and job_id and self.job_repository:
                await self.job_repository.save_document_summary(job_id, document_summary)
                await self.job_repository.session.commit()

            # Stage 2: Chunking
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.CHUNKING,
                progress=0.0,
                message="Chunking content...",
            )

            if is_structured and _use_docling:
                # DoclingWrapper HierarchicalChunker with prefix enrichment
                chunks_with_context = self.docling_converter.chunk_document(
                    url_result,
                    chunk_prefix=chunk_prefix,
                )
            elif is_structured:
                if doc is None:
                    raise ValueError(f"Lightweight converter returned no document for URL {url}")
                chunks_with_context = self.docling_converter.chunk_document(
                    doc, chunk_prefix=chunk_prefix
                )
            else:
                # Plain text: use TextChunker with metadata-only enrichment
                raw_chunks = self.chunker.chunk_document_with_structure(
                    text=document_text, document_name=url, detect_headings=True
                )
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
                    text=chunk_text,
                    document_name=url,
                    _chunk_index=i,
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
                    source_uri=url,
                    # Three-tier scoping
                    scope_type=scope_type,
                    connector_type_scope=connector_type_scope,
                    doc_version=doc_version,
                    family_id=family_id,
                    knowledge_type=knowledge_type or KnowledgeType.DOCUMENTATION,
                    priority=priority,
                    expires_at=None,
                    search_metadata=metadata,
                )

                chunk_created = False
                for attempt in range(CHUNK_MAX_RETRIES):
                    try:
                        chunk = await self.knowledge_store.add_chunk(chunk_create)
                        chunk_ids.append(str(chunk.id))
                        chunk_created = True
                        break
                    except Exception as exc:
                        if attempt < CHUNK_MAX_RETRIES - 1:
                            logger.warning(
                                "chunk_creation_retry",
                                chunk_index=i + 1,
                                attempt=attempt + 1,
                                error=str(exc),
                            )
                            await asyncio.sleep(1.0 * (attempt + 1))
                        else:
                            skipped_chunks += 1
                            logger.warning(
                                "chunk_creation_failed_skipping",
                                chunk_index=i + 1,
                                total_chunks=total_chunks,
                                error=str(exc),
                            )
                            if skipped_chunks > max(10, total_chunks // 10):
                                raise ValueError(
                                    f"Too many chunk failures ({skipped_chunks}/{total_chunks}), aborting"
                                ) from exc

                if chunk_created and ((i + 1) % 10 == 0 or (i + 1) == total_chunks):
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

            # Finalize
            await self._update_job_stage(
                job_id, stage=IngestionStage.STORING, progress=1.0, message="Complete"
            )

            if self.job_repository and job_id:
                await self.job_repository.complete_job(job_id, chunk_ids)
                await self.job_repository.session.commit()
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
                await self.job_repository.session.commit()
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

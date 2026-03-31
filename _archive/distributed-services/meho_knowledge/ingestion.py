"""
Knowledge ingestion service.

Orchestrates document processing: extraction, chunking, embedding, and storage.
NOW with job tracking for progress visibility!
"""
# mypy: disable-error-code="no-untyped-def,var-annotated"
from meho_knowledge.extractors import get_extractor
from meho_knowledge.chunking import TextChunker
from meho_knowledge.knowledge_store import KnowledgeStore
from meho_knowledge.object_storage import ObjectStorage
from meho_knowledge.schemas import KnowledgeChunkCreate, KnowledgeType
from meho_knowledge.job_repository import IngestionJobRepository
from meho_knowledge.job_models import IngestionStage
from meho_knowledge.metadata_extraction import MetadataExtractor
from meho_core.structured_logging import get_logger
from typing import List, Optional
from datetime import datetime, timedelta
from uuid import UUID
import uuid
import hashlib
import traceback

logger = get_logger(__name__)


class IngestionService:
    """Orchestrates document ingestion into knowledge base with job tracking"""
    
    def __init__(
        self,
        knowledge_store: KnowledgeStore,
        object_storage: ObjectStorage,
        job_repository: Optional[IngestionJobRepository] = None,
        chunker: Optional[TextChunker] = None
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
        self.chunker = chunker or TextChunker()
        self.metadata_extractor = MetadataExtractor()
    
    async def _update_job_stage(
        self,
        job_id: Optional[str],
        stage: IngestionStage,
        progress: float,
        message: str = "",
        overall_progress: Optional[float] = None,
        **kwargs
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
                stage_started_at=datetime.utcnow(),
                **kwargs
            )
    
    def _calculate_eta(
        self,
        total_chunks: int,
        processed_chunks: int,
        stage_start_time: datetime
    ) -> Optional[datetime]:
        """Calculate estimated completion time for embedding stage"""
        if processed_chunks == 0:
            return None
        
        elapsed = (datetime.utcnow() - stage_start_time).total_seconds()
        chunks_per_second = processed_chunks / elapsed
        remaining_chunks = total_chunks - processed_chunks
        remaining_seconds = remaining_chunks / chunks_per_second if chunks_per_second > 0 else 0
        
        return datetime.utcnow() + timedelta(seconds=remaining_seconds)
    
    async def ingest_document(
        self,
        file_bytes: bytes,
        filename: str,
        mime_type: str,
        tenant_id: Optional[str] = None,
        system_id: Optional[str] = None,
        user_id: Optional[str] = None,
        roles: Optional[List[str]] = None,
        groups: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        knowledge_type: Optional[KnowledgeType] = None,
        priority: int = 0,
        job_id: Optional[str] = None  # If provided, update existing job instead of creating new one
    ) -> List[str]:
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
            system_id: System ID (None for tenant-wide)
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
            await self.job_repository.update_status(job_id, 'processing')
            logger.info(f"Job {job_id}: Starting document processing")
        
        # Stage 1: Uploading to object storage (5% of total time)
        await self._update_job_stage(
            job_id,
            stage=IngestionStage.UPLOADING,
            progress=0.0,
            message="Uploading file to storage..."
        )
        
        doc_id = str(uuid.uuid4())
        storage_key = f"documents/{tenant_id or 'global'}/{doc_id}/{filename}"
        storage_uri = self.object_storage.upload_document(
            file_bytes,
            storage_key,
            mime_type
        )
        
        await self._update_job_stage(
            job_id,
            stage=IngestionStage.UPLOADING,
            progress=1.0,
            message="Upload complete"
        )
        
        chunk_ids = []  # Track created chunks for cleanup on failure
        
        try:
            # Stage 2: Extracting text (10% of total time)
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.EXTRACTING,
                progress=0.0,
                message="Extracting text from document..."
            )
            
            extractor = get_extractor(mime_type)
            pages = extractor.extract(file_bytes)
            
            # Combine pages into single document text
            document_text = "\n\n".join(pages)
            
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.EXTRACTING,
                progress=1.0,
                message=f"Extracted {len(pages)} pages"
            )
            
            # Stage 3: Chunking (5% of total time)
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.CHUNKING,
                progress=0.0,
                message="Chunking document..."
            )
            
            chunks_with_context = self.chunker.chunk_document_with_structure(
                text=document_text,
                document_name=filename,
                detect_headings=True
            )
            
            # Validate we got some chunks
            if not chunks_with_context:
                raise ValueError(f"No text extracted from document {filename}")
            
            total_chunks = len(chunks_with_context)
            
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.CHUNKING,
                progress=1.0,
                message=f"Created {total_chunks} chunks",
                total_chunks=total_chunks
            )
            
            # Stage 4: Metadata Extraction + Embedding (75% of total time - SLOWEST!)
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.EMBEDDING,
                progress=0.0,
                message=f"Processing chunk 0 of {total_chunks}...",
                total_chunks=total_chunks
            )
            
            embedding_start_time = datetime.utcnow()
            
            # 4. Create knowledge chunks with metadata
            # Note: If chunk creation fails midway, we'll clean up partial chunks
            for i, (chunk_text, context) in enumerate(chunks_with_context):
                # Extract rich metadata for this chunk
                metadata = self.metadata_extractor.extract_metadata(
                    text=chunk_text,
                    document_name=filename,
                    chunk_index=i,
                    document_context=context
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
                    metadata_type=type(metadata).__name__
                )
                
                chunk_create = KnowledgeChunkCreate(
                    text=chunk_text,
                    tenant_id=tenant_id,
                    system_id=system_id,
                    user_id=user_id,
                    roles=roles,
                    groups=groups,
                    tags=tags,
                    source_uri=storage_uri,
                    # Lifecycle fields
                    knowledge_type=knowledge_type or KnowledgeType.DOCUMENTATION,
                    priority=priority,
                    expires_at=None,  # Documents never expire (use EVENT for temporary)
                    # Rich metadata for enhanced retrieval
                    search_metadata=metadata
                )
                
                # Verify metadata is in the create object
                logger.info(
                    "chunk_create_object_ready",
                    chunk_index=i,
                    has_search_metadata=chunk_create.search_metadata is not None,
                    search_metadata_type=type(chunk_create.search_metadata).__name__ if chunk_create.search_metadata else None,
                    search_metadata_chapter=chunk_create.search_metadata.chapter if chunk_create.search_metadata else None
                )

                
                try:
                    chunk = await self.knowledge_store.add_chunk(chunk_create)
                    chunk_ids.append(chunk.id)
                    
                    # Update progress every 10 chunks (don't spam database)
                    if (i + 1) % 10 == 0 or (i + 1) == total_chunks:
                        stage_progress = (i + 1) / total_chunks
                        overall_progress = 0.20 + (0.75 * stage_progress)  # 20% done before embedding
                        
                        # Calculate ETA
                        eta = self._calculate_eta(total_chunks, i + 1, embedding_start_time)
                        
                        await self._update_job_stage(
                            job_id,
                            stage=IngestionStage.EMBEDDING,
                            progress=stage_progress,
                            overall_progress=overall_progress,
                            message=f"Processing chunk {i+1} of {total_chunks}...",
                            chunks_processed=i+1,
                            estimated_completion=eta
                        )
                    
                except Exception as chunk_error:
                    # If chunk creation fails, we have partial ingestion
                    # Clean up what we created so far
                    raise ValueError(
                        f"Failed to create chunk {len(chunk_ids)+1} of {len(chunks_with_context)}: {chunk_error}"
                    ) from chunk_error
            
            # Stage 5: Storing/Finalizing (5% of total time)
            await self._update_job_stage(
                job_id,
                stage=IngestionStage.STORING,
                progress=0.5,
                message="Finalizing..."
            )
            
            # NOTE: No manual index building needed!
            # PostgreSQL FTS indexes are automatically maintained by the database.
            # The GIN index on knowledge_chunk.text is updated on every INSERT/UPDATE.
            logger.info(
                "fts_index_auto_maintained",
                tenant_id=tenant_id,
                num_chunks=len(chunk_ids),
                message="PostgreSQL FTS indexes automatically updated"
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
                    "size_bytes": len(file_bytes)
                }
            }
            
            # Determine which stage failed
            current_stage = None
            error_chunk_index = None
            
            # Try to get current stage from locals
            if 'embedding_start_time' in locals():
                current_stage = IngestionStage.EMBEDDING.value
                error_chunk_index = len(chunk_ids)  # Which chunk we were on
            elif 'chunks_with_context' in locals():
                current_stage = IngestionStage.CHUNKING.value
            elif 'document_text' in locals():
                current_stage = IngestionStage.EXTRACTING.value
            elif 'storage_uri' in locals():
                current_stage = IngestionStage.UPLOADING.value
            
            # Mark job as failed with detailed error
            if self.job_repository and job_id:
                await self.job_repository.fail_job(
                    job_id=job_id,
                    error=str(e),
                    error_stage=current_stage,
                    error_chunk_index=error_chunk_index,
                    error_details=error_details
                )
                logger.error(f"Job {job_id}: Failed at stage {current_stage} - {e}")
            
            # Clean up on any failure
            # 1. Delete partially created chunks
            for chunk_id in chunk_ids:
                try:
                    await self.knowledge_store.delete_chunk(chunk_id)
                except Exception:
                    # Best effort cleanup
                    pass
            
            # 2. Delete uploaded document
            try:
                self.object_storage.delete_document(storage_key)
            except Exception:
                # Best effort cleanup
                pass
            
            # Re-raise original error with context
            raise ValueError(f"Failed to ingest document {filename}: {e}") from e
    
    async def ingest_text(
        self,
        text: str,
        tenant_id: Optional[str] = None,
        system_id: Optional[str] = None,
        user_id: Optional[str] = None,
        roles: Optional[List[str]] = None,
        groups: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        source_uri: Optional[str] = None,
        knowledge_type: Optional[KnowledgeType] = None,
        priority: int = 0,
        expires_at: Optional[datetime] = None
    ) -> List[str]:
        """
        Ingest raw text (e.g., from notes, procedures, temporary notices).
        
        Args:
            text: Text to ingest
            tenant_id: Tenant ID
            system_id: System ID
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
        if self.job_repository and hasattr(self, '_current_job_id'):
            job_id = self._current_job_id
            await self.job_repository.update_status(job_id, 'processing')
        
        chunk_ids = []  # Track created chunks for cleanup on failure
        
        try:
            # Chunk the text with structure tracking
            chunks_with_context = self.chunker.chunk_document_with_structure(
                text=text,
                document_name=source_uri or "text-input",
                detect_headings=True
            )
            
            if not chunks_with_context:
                raise ValueError("No chunks created from text (text may be empty)")
            
            # Update job with total chunks
            if self.job_repository and hasattr(self, '_current_job_id'):
                await self.job_repository.update_progress(
                    self._current_job_id,
                    total_chunks=len(chunks_with_context)
                )
            
            # Create knowledge chunks with metadata
            for i, (chunk_text, context) in enumerate(chunks_with_context):
                chunk_source_uri = f"{source_uri}#chunk={i}" if source_uri else None
                
                # Extract metadata
                metadata = self.metadata_extractor.extract_metadata(
                    text=chunk_text,
                    document_name=source_uri or "text-input",
                    chunk_index=i,
                    document_context=context
                )
                
                chunk_create = KnowledgeChunkCreate(
                    text=chunk_text,
                    tenant_id=tenant_id,
                    system_id=system_id,
                    user_id=user_id,
                    roles=roles,
                    groups=groups,
                    tags=tags,
                    source_uri=chunk_source_uri,
                    # Lifecycle fields
                    knowledge_type=knowledge_type or KnowledgeType.DOCUMENTATION,
                    priority=priority,
                    expires_at=expires_at,
                    # Rich metadata for enhanced retrieval
                    search_metadata=metadata
                )
                
                try:
                    chunk = await self.knowledge_store.add_chunk(chunk_create)
                    chunk_ids.append(chunk.id)
                    
                    # Update job progress
                    if self.job_repository and hasattr(self, '_current_job_id'):
                        await self.job_repository.update_progress(
                            self._current_job_id,
                            chunks_processed=i + 1,
                            chunks_created=len(chunk_ids)
                        )
                    
                except Exception as chunk_error:
                    # Chunk creation failed
                    raise ValueError(
                        f"Failed to create text chunk {i+1} of {len(chunks_with_context)}: {chunk_error}"
                    ) from chunk_error
            
            # Mark job complete if tracking
            if self.job_repository and hasattr(self, '_current_job_id'):
                await self.job_repository.complete_job(self._current_job_id, chunk_ids)
            
            return chunk_ids
        
        except Exception as e:
            # Mark job failed if tracking
            if self.job_repository and hasattr(self, '_current_job_id'):
                await self.job_repository.fail_job(self._current_job_id, str(e))
            
            # Clean up partially created chunks on failure
            for chunk_id in chunk_ids:
                try:
                    await self.knowledge_store.delete_chunk(chunk_id)
                except Exception:
                    # Best effort cleanup
                    pass
            
            # Re-raise with context
            raise ValueError(f"Failed to ingest text: {e}") from e

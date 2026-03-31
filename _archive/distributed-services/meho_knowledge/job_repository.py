"""
Repository for ingestion job tracking.

Provides CRUD operations and progress updates for ingestion jobs.
"""
# mypy: disable-error-code="assignment"
from typing import Optional, List, Any, Dict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from meho_knowledge.job_models import IngestionJob
from meho_knowledge.job_schemas import IngestionJobCreate, IngestionJobFilter
from meho_core.errors import NotFoundError
import uuid
from datetime import datetime, timedelta


class IngestionJobRepository:
    """Repository for managing ingestion jobs"""
    
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create_job(self, job_create: IngestionJobCreate) -> IngestionJob:
        """
        Create a new ingestion job.
        
        Args:
            job_create: Job creation data
            
        Returns:
            Created ingestion job
        """
        job = IngestionJob(
            id=uuid.uuid4(),
            status="pending",  # Default status for new jobs
            **job_create.model_dump()
        )
        self.session.add(job)
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
        await self.session.refresh(job)
        
        return job
    
    async def get_job(self, job_id: str) -> Optional[IngestionJob]:
        """
        Get ingestion job by ID.
        
        Args:
            job_id: Job ID (UUID string)
            
        Returns:
            Ingestion job or None
        """
        try:
            job_uuid = uuid.UUID(job_id)
        except ValueError:
            return None
        
        result = await self.session.execute(
            select(IngestionJob).where(IngestionJob.id == job_uuid)
        )
        return result.scalar_one_or_none()
    
    async def update_status(self, job_id: str, status: str) -> None:
        """
        Update job status.
        
        Args:
            job_id: Job ID
            status: New status ('pending', 'processing', 'completed', 'failed')
        """
        job = await self.get_job(job_id)
        if not job:
            raise NotFoundError(f"Job {job_id} not found")
        
        job.status = status
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
    
    async def update_progress(
        self,
        job_id: str,
        total_chunks: Optional[int] = None,
        chunks_processed: Optional[int] = None,
        chunks_created: Optional[int] = None
    ) -> None:
        """
        Update job progress.
        
        Args:
            job_id: Job ID
            total_chunks: Total chunks (if known)
            chunks_processed: Chunks processed so far
            chunks_created: Chunks successfully created
        """
        job = await self.get_job(job_id)
        if not job:
            raise NotFoundError(f"Job {job_id} not found")
        
        if total_chunks is not None:
            job.total_chunks = total_chunks
        if chunks_processed is not None:
            job.chunks_processed = chunks_processed
        if chunks_created is not None:
            job.chunks_created = chunks_created
        
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
    
    async def update_stage(
        self,
        job_id: str,
        current_stage: str,
        stage_progress: float,
        overall_progress: float,
        status_message: Optional[str] = None,
        stage_started_at: Optional[datetime] = None,
        estimated_completion: Optional[datetime] = None,
        **kwargs: Any
    ) -> None:
        """
        Update job stage and progress (Session 30 - Task 29).
        
        Args:
            job_id: Job identifier
            current_stage: Current IngestionStage value
            stage_progress: Progress within stage (0.0-1.0)
            overall_progress: Overall progress (0.0-1.0)
            status_message: Human-readable message
            stage_started_at: When this stage started
            estimated_completion: ETA for completion
            **kwargs: Additional fields (chunks_processed, etc.)
        """
        job = await self.get_job(job_id)
        if not job:
            raise NotFoundError(f"Job {job_id} not found")
        
        # Update stage tracking
        job.current_stage = current_stage
        job.stage_progress = stage_progress
        job.overall_progress = overall_progress
        
        if status_message:
            job.status_message = status_message
        if stage_started_at:
            job.stage_started_at = stage_started_at
        if estimated_completion:
            job.estimated_completion = estimated_completion
        
        # Update any additional fields passed in kwargs
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)
        
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
    
    async def complete_job(self, job_id: str, chunk_ids: List[str]) -> None:
        """
        Mark job as completed.
        
        Args:
            job_id: Job ID
            chunk_ids: List of created chunk IDs
        """
        job = await self.get_job(job_id)
        if not job:
            raise NotFoundError(f"Job {job_id} not found")
        
        # Keep successes for 24 hours
        retention = datetime.utcnow() + timedelta(hours=24)
        
        job.status = 'completed'
        job.chunk_ids = chunk_ids
        job.chunks_created = len(chunk_ids)
        job.completed_at = datetime.utcnow()
        job.overall_progress = 1.0
        job.status_message = f"Completed successfully - {len(chunk_ids)} chunks created"
        job.retention_until = retention
        
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
    
    async def fail_job(
        self,
        job_id: str,
        error: str,
        error_stage: Optional[str] = None,
        error_chunk_index: Optional[int] = None,
        error_details: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Mark job as failed with detailed error information (Session 30 - Task 29).
        
        Args:
            job_id: Job identifier
            error: Error message
            error_stage: Which stage failed
            error_chunk_index: Which chunk failed (if applicable)
            error_details: Structured error details (stack trace, etc.)
        """
        job = await self.get_job(job_id)
        if not job:
            raise NotFoundError(f"Job {job_id} not found")
        
        # Keep failures for 7 days
        retention = datetime.utcnow() + timedelta(days=7)
        
        job.status = 'failed'
        job.error = error
        job.error_stage = error_stage
        job.error_chunk_index = error_chunk_index
        job.error_details = error_details
        job.completed_at = datetime.utcnow()
        job.retention_until = retention
        
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
    
    async def list_jobs(self, filter: IngestionJobFilter) -> List[IngestionJob]:
        """
        List ingestion jobs with filtering.
        
        Args:
            filter: Filter criteria
            
        Returns:
            List of ingestion jobs
        """
        query = select(IngestionJob)
        
        # Apply filters
        if filter.tenant_id:
            query = query.where(IngestionJob.tenant_id == filter.tenant_id)
        if filter.status:
            query = query.where(IngestionJob.status == filter.status)
        if filter.job_type:
            query = query.where(IngestionJob.job_type == filter.job_type)
        
        # Order by most recent first
        query = query.order_by(IngestionJob.started_at.desc())
        
        # Pagination
        query = query.limit(filter.limit).offset(filter.offset)
        
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def count_jobs(self, filter: IngestionJobFilter) -> int:
        """
        Count total ingestion jobs matching filter criteria.
        
        Args:
            filter: Filter criteria (limit/offset are ignored)
            
        Returns:
            Total count of jobs matching filters
        """
        query = select(func.count()).select_from(IngestionJob)
        
        # Apply same filters as list_jobs (but no pagination)
        if filter.tenant_id:
            query = query.where(IngestionJob.tenant_id == filter.tenant_id)
        if filter.status:
            query = query.where(IngestionJob.status == filter.status)
        if filter.job_type:
            query = query.where(IngestionJob.job_type == filter.job_type)
        
        result = await self.session.execute(query)
        return result.scalar() or 0
    
    async def get_active_jobs(self, tenant_id: Optional[str] = None) -> List[IngestionJob]:
        """
        Get all currently active (processing) jobs (Session 30 - Task 29).
        
        Useful for the global job monitor in the frontend.
        
        Args:
            tenant_id: Optional tenant filter
            
        Returns:
            List of jobs with status 'processing'
        """
        query = select(IngestionJob).where(IngestionJob.status == 'processing')
        
        if tenant_id:
            query = query.where(IngestionJob.tenant_id == tenant_id)
        
        # Order by most recent first
        query = query.order_by(IngestionJob.started_at.desc())
        
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def delete_job(self, job_id: str) -> bool:
        """
        Permanently delete a job record.
        
        Args:
            job_id: Job identifier
        
        Returns:
            True if deleted, False if not found
        """
        job = await self.get_job(job_id)
        if not job:
            return False
        
        await self.session.delete(job)
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
        return True


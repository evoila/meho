# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Transcript retention service for data lifecycle management.

Handles:
- Soft-delete of transcripts older than retention period
- Hard-delete of soft-deleted transcripts after grace period
- Retention statistics for monitoring

Part of TASK-186: Deep Observability & Introspection System.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.config import get_config
from meho_app.core.otel import get_logger
from meho_app.modules.agents.models import ChatSessionModel
from meho_app.modules.agents.persistence.transcript_models import (
    SessionTranscriptModel,
)

logger = get_logger(__name__)


@dataclass
class RetentionStats:
    """Statistics about transcript retention state."""

    total_transcripts: int
    active_transcripts: int
    soft_deleted_transcripts: int
    pending_hard_delete: int  # Soft-deleted and past grace period
    oldest_active_timestamp: datetime | None
    oldest_soft_deleted_timestamp: datetime | None


@dataclass
class CleanupResult:
    """Result of a retention cleanup operation."""

    soft_deleted_count: int
    hard_deleted_count: int
    errors: list[str]


class TranscriptRetentionService:
    """Service for managing transcript lifecycle and retention.

    Implements a two-phase deletion process:
    1. Soft-delete: Mark transcripts as deleted (can be recovered)
    2. Hard-delete: Permanently remove after grace period

    Usage:
        service = TranscriptRetentionService(db_session)

        # Run full cleanup
        result = await service.run_cleanup()

        # Get statistics
        stats = await service.get_retention_stats()
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initialize the retention service.

        Args:
            session: SQLAlchemy async session.
        """
        self.session = session
        config = get_config()
        self.retention_days = config.transcript_retention_days
        self.grace_days = config.transcript_grace_days

    async def soft_delete_old_transcripts(
        self,
        retention_days: int | None = None,
        batch_size: int = 100,
        tenant_id: str | None = None,
    ) -> int:
        """Mark old transcripts as deleted.

        Transcripts older than retention_days that are completed
        will have their deleted_at timestamp set.

        Args:
            retention_days: Override config retention period.
            batch_size: Maximum transcripts to process per call.
            tenant_id: Scope soft-delete to this tenant only (security).

        Returns:
            Number of transcripts soft-deleted.
        """
        days = retention_days if retention_days is not None else self.retention_days
        cutoff = datetime.now(tz=UTC) - timedelta(days=days)

        # Limit batch size using subquery
        subquery = (
            select(SessionTranscriptModel.id)
            .where(SessionTranscriptModel.status == "completed")
            .where(SessionTranscriptModel.completed_at < cutoff)
            .where(SessionTranscriptModel.deleted_at.is_(None))
        )
        if tenant_id:
            subquery = subquery.join(
                ChatSessionModel,
                ChatSessionModel.id == SessionTranscriptModel.session_id,
            ).where(ChatSessionModel.tenant_id == tenant_id)
        subquery = subquery.limit(batch_size)

        stmt = (
            update(SessionTranscriptModel)
            .where(SessionTranscriptModel.id.in_(subquery))
            .values(deleted_at=datetime.now(tz=UTC))
            .returning(SessionTranscriptModel.id)
        )

        result = await self.session.execute(stmt)
        deleted_ids = result.scalars().all()
        await self.session.flush()

        if deleted_ids:
            logger.info(f"Soft-deleted {len(deleted_ids)} transcripts older than {days} days")

        return len(deleted_ids)

    async def hard_delete_soft_deleted(
        self,
        grace_days: int | None = None,
        batch_size: int = 100,
        tenant_id: str | None = None,
    ) -> int:
        """Permanently delete soft-deleted transcripts after grace period.

        Transcripts that were soft-deleted more than grace_days ago
        will be permanently removed along with their events.

        Args:
            grace_days: Override config grace period.
            batch_size: Maximum transcripts to process per call.
            tenant_id: Scope hard-delete to this tenant only (security).

        Returns:
            Number of transcripts hard-deleted.
        """
        days = grace_days if grace_days is not None else self.grace_days
        cutoff = datetime.now(tz=UTC) - timedelta(days=days)

        # Find transcripts to hard-delete:
        # - soft-deleted
        # - deleted_at older than grace period
        subquery = (
            select(SessionTranscriptModel.id)
            .where(SessionTranscriptModel.deleted_at.is_not(None))
            .where(SessionTranscriptModel.deleted_at < cutoff)
        )
        if tenant_id:
            subquery = subquery.join(
                ChatSessionModel,
                ChatSessionModel.id == SessionTranscriptModel.session_id,
            ).where(ChatSessionModel.tenant_id == tenant_id)
        subquery = subquery.limit(batch_size)

        # Get IDs first for logging
        result = await self.session.execute(subquery)
        ids_to_delete = list(result.scalars().all())

        if not ids_to_delete:
            return 0

        # Events will be cascade-deleted due to FK relationship
        stmt = delete(SessionTranscriptModel).where(SessionTranscriptModel.id.in_(ids_to_delete))

        await self.session.execute(stmt)
        await self.session.flush()

        logger.info(
            f"Hard-deleted {len(ids_to_delete)} transcripts "
            f"(soft-deleted more than {days} days ago)"
        )

        return len(ids_to_delete)

    async def run_cleanup(
        self,
        retention_days: int | None = None,
        grace_days: int | None = None,
        batch_size: int = 100,
        tenant_id: str | None = None,
    ) -> CleanupResult:
        """Run full cleanup cycle: soft-delete then hard-delete.

        Args:
            retention_days: Override retention period for soft-delete.
            grace_days: Override grace period for hard-delete.
            batch_size: Maximum transcripts per operation.
            tenant_id: Scope cleanup to this tenant only (security).

        Returns:
            CleanupResult with counts and any errors.
        """
        errors: list[str] = []
        soft_deleted = 0
        hard_deleted = 0

        try:
            soft_deleted = await self.soft_delete_old_transcripts(
                retention_days=retention_days,
                batch_size=batch_size,
                tenant_id=tenant_id,
            )
        except Exception as e:
            error = f"Soft-delete failed: {e}"
            logger.error(error, exc_info=True)
            errors.append(error)

        try:
            hard_deleted = await self.hard_delete_soft_deleted(
                grace_days=grace_days,
                batch_size=batch_size,
                tenant_id=tenant_id,
            )
        except Exception as e:
            error = f"Hard-delete failed: {e}"
            logger.error(error, exc_info=True)
            errors.append(error)

        return CleanupResult(
            soft_deleted_count=soft_deleted,
            hard_deleted_count=hard_deleted,
            errors=errors,
        )

    def _tenant_join(self, stmt, tenant_id: str | None):
        """Add tenant scoping via join to chat_session if tenant_id provided."""
        if tenant_id:
            stmt = stmt.join(
                ChatSessionModel,
                ChatSessionModel.id == SessionTranscriptModel.session_id,
            ).where(ChatSessionModel.tenant_id == tenant_id)
        return stmt

    async def get_retention_stats(self, tenant_id: str | None = None) -> RetentionStats:
        """Get statistics about transcript retention state.

        Args:
            tenant_id: Scope stats to this tenant only (security).

        Returns:
            RetentionStats with counts and timestamps.
        """
        # Total transcripts
        total_stmt = select(func.count(SessionTranscriptModel.id))
        total_stmt = self._tenant_join(total_stmt, tenant_id)
        total_result = await self.session.execute(total_stmt)
        total = total_result.scalar() or 0

        # Active (not soft-deleted)
        active_stmt = select(func.count(SessionTranscriptModel.id)).where(
            SessionTranscriptModel.deleted_at.is_(None)
        )
        active_stmt = self._tenant_join(active_stmt, tenant_id)
        active_result = await self.session.execute(active_stmt)
        active = active_result.scalar() or 0

        # Soft-deleted
        soft_deleted = total - active

        # Pending hard-delete (soft-deleted and past grace period)
        grace_cutoff = datetime.now(tz=UTC) - timedelta(days=self.grace_days)
        pending_stmt = select(func.count(SessionTranscriptModel.id)).where(
            SessionTranscriptModel.deleted_at.is_not(None),
            SessionTranscriptModel.deleted_at < grace_cutoff,
        )
        pending_stmt = self._tenant_join(pending_stmt, tenant_id)
        pending_result = await self.session.execute(pending_stmt)
        pending = pending_result.scalar() or 0

        # Oldest active transcript
        oldest_active_stmt = select(func.min(SessionTranscriptModel.created_at)).where(
            SessionTranscriptModel.deleted_at.is_(None)
        )
        oldest_active_stmt = self._tenant_join(oldest_active_stmt, tenant_id)
        oldest_active_result = await self.session.execute(oldest_active_stmt)
        oldest_active = oldest_active_result.scalar()

        # Oldest soft-deleted transcript
        oldest_deleted_stmt = select(func.min(SessionTranscriptModel.deleted_at)).where(
            SessionTranscriptModel.deleted_at.is_not(None)
        )
        oldest_deleted_stmt = self._tenant_join(oldest_deleted_stmt, tenant_id)
        oldest_deleted_result = await self.session.execute(oldest_deleted_stmt)
        oldest_deleted = oldest_deleted_result.scalar()

        return RetentionStats(
            total_transcripts=total,
            active_transcripts=active,
            soft_deleted_transcripts=soft_deleted,
            pending_hard_delete=pending,
            oldest_active_timestamp=oldest_active,
            oldest_soft_deleted_timestamp=oldest_deleted,
        )

    async def restore_transcript(self, transcript_id: UUID) -> bool:
        """Restore a soft-deleted transcript.

        Args:
            transcript_id: ID of transcript to restore.

        Returns:
            True if restored, False if not found or not soft-deleted.
        """
        stmt = (
            update(SessionTranscriptModel)
            .where(SessionTranscriptModel.id == transcript_id)
            .where(SessionTranscriptModel.deleted_at.is_not(None))
            .values(deleted_at=None)
            .returning(SessionTranscriptModel.id)
        )

        result = await self.session.execute(stmt)
        restored = result.scalar_one_or_none()

        if restored:
            await self.session.flush()
            logger.info(f"Restored transcript {transcript_id}")
            return True

        return False

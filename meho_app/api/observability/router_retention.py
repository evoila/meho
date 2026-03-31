# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Retention management endpoints for the Observability API.

Provides endpoints to:
- Get retention statistics
- Trigger manual cleanup

Part of TASK-186: Deep Observability & Introspection System.
"""

from fastapi import APIRouter, HTTPException, Query, Request

from meho_app.api.dependencies import CurrentUser, DbSession
from meho_app.api.observability.schemas import (
    CleanupResultResponse,
    RetentionStatsResponse,
)
from meho_app.core.config import get_config
from meho_app.core.otel import get_logger
from meho_app.core.rate_limiting import get_limiter
from meho_app.modules.agents.persistence.retention_service import (
    TranscriptRetentionService,
)

logger = get_logger(__name__)

router = APIRouter()

# Get rate limiter
limiter = get_limiter()


@router.get("/retention/stats", response_model=RetentionStatsResponse)
@limiter.limit(lambda: get_config().rate_limit_transcript)
async def get_retention_stats(
    request: Request,
    user: CurrentUser,
    db_session: DbSession,
):
    """
    Get retention statistics for transcript cleanup.

    Shows counts of active, soft-deleted, and pending hard-delete transcripts.
    Useful for monitoring data growth and cleanup effectiveness.
    """
    try:
        service = TranscriptRetentionService(db_session)
        stats = await service.get_retention_stats(tenant_id=user.tenant_id)
        config = get_config()

        return RetentionStatsResponse(
            total_transcripts=stats.total_transcripts,
            active_transcripts=stats.active_transcripts,
            soft_deleted_transcripts=stats.soft_deleted_transcripts,
            pending_hard_delete=stats.pending_hard_delete,
            oldest_active_timestamp=stats.oldest_active_timestamp,
            oldest_soft_deleted_timestamp=stats.oldest_soft_deleted_timestamp,
            retention_days=config.transcript_retention_days,
            grace_days=config.transcript_grace_days,
        )

    except Exception as e:
        logger.error(f"Error getting retention stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/retention/cleanup", response_model=CleanupResultResponse)
@limiter.limit(lambda: get_config().rate_limit_cleanup)
async def trigger_retention_cleanup(
    request: Request,
    user: CurrentUser,
    db_session: DbSession,
    retention_days: int | None = Query(
        default=None, description="Override retention period (days)"
    ),
    grace_days: int | None = Query(default=None, description="Override grace period (days)"),
    batch_size: int = Query(default=100, ge=1, le=1000),
):
    """
    Trigger manual retention cleanup.

    Performs both soft-delete (mark old transcripts) and hard-delete
    (remove soft-deleted past grace period).

    This endpoint is rate-limited to 1/hour to prevent abuse.
    Normally cleanup runs automatically on a schedule.
    """
    try:
        service = TranscriptRetentionService(db_session)
        result = await service.run_cleanup(
            retention_days=retention_days,
            grace_days=grace_days,
            batch_size=batch_size,
            tenant_id=user.tenant_id,
        )
        await db_session.commit()

        if result.errors:
            message = f"Cleanup completed with errors: {', '.join(result.errors)}"
        else:
            message = (
                f"Cleanup completed: {result.soft_deleted_count} soft-deleted, "
                f"{result.hard_deleted_count} hard-deleted"
            )

        return CleanupResultResponse(
            soft_deleted_count=result.soft_deleted_count,
            hard_deleted_count=result.hard_deleted_count,
            errors=result.errors,
            message=message,
        )

    except Exception as e:
        logger.error(f"Error running cleanup: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e

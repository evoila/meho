# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Knowledge module startup tasks (Phase 90.2).

Runs during application lifespan startup to clean up state
left behind by crashes or unexpected shutdowns.
"""

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


async def cleanup_stuck_ingestion_jobs() -> int:
    """Mark ingestion jobs stuck in 'processing' as failed.

    When the server crashes or restarts during document processing,
    jobs are left in 'processing' status forever. This function
    marks them as failed with a clear error message so users know
    to re-upload.

    Returns:
        Number of jobs cleaned up.
    """
    from sqlalchemy import update

    from meho_app.database import get_session_maker
    from meho_app.modules.knowledge.job_models import IngestionJob

    session_maker = get_session_maker()
    async with session_maker() as session:
        result = await session.execute(
            update(IngestionJob)
            .where(IngestionJob.status == "processing")
            .values(
                status="failed",
                error="Server restarted during processing -- please re-upload the document",
            )
        )
        await session.commit()
        cleaned = result.rowcount
        if cleaned:
            logger.info(
                "stuck_ingestion_jobs_cleaned",
                count=cleaned,
            )
        return cleaned

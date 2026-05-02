# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Orphaned transcript cleanup utilities.

This module provides functions to clean up orphaned transcripts that were
never properly closed (e.g., due to client disconnect, server restart,
or unhandled exceptions).

TASK-188: Bulletproof Transcript Persistence
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.agents.persistence.transcript_models import SessionTranscriptModel

logger = get_logger(__name__)


async def cleanup_orphaned_transcripts(
    session: AsyncSession,
    max_age_minutes: int = 30,
) -> int:
    """Mark orphaned transcripts (stuck in 'running' status) as failed.

    Transcripts can become orphaned when:
    - Browser is refreshed mid-stream (GeneratorExit not handled)
    - Network connection drops
    - Server restarts during execution
    - Unhandled exceptions bypass cleanup

    This function finds all transcripts older than max_age_minutes that are
    still in "running" status and marks them as "failed".

    Args:
        session: AsyncSession for database operations.
        max_age_minutes: Maximum age in minutes for a "running" transcript
            before it's considered orphaned. Default: 30 minutes.

    Returns:
        Number of transcripts that were cleaned up.

    Example:
        >>> async with session_maker() as session:
        ...     count = await cleanup_orphaned_transcripts(session)
        ...     if count > 0:
        ...         logger.info(f"Cleaned up {count} orphaned transcripts")
        ...     await session.commit()
    """
    cutoff = datetime.now(tz=UTC) - timedelta(minutes=max_age_minutes)

    stmt = (
        update(SessionTranscriptModel)
        .where(
            SessionTranscriptModel.status == "running",
            SessionTranscriptModel.created_at < cutoff,
            SessionTranscriptModel.completed_at.is_(None),
        )
        .values(
            status="failed",
            completed_at=datetime.now(tz=UTC),
        )
    )

    result = await session.execute(stmt)
    count: int = result.rowcount or 0  # type: ignore[attr-defined]  # SQLAlchemy Result.rowcount exists at runtime

    if count > 0:
        logger.info(
            f"Cleaned up {count} orphaned transcripts (older than {max_age_minutes} minutes)"
        )

    return count

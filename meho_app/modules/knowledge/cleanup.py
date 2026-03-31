# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Knowledge cleanup jobs.

Removes expired event chunks to prevent knowledge base bloat.

After pgvector migration (Session 15):
- All data stored in PostgreSQL (text, metadata, AND vectors)
- Single deletion removes everything (no separate vector store sync)
- Cleanup is simple: DELETE FROM knowledge_chunk WHERE expires_at < NOW()
"""

# mypy: disable-error-code="attr-defined,var-annotated,arg-type"
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, select
from sqlalchemy import func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.knowledge.models import KnowledgeChunkModel

logger = get_logger(__name__)


async def cleanup_expired_events(session: AsyncSession) -> dict:
    """
    Delete expired event chunks from PostgreSQL (includes vector embeddings).

    Removes chunks where:
    - expires_at IS NOT NULL
    - expires_at < current time

    Note: Since pgvector stores embeddings in PostgreSQL, a single DELETE
    removes both the chunk data AND its vector embedding.

    Args:
        session: Database session

    Returns:
        Dict with cleanup statistics
    """
    now = datetime.now(tz=UTC)

    logger.info(f"Starting cleanup of expired events (cutoff: {now.isoformat()})")

    # 1. Find expired chunks (for logging)
    result = await session.execute(
        select(KnowledgeChunkModel).where(
            and_(KnowledgeChunkModel.expires_at.is_not(None), KnowledgeChunkModel.expires_at < now)
        )
    )
    expired_chunks = list(result.scalars().all())

    if not expired_chunks:
        logger.info("No expired chunks found")
        return {"deleted_count": 0, "timestamp": now.isoformat()}

    chunk_ids = [str(chunk.id) for chunk in expired_chunks]
    logger.info(f"Found {len(expired_chunks)} expired chunks to delete")

    # 2. Delete from PostgreSQL (automatically deletes embeddings via pgvector)
    delete_result = await session.execute(
        delete(KnowledgeChunkModel).where(
            and_(KnowledgeChunkModel.expires_at.is_not(None), KnowledgeChunkModel.expires_at < now)
        )
    )
    await session.commit()

    deleted_count = delete_result.rowcount

    logger.info(f"Cleanup complete: deleted {deleted_count} chunks (including embeddings)")

    return {
        "deleted_count": deleted_count,
        "timestamp": now.isoformat(),
        "chunk_ids": chunk_ids[:10],  # Sample of deleted IDs
    }


async def get_cleanup_statistics(session: AsyncSession) -> dict:
    """
    Get statistics about knowledge chunks and expiration.

    Useful for monitoring and capacity planning.

    Args:
        session: Database session

    Returns:
        Dict with statistics
    """
    now = datetime.now(tz=UTC)

    # Total chunks
    total_result = await session.execute(select(sa_func.count()).select_from(KnowledgeChunkModel))
    total_chunks = total_result.scalar()

    # Chunks by type
    type_result = await session.execute(
        select(KnowledgeChunkModel.knowledge_type, sa_func.count()).group_by(
            KnowledgeChunkModel.knowledge_type
        )
    )
    chunks_by_type = dict(type_result.all())

    # Expired but not deleted (shouldn't happen if cleanup runs)
    expired_result = await session.execute(
        select(sa_func.count())
        .select_from(KnowledgeChunkModel)
        .where(
            and_(KnowledgeChunkModel.expires_at.is_not(None), KnowledgeChunkModel.expires_at < now)
        )
    )
    expired_count = expired_result.scalar()

    # Will expire in next 24 hours
    tomorrow = now + timedelta(hours=24)
    expiring_soon_result = await session.execute(
        select(sa_func.count())
        .select_from(KnowledgeChunkModel)
        .where(
            and_(
                KnowledgeChunkModel.expires_at.is_not(None),
                KnowledgeChunkModel.expires_at >= now,
                KnowledgeChunkModel.expires_at < tomorrow,
            )
        )
    )
    expiring_soon_count = expiring_soon_result.scalar()

    return {
        "total_chunks": total_chunks,
        "chunks_by_type": chunks_by_type,
        "expired_not_deleted": expired_count,  # Should be 0 if cleanup runs
        "expiring_in_24h": expiring_soon_count,
        "timestamp": now.isoformat(),
    }

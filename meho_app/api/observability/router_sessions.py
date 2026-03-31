# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Session listing endpoint for the Observability API.

Part of TASK-186: Deep Observability & Introspection System.
"""

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import desc, func, select

from meho_app.api.dependencies import CurrentUser, DbSession
from meho_app.api.observability.schemas import (
    SessionListItem,
    SessionListResponse,
)
from meho_app.core.config import get_config
from meho_app.core.otel import get_logger
from meho_app.core.rate_limiting import get_limiter
from meho_app.modules.agents.models import ChatSessionModel
from meho_app.modules.agents.persistence.transcript_models import SessionTranscriptModel

logger = get_logger(__name__)

router = APIRouter()

# Get rate limiter
limiter = get_limiter()


@router.get("/sessions", response_model=SessionListResponse)
@limiter.limit(lambda: get_config().rate_limit_transcript)
async def list_sessions(
    request: Request,
    user: CurrentUser,
    db_session: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, description="Filter by status"),
):
    """
    List sessions with transcript summaries.

    Returns paginated list of sessions that have transcripts,
    ordered by creation date (newest first).
    """
    try:
        # Query transcripts joined with sessions for tenant filtering
        stmt = (
            select(SessionTranscriptModel)
            .join(
                ChatSessionModel,
                SessionTranscriptModel.session_id == ChatSessionModel.id,
            )
            .where(ChatSessionModel.tenant_id == user.tenant_id)
        )

        if status:
            stmt = stmt.where(SessionTranscriptModel.status == status)

        # Get total count
        count_stmt = select(func.count()).select_from(stmt.subquery())
        count_result = await db_session.execute(count_stmt)
        total = count_result.scalar() or 0

        # Get paginated results
        stmt = stmt.order_by(desc(SessionTranscriptModel.created_at))
        stmt = stmt.offset(offset).limit(limit)
        result = await db_session.execute(stmt)
        transcripts = result.scalars().all()

        sessions = [
            SessionListItem(
                session_id=str(t.session_id),
                created_at=t.created_at,
                status=t.status,
                user_query=t.user_query,
                total_llm_calls=t.total_llm_calls,
                total_tokens=t.total_tokens,
                total_duration_ms=t.total_duration_ms,
            )
            for t in transcripts
        ]

        return SessionListResponse(
            sessions=sessions,
            total=total,
            offset=offset,
            limit=limit,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e

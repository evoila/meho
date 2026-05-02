# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Event-related endpoints for the Observability API.

Provides endpoints to:
- Get specific event details
- Filter events by type (LLM calls, HTTP calls, SQL queries)
- Search across sessions

Part of TASK-186: Deep Observability & Introspection System.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import String, cast, desc, or_, select

from meho_app.api.dependencies import CurrentUser, DbSession
from meho_app.api.observability.schemas import (
    EventResponse,
    SearchResponse,
    SearchResultItem,
)
from meho_app.api.observability.utils import (
    event_model_to_response,
    resolve_session_id,
    verify_session_access,
)
from meho_app.core.config import get_config
from meho_app.core.otel import get_logger
from meho_app.core.rate_limiting import get_limiter
from meho_app.modules.agents.models import ChatSessionModel
from meho_app.modules.agents.persistence.transcript_models import (
    SessionTranscriptModel,
    TranscriptEventModel,
)
from meho_app.modules.agents.persistence.transcript_service import TranscriptService

logger = get_logger(__name__)

DESC_NUM_EVENTS_SKIP = "Number of events to skip"

router = APIRouter()

# Get rate limiter
limiter = get_limiter()


@router.get("/sessions/{session_id}/events/{event_id}", response_model=EventResponse)
@limiter.limit(lambda: get_config().rate_limit_transcript)
async def get_event_details(
    request: Request,
    session_id: str,
    event_id: str,
    user: CurrentUser,
    db_session: DbSession,
) -> Any:
    """
    Get full details for a specific event.

    Use this to drill into a specific event from the timeline.
    """
    try:
        session_uuid = await resolve_session_id(session_id, user, db_session)
        await verify_session_access(session_uuid, user, db_session)

        try:
            event_uuid = UUID(event_id)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Invalid event ID format: {event_id}"
            ) from None

        service = TranscriptService(db_session)
        event = await service.get_event_by_id(event_uuid)

        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")

        # Verify event belongs to this session
        if event.session_id != session_uuid:
            raise HTTPException(status_code=404, detail="Event not found in session")

        return event_model_to_response(event)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting event: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/sessions/{session_id}/llm-calls", response_model=list[EventResponse])
@limiter.limit(lambda: get_config().rate_limit_transcript)
async def get_llm_calls(
    request: Request,
    session_id: str,
    user: CurrentUser,
    db_session: DbSession,
    include_system_prompt: bool = Query(default=True, description="Include system prompts"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, description=DESC_NUM_EVENTS_SKIP),
) -> Any:
    """
    Get all LLM calls from a session with full prompts and responses.

    Use this to understand MEHO's reasoning and decision-making.
    """
    try:
        session_uuid = await resolve_session_id(session_id, user, db_session)
        await verify_session_access(session_uuid, user, db_session)

        service = TranscriptService(db_session)
        events = await service.get_llm_calls(session_uuid, limit=limit, offset=offset)

        responses = [event_model_to_response(e) for e in events]

        # Optionally strip system prompts
        if not include_system_prompt:
            for r in responses:
                r.details.llm_prompt = None

        return responses

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting LLM calls: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/sessions/{session_id}/operation-calls", response_model=list[EventResponse])
@limiter.limit(lambda: get_config().rate_limit_transcript)
async def get_operation_calls(
    request: Request,
    session_id: str,
    user: CurrentUser,
    db_session: DbSession,
    include_bodies: bool = Query(default=True, description="Include request/response bodies"),
    status_filter: str | None = Query(
        default=None, description="Filter by status: 'success', 'error', or 'all'"
    ),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, description=DESC_NUM_EVENTS_SKIP),
) -> Any:
    """
    Get all operation calls (REST/SOAP/VMware) made during a session.

    Use this to debug connector API interactions.
    """
    try:
        session_uuid = await resolve_session_id(session_id, user, db_session)
        await verify_session_access(session_uuid, user, db_session)

        service = TranscriptService(db_session)
        events = await service.get_operation_calls(session_uuid, limit=limit, offset=offset)

        # Filter by status if requested
        if status_filter:
            if status_filter.lower() == "success":
                events = [e for e in events if e.details.get("http_status_code", 0) < 400]
            elif status_filter.lower() == "error":
                events = [e for e in events if e.details.get("http_status_code", 0) >= 400]

        responses = [event_model_to_response(e) for e in events]

        # Optionally strip bodies
        if not include_bodies:
            for r in responses:
                r.details.http_request_body = None
                r.details.http_response_body = None

        return responses

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting HTTP calls: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/sessions/{session_id}/sql-queries", response_model=list[EventResponse])
@limiter.limit(lambda: get_config().rate_limit_transcript)
async def get_sql_queries(
    request: Request,
    session_id: str,
    user: CurrentUser,
    db_session: DbSession,
    include_results: bool = Query(default=True, description="Include result samples"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, description=DESC_NUM_EVENTS_SKIP),
) -> Any:
    """
    Get all SQL queries executed during a session.

    Use this to debug database interactions.
    """
    try:
        session_uuid = await resolve_session_id(session_id, user, db_session)
        await verify_session_access(session_uuid, user, db_session)

        service = TranscriptService(db_session)
        events = await service.get_sql_queries(session_uuid, limit=limit, offset=offset)

        responses = [event_model_to_response(e) for e in events]

        # Optionally strip results
        if not include_results:
            for r in responses:
                r.details.sql_result_sample = None

        return responses

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting SQL queries: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/search", response_model=SearchResponse)
@limiter.limit(lambda: get_config().rate_limit_search)
async def search_events(
    request: Request,
    query: str,
    user: CurrentUser,
    db_session: DbSession,
    event_type: str | None = Query(default=None, description="Filter by event type"),
    since_minutes: int = Query(default=60, ge=1, le=10080),
    limit: int = Query(default=20, ge=1, le=100),
) -> Any:
    """
    Search for events matching criteria across recent sessions.

    Useful for finding patterns or specific occurrences across sessions.
    Searches event summaries and details.
    """
    try:
        # Calculate time boundary
        since = datetime.now(tz=UTC) - timedelta(minutes=since_minutes)

        # Build query with tenant filter
        stmt = (
            select(TranscriptEventModel)
            .join(
                SessionTranscriptModel,
                TranscriptEventModel.transcript_id == SessionTranscriptModel.id,
            )
            .join(
                ChatSessionModel,
                SessionTranscriptModel.session_id == ChatSessionModel.id,
            )
            .where(ChatSessionModel.tenant_id == user.tenant_id)
            .where(TranscriptEventModel.timestamp >= since)
        )

        if event_type:
            stmt = stmt.where(TranscriptEventModel.type == event_type)

        # Search in summary and JSONB details
        search_pattern = f"%{query}%"
        stmt = stmt.where(
            or_(
                TranscriptEventModel.summary.ilike(search_pattern),
                cast(TranscriptEventModel.details, String).ilike(search_pattern),
            )
        )

        stmt = stmt.order_by(desc(TranscriptEventModel.timestamp))
        stmt = stmt.limit(limit)

        result = await db_session.execute(stmt)
        events = result.scalars().all()

        results = [
            SearchResultItem(
                event_id=str(e.id),
                session_id=str(e.session_id),
                event_type=e.type,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                summary=e.summary,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                timestamp=e.timestamp,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                match_context=e.summary[:200] if len(e.summary) > 200 else e.summary,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
            )
            for e in events
        ]

        return SearchResponse(
            query=query,
            results=results,
            total=len(results),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error searching events: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e

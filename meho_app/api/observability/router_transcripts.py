# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Transcript retrieval endpoints for the Observability API.

Part of TASK-186: Deep Observability & Introspection System.
"""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request

from meho_app.api.dependencies import CurrentUser, DbSession
from meho_app.api.observability.schemas import (
    EventDetailsResponse,
    MultiTranscriptResponse,
    SessionSummaryResponse,
    TranscriptItemResponse,
)
from meho_app.api.observability.utils import (
    event_model_to_response,
    resolve_session_id,
    transcript_to_summary,
    verify_session_access,
)
from meho_app.core.config import get_config
from meho_app.core.otel import get_logger
from meho_app.core.rate_limiting import get_limiter
from meho_app.modules.agents.persistence.transcript_service import TranscriptService

logger = get_logger(__name__)

router = APIRouter()

# Get rate limiter
limiter = get_limiter()

# Summary cache TTL (5 minutes)
SUMMARY_CACHE_TTL_SECONDS = 300


@router.get(
    "/sessions/{session_id}/transcript",
    response_model=MultiTranscriptResponse,
    responses={
        404: {"description": "No transcripts found for this session"},
        500: {"description": "Internal server error"},
    },
)
@limiter.limit(lambda: get_config().rate_limit_transcript)
async def get_transcript(
    request: Request,
    session_id: str,
    user: CurrentUser,
    db_session: DbSession,
    event_types: Annotated[list[str] | None, Query(description="Filter by event types")] = None,
    include_details: Annotated[bool, Query(description="Include event details")] = True,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
) -> Any:
    """
    Get all execution transcripts for a session.

    For multi-turn conversations, returns all transcripts (one per user message).
    Includes all events with their full details (LLM prompts, operation calls, etc.).
    Use session_id='latest' to get the most recent session.

    This endpoint enables:
    - Developers to debug issues
    - Users to understand what happened
    - LLMs to analyze behavior and suggest improvements
    """
    try:
        # Resolve session ID (handles 'latest')
        session_uuid = await resolve_session_id(session_id, user, db_session)

        # Verify access
        await verify_session_access(session_uuid, user, db_session)

        # Get all transcripts for this session
        service = TranscriptService(db_session)
        transcripts = await service.get_transcripts_for_session(session_uuid)

        if not transcripts:
            raise HTTPException(status_code=404, detail="No transcripts found for this session")

        # Build response for each transcript
        transcript_items: list[TranscriptItemResponse] = []
        for transcript in transcripts:
            # Get events for this transcript
            events = await service.get_events(
                transcript.id,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                event_types=event_types,
                limit=limit,
            )

            # Convert to response
            event_responses = [event_model_to_response(e) for e in events]

            # If not including details, strip them
            if not include_details:
                for er in event_responses:
                    er.details = EventDetailsResponse()

            transcript_items.append(
                TranscriptItemResponse(
                    transcript_id=str(transcript.id),
                    user_query=transcript.user_query,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                    created_at=transcript.created_at,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                    status=transcript.status,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                    summary=transcript_to_summary(transcript),
                    events=event_responses,
                )
            )

        return MultiTranscriptResponse(
            session_id=str(session_uuid),
            transcripts=transcript_items,
            total_transcripts=len(transcript_items),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting transcript: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/sessions/{session_id}/summary",
    response_model=SessionSummaryResponse,
    responses={
        404: {"description": "Transcript not found for this session"},
        500: {"description": "Internal server error"},
    },
)
@limiter.limit(lambda: get_config().rate_limit_transcript)
async def get_session_summary(
    request: Request,
    session_id: str,
    user: CurrentUser,
    db_session: DbSession,
) -> Any:
    """
    Get execution summary for a session.

    Returns token usage, timing, success/failure status,
    and counts of LLM calls, SQL queries, operation calls, etc.

    Results are cached for 5 minutes to improve performance.
    """
    try:
        session_uuid = await resolve_session_id(session_id, user, db_session)
        await verify_session_access(session_uuid, user, db_session)

        # Try to get from cache first
        cache_key = f"meho:transcript_summary:{session_uuid}"
        try:
            import json as json_module

            from meho_app.core.redis import get_redis_client

            config = get_config()
            redis_client = get_redis_client(config.redis_url)
            cached = await redis_client.get(cache_key)
            if cached:
                cached_data = json_module.loads(cached)
                logger.debug(f"Cache hit for summary {session_uuid}")
                return SessionSummaryResponse(**cached_data)
        except Exception as cache_err:
            logger.debug(f"Cache lookup failed (non-fatal): {cache_err}")

        service = TranscriptService(db_session)
        transcript = await service.get_transcript(session_uuid)

        if transcript is None:
            raise HTTPException(status_code=404, detail="Transcript not found for this session")

        summary = transcript_to_summary(transcript)

        # Cache the result (best effort)
        try:
            import json as json_module

            from meho_app.core.redis import get_redis_client

            config = get_config()
            redis_client = get_redis_client(config.redis_url)
            # Convert to dict for JSON serialization
            summary_dict = summary.model_dump(mode="json")
            await redis_client.setex(
                cache_key,
                SUMMARY_CACHE_TTL_SECONDS,
                json_module.dumps(summary_dict),
            )
            logger.debug(f"Cached summary for {session_uuid}")
        except Exception as cache_err:
            logger.debug(f"Cache write failed (non-fatal): {cache_err}")

        return summary

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e

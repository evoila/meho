# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Shared utility functions for the Observability API.

Part of TASK-186: Deep Observability & Introspection System.
"""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.api.dependencies import CurrentUser
from meho_app.api.observability.schemas import (
    EventDetailsResponse,
    EventResponse,
    SessionSummaryResponse,
    TokenUsageResponse,
)
from meho_app.modules.agents.models import ChatSessionModel
from meho_app.modules.agents.persistence.transcript_models import (
    SessionTranscriptModel,
    TranscriptEventModel,
)


def extract_event_tags(event: TranscriptEventModel) -> dict | None:
    """Extract factual metadata tags from event details.

    Tags are machine-readable metadata about an event -- NOT quality judgments.
    The coding agent reads these to filter and analyze transcripts programmatically.

    Args:
        event: TranscriptEventModel from database.

    Returns:
        Dict of factual tags, or None if no tags apply.
    """
    details = event.details or {}
    tags: dict = {}

    # Connector metadata (from HTTP calls and tool calls)
    if details.get("tool_name"):
        tags["operation_name"] = details["tool_name"]
    if details.get("http_url"):
        # Extract connector type from URL pattern (e.g., /api/connectors/prometheus/...)
        url = details["http_url"]
        for connector_type in (
            "prometheus",
            "loki",
            "tempo",
            "alertmanager",
            "kubernetes",
            "vmware",
        ):
            if connector_type in url.lower():
                tags["connector_type"] = connector_type
                break

    # Trust level (from tool metadata if available)
    if details.get("trust_level"):
        tags["trust_level"] = details["trust_level"]

    # Cross-system indicator
    if event.node_name and "cross" in (event.node_name or "").lower():
        tags["cross_system"] = True

    # Agent context
    if event.agent_name:
        tags["agent_name"] = event.agent_name
    if event.step_number is not None:
        tags["step_number"] = event.step_number

    # Event category (derived from type)
    event_type = event.type or ""
    if event_type in ("thought", "action", "observation", "final_answer"):
        tags["category"] = "reasoning"
    elif event_type in ("llm_call",):
        tags["category"] = "llm"
    elif event_type in ("operation_call",):
        tags["category"] = "external"
    elif event_type in ("sql_query",):
        tags["category"] = "database"
    elif event_type in ("knowledge_search", "topology_lookup"):
        tags["category"] = "context"
    elif event_type == "error":
        tags["category"] = "error"

    return tags if tags else None


def convert_details_to_response(details: dict) -> EventDetailsResponse:
    """Convert JSONB details dict to response model.

    Args:
        details: Raw JSONB details from database.

    Returns:
        EventDetailsResponse with properly typed fields.
    """
    token_usage = None
    tu = details.get("token_usage")
    if tu is not None:
        token_usage = TokenUsageResponse(
            prompt_tokens=tu.get("prompt_tokens", 0),
            completion_tokens=tu.get("completion_tokens", 0),
            total_tokens=tu.get("total_tokens", 0),
            estimated_cost_usd=tu.get("estimated_cost_usd"),
        )

    return EventDetailsResponse(
        llm_prompt=details.get("llm_prompt"),
        llm_messages=details.get("llm_messages"),
        llm_response=details.get("llm_response"),
        llm_parsed=details.get("llm_parsed"),
        token_usage=token_usage,
        llm_duration_ms=details.get("llm_duration_ms"),
        model=details.get("model"),
        http_method=details.get("http_method"),
        http_url=details.get("http_url"),
        http_headers=details.get("http_headers"),
        http_request_body=details.get("http_request_body"),
        http_response_body=details.get("http_response_body"),
        http_status_code=details.get("http_status_code"),
        http_duration_ms=details.get("http_duration_ms"),
        sql_query=details.get("sql_query"),
        sql_parameters=details.get("sql_parameters"),
        sql_row_count=details.get("sql_row_count"),
        sql_result_sample=details.get("sql_result_sample"),
        sql_duration_ms=details.get("sql_duration_ms"),
        tool_name=details.get("tool_name"),
        tool_input=details.get("tool_input"),
        tool_output=details.get("tool_output"),
        tool_duration_ms=details.get("tool_duration_ms"),
        tool_error=details.get("tool_error"),
        search_query=details.get("search_query"),
        search_type=details.get("search_type"),
        search_results=details.get("search_results"),
        search_scores=details.get("search_scores"),
        entities_extracted=details.get("entities_extracted"),
        entities_found=details.get("entities_found"),
        context_injected=details.get("context_injected"),
    )


def event_model_to_response(event: TranscriptEventModel) -> EventResponse:
    """Convert database event model to response model.

    Args:
        event: TranscriptEventModel from database.

    Returns:
        EventResponse with properly formatted fields.
    """
    return EventResponse(
        id=str(event.id),
        timestamp=event.timestamp,
        type=event.type,
        summary=event.summary,
        details=convert_details_to_response(event.details or {}),
        parent_event_id=str(event.parent_event_id) if event.parent_event_id else None,
        step_number=event.step_number,
        node_name=event.node_name,
        agent_name=event.agent_name,
        duration_ms=event.duration_ms,
        tags=extract_event_tags(event),
    )


def transcript_to_summary(
    transcript: SessionTranscriptModel,
) -> SessionSummaryResponse:
    """Convert transcript model to summary response.

    Args:
        transcript: SessionTranscriptModel from database.

    Returns:
        SessionSummaryResponse with aggregated statistics.
    """
    return SessionSummaryResponse(
        session_id=str(transcript.session_id),
        status=transcript.status,
        created_at=transcript.created_at,
        completed_at=transcript.completed_at,
        total_llm_calls=transcript.total_llm_calls,
        total_operation_calls=transcript.total_operation_calls,
        total_sql_queries=transcript.total_sql_queries,
        total_tool_calls=transcript.total_tool_calls,
        total_tokens=transcript.total_tokens,
        total_cost_usd=transcript.total_cost_usd,
        total_duration_ms=transcript.total_duration_ms,
        user_query=transcript.user_query,
        agent_type=transcript.agent_type,
    )


async def resolve_session_id(
    session_id: str,
    user: CurrentUser,
    db_session: AsyncSession,
) -> UUID:
    """Resolve session ID, handling 'latest' keyword.

    Args:
        session_id: Session ID string or 'latest'.
        user: Current authenticated user.
        db_session: Database session.

    Returns:
        Resolved UUID for the session.

    Raises:
        HTTPException: If session ID is invalid or not found.
    """
    if session_id.lower() == "latest":
        # Find the most recent session for this tenant
        stmt = (
            select(ChatSessionModel)
            .where(ChatSessionModel.tenant_id == user.tenant_id)
            .order_by(desc(ChatSessionModel.created_at))
            .limit(1)
        )
        result = await db_session.execute(stmt)
        session = result.scalar_one_or_none()

        if session is None:
            raise HTTPException(status_code=404, detail="No sessions found")

        return session.id

    try:
        return UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"Invalid session ID format: {session_id}"
        ) from None


async def verify_session_access(
    session_id: UUID,
    user: CurrentUser,
    db_session: AsyncSession,
) -> ChatSessionModel:
    """Verify user has access to the session.

    Args:
        session_id: Session UUID.
        user: Current authenticated user.
        db_session: Database session.

    Returns:
        The ChatSessionModel if accessible.

    Raises:
        HTTPException: If session not found or access denied.
    """
    stmt = select(ChatSessionModel).where(ChatSessionModel.id == session_id)
    result = await db_session.execute(stmt)
    session = result.scalar_one_or_none()

    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")

    return session

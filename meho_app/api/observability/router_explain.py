# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Session explanation endpoint for the Observability API.

Provides human-readable explanations of session execution.

Part of TASK-186: Deep Observability & Introspection System.
"""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request

from meho_app.api.dependencies import CurrentUser, DbSession
from meho_app.api.observability.schemas import (
    SessionExplanationResponse,
)
from meho_app.api.observability.utils import (
    resolve_session_id,
    transcript_to_summary,
    verify_session_access,
)
from meho_app.core.config import get_config
from meho_app.core.otel import get_logger
from meho_app.core.rate_limiting import get_limiter
from meho_app.modules.agents.persistence.transcript_models import SessionTranscriptModel
from meho_app.modules.agents.persistence.transcript_service import TranscriptService

logger = get_logger(__name__)

router = APIRouter()

# Get rate limiter
limiter = get_limiter()


def _generate_explanation(  # NOSONAR (cognitive complexity)
    transcript: SessionTranscriptModel,
    events: list,
    focus: str,
) -> tuple[str, list[dict]]:
    """
    Generate a human-readable explanation of the session.

    Args:
        transcript: Session transcript model.
        events: List of transcript events.
        focus: Focus mode - 'overview', 'errors', 'performance', 'decisions'.

    Returns:
        Tuple of (explanation text, key events list).
    """
    key_events: list[dict] = []

    # Build explanation based on focus
    if focus == "errors":
        # Focus on errors and failures
        error_events = [
            e
            for e in events
            if e.type == "error"
            or (e.details and e.details.get("tool_error"))
            or (e.details and e.details.get("http_status_code", 0) >= 400)
        ]

        if not error_events:
            explanation = "Session completed successfully with no errors.\n\n"
            explanation += f"- Total duration: {transcript.total_duration_ms:.0f}ms\n"
            explanation += f"- LLM calls: {transcript.total_llm_calls}\n"
            explanation += f"- Operation calls: {transcript.total_operation_calls}\n"
        else:
            explanation = f"Found {len(error_events)} error(s) in this session:\n\n"
            for i, e in enumerate(error_events[:5], 1):
                explanation += f"{i}. **{e.type}** at {e.timestamp.strftime('%H:%M:%S')}\n"
                explanation += f"   {e.summary}\n"
                if e.details:
                    if e.details.get("tool_error"):
                        explanation += f"   Error: {e.details.get('tool_error')}\n"
                    if e.details.get("http_status_code"):
                        explanation += f"   HTTP {e.details.get('http_status_code')}\n"
                explanation += "\n"
                key_events.append(
                    {
                        "id": str(e.id),
                        "type": e.type,
                        "summary": e.summary,
                        "timestamp": e.timestamp.isoformat(),
                    }
                )

    elif focus == "performance":
        # Focus on timing and token usage
        explanation = "## Performance Analysis\n\n"
        explanation += f"**Total Duration:** {transcript.total_duration_ms:.0f}ms\n"
        explanation += f"**Total Tokens:** {transcript.total_tokens:,}\n"
        if transcript.total_cost_usd:
            explanation += f"**Estimated Cost:** ${transcript.total_cost_usd:.4f}\n"
        explanation += "\n"

        # Find slowest operations
        timed_events = [
            e
            for e in events
            if e.details
            and (
                e.details.get("llm_duration_ms")
                or e.details.get("http_duration_ms")
                or e.details.get("tool_duration_ms")
            )
        ]

        if timed_events:
            # Sort by duration
            def get_duration(e: Any) -> int:
                d = e.details or {}
                return max(
                    d.get("llm_duration_ms", 0) or 0,
                    d.get("http_duration_ms", 0) or 0,
                    d.get("tool_duration_ms", 0) or 0,
                )

            sorted_events = sorted(timed_events, key=get_duration, reverse=True)[:5]
            explanation += "### Slowest Operations\n\n"
            for i, e in enumerate(sorted_events, 1):
                duration = get_duration(e)
                explanation += f"{i}. **{e.type}** - {duration:.0f}ms\n"
                if len(e.summary) > 100:
                    explanation += f"   {e.summary[:100]}...\n"
                else:
                    explanation += f"   {e.summary}\n"
                key_events.append(
                    {
                        "id": str(e.id),
                        "type": e.type,
                        "duration_ms": duration,
                        "summary": e.summary,
                    }
                )

    elif focus == "decisions":
        # Focus on LLM reasoning
        llm_events = [e for e in events if e.type in ("thought", "llm_call")]

        explanation = "## Decision Analysis\n\n"
        explanation += f"The agent made {len(llm_events)} reasoning step(s):\n\n"

        for i, e in enumerate(llm_events[:10], 1):
            explanation += f"### Step {i}: {e.summary[:80]}\n"
            if e.details and e.details.get("llm_parsed"):
                parsed = e.details.get("llm_parsed", {})
                if parsed.get("thought"):
                    explanation += f"**Thought:** {parsed.get('thought')[:200]}\n"
                if parsed.get("action"):
                    action = parsed.get("action", {})
                    explanation += f"**Action:** {action.get('tool', 'unknown')}\n"
            explanation += "\n"
            key_events.append(
                {
                    "id": str(e.id),
                    "type": e.type,
                    "summary": e.summary,
                }
            )

    else:  # overview (default)
        # General overview of what happened
        explanation = "## Session Overview\n\n"
        explanation += f"**Query:** {transcript.user_query or 'N/A'}\n"
        explanation += f"**Status:** {transcript.status}\n"
        explanation += f"**Duration:** {transcript.total_duration_ms:.0f}ms\n\n"

        explanation += "### Execution Summary\n\n"
        explanation += f"- **LLM Calls:** {transcript.total_llm_calls}\n"
        explanation += f"- **Operation Calls:** {transcript.total_operation_calls}\n"
        explanation += f"- **SQL Queries:** {transcript.total_sql_queries}\n"
        explanation += f"- **Tool Calls:** {transcript.total_tool_calls}\n"
        explanation += f"- **Total Tokens:** {transcript.total_tokens:,}\n"
        if transcript.total_cost_usd:
            explanation += f"- **Estimated Cost:** ${transcript.total_cost_usd:.4f}\n"
        explanation += "\n"

        # Timeline of major events
        major_events = [
            e
            for e in events
            if e.type in ("thought", "action", "observation", "error", "final_answer")
        ][:10]

        if major_events:
            explanation += "### Event Timeline\n\n"
            for i, e in enumerate(major_events, 1):
                time_str = e.timestamp.strftime("%H:%M:%S")
                explanation += f"{i}. [{time_str}] **{e.type}**: {e.summary[:100]}\n"
                key_events.append(
                    {
                        "id": str(e.id),
                        "type": e.type,
                        "timestamp": e.timestamp.isoformat(),
                        "summary": e.summary,
                    }
                )

    return explanation, key_events


@router.get(
    "/sessions/{session_id}/explain",
    response_model=SessionExplanationResponse,
    responses={
        400: {"description": "Invalid focus '...'. Must be one of: ..."},
        404: {"description": "Transcript not found for this session"},
        500: {"description": "Internal server error"},
    },
)
@limiter.limit(lambda: get_config().rate_limit_transcript)
async def explain_session(
    request: Request,
    session_id: str,
    user: CurrentUser,
    db_session: DbSession,
    focus: Annotated[
        str,
        Query(
            description="Focus mode: 'overview', 'errors', 'performance', 'decisions'",
        ),
    ] = "overview",
) -> Any:
    """
    Generate a human-readable explanation of what happened during a session.

    Summarizes the execution flow, decisions made, and any issues encountered.
    Useful for debugging and understanding agent behavior.

    Focus modes:
    - **overview**: General summary of the session
    - **errors**: Focus on errors and failures
    - **performance**: Analysis of timing and token usage
    - **decisions**: Focus on LLM reasoning and tool choices
    """
    try:
        # Validate focus
        valid_focuses = ["overview", "errors", "performance", "decisions"]
        if focus not in valid_focuses:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid focus '{focus}'. Must be one of: {valid_focuses}",
            )

        session_uuid = await resolve_session_id(session_id, user, db_session)
        await verify_session_access(session_uuid, user, db_session)

        service = TranscriptService(db_session)
        transcript = await service.get_transcript(session_uuid)

        if transcript is None:
            raise HTTPException(status_code=404, detail="Transcript not found for this session")

        # Get all events for analysis
        events = await service.get_events(transcript.id, limit=500)  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access

        # Generate explanation
        explanation, key_events = _generate_explanation(transcript, events, focus)

        return SessionExplanationResponse(
            session_id=str(transcript.session_id),
            focus=focus,
            explanation=explanation,
            summary=transcript_to_summary(transcript),
            key_events=key_events,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error explaining session: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e

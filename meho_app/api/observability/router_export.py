# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Export endpoints for the Observability API.

Provides endpoints to:
- Export single session transcripts (JSON/CSV)
- Bulk export multiple transcripts

Part of TASK-186: Deep Observability & Introspection System.
"""

import csv
import io
import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, select

from meho_app.api.dependencies import CurrentUser, DbSession
from meho_app.api.observability.schemas import (
    BulkExportRequest,
    ExportFormat,
)
from meho_app.api.observability.utils import (
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

router = APIRouter()

# Get rate limiter
limiter = get_limiter()


def _events_to_csv(events: list[TranscriptEventModel]) -> str:
    """Convert transcript events to CSV format.

    Args:
        events: List of transcript events.

    Returns:
        CSV string with headers and data rows.
    """
    output = io.StringIO()
    writer = csv.writer(output)

    # Write header
    writer.writerow(
        [
            "id",
            "timestamp",
            "type",
            "summary",
            "duration_ms",
            "step_number",
            "node_name",
            "agent_name",
            # LLM fields
            "llm_model",
            "llm_prompt_tokens",
            "llm_completion_tokens",
            "llm_total_tokens",
            "llm_cost_usd",
            # HTTP fields
            "http_method",
            "http_url",
            "http_status_code",
            "http_duration_ms",
            # Tool fields
            "tool_name",
            "tool_error",
        ]
    )

    # Write data rows
    for event in events:
        details = event.details or {}
        token_usage = details.get("token_usage", {}) or {}

        writer.writerow(
            [
                str(event.id),
                event.timestamp.isoformat() if event.timestamp else "",
                event.type,
                event.summary[:500] if event.summary else "",  # Truncate for CSV
                event.duration_ms or "",
                event.step_number or "",
                event.node_name or "",
                event.agent_name or "",
                # LLM fields
                details.get("model", ""),
                token_usage.get("prompt_tokens", ""),
                token_usage.get("completion_tokens", ""),
                token_usage.get("total_tokens", ""),
                token_usage.get("estimated_cost_usd", ""),
                # HTTP fields
                details.get("http_method", ""),
                details.get("http_url", ""),
                details.get("http_status_code", ""),
                details.get("http_duration_ms", ""),
                # Tool fields
                details.get("tool_name", ""),
                details.get("tool_error", ""),
            ]
        )

    return output.getvalue()


def _transcript_to_export_dict(
    transcript: SessionTranscriptModel,
    events: list[TranscriptEventModel],
) -> dict:
    """Convert transcript and events to export dictionary.

    Args:
        transcript: Session transcript model.
        events: List of transcript events.

    Returns:
        Dictionary suitable for JSON export.
    """
    return {
        "session_id": str(transcript.session_id),
        "created_at": transcript.created_at.isoformat(),
        "completed_at": transcript.completed_at.isoformat() if transcript.completed_at else None,
        "status": transcript.status,
        "user_query": transcript.user_query,
        "agent_type": transcript.agent_type,
        "summary": {
            "total_llm_calls": transcript.total_llm_calls,
            "total_operation_calls": transcript.total_operation_calls,
            "total_sql_queries": transcript.total_sql_queries,
            "total_tool_calls": transcript.total_tool_calls,
            "total_tokens": transcript.total_tokens,
            "total_cost_usd": transcript.total_cost_usd,
            "total_duration_ms": transcript.total_duration_ms,
        },
        "events": [
            {
                "id": str(e.id),
                "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                "type": e.type,
                "summary": e.summary,
                "details": e.details,
                "step_number": e.step_number,
                "node_name": e.node_name,
                "agent_name": e.agent_name,
                "duration_ms": e.duration_ms,
            }
            for e in events
        ],
    }


@router.get("/export/sessions/{session_id}")
@limiter.limit(lambda: get_config().rate_limit_export)
async def export_session_transcript(
    request: Request,
    session_id: str,
    user: CurrentUser,
    db_session: DbSession,
    format: ExportFormat = Query(default=ExportFormat.JSON),
    include_details: bool = Query(default=True, description="Include event details"),
    event_types: list[str] | None = Query(default=None, description="Filter by event types"),
):
    """
    Export a session transcript as a downloadable file.

    Supports JSON and CSV formats. JSON includes full event details,
    while CSV provides a flattened view suitable for spreadsheets.

    Returns a file download with appropriate Content-Disposition header.
    """
    try:
        # Resolve and verify session access
        session_uuid = await resolve_session_id(session_id, user, db_session)
        await verify_session_access(session_uuid, user, db_session)

        # Get transcript
        service = TranscriptService(db_session)
        transcript = await service.get_transcript(session_uuid)

        if transcript is None:
            raise HTTPException(status_code=404, detail="Transcript not found for this session")

        # Get events
        events = await service.get_events(
            transcript.id,
            event_types=event_types,
            limit=10000,  # Higher limit for exports
        )

        # Generate filename
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
        session_short = str(session_uuid)[:8]

        if format == ExportFormat.CSV:
            # Generate CSV
            csv_content = _events_to_csv(events)
            filename = f"meho-transcript-{session_short}-{timestamp}.csv"

            return StreamingResponse(
                iter([csv_content]),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
            )
        else:
            # Generate JSON
            export_data = _transcript_to_export_dict(transcript, events)

            # Optionally strip details
            if not include_details:
                for event in export_data["events"]:
                    event["details"] = {}

            json_content = json.dumps(export_data, indent=2, default=str)
            filename = f"meho-transcript-{session_short}-{timestamp}.json"

            return StreamingResponse(
                iter([json_content]),
                media_type="application/json",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting transcript: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/export/bulk")
@limiter.limit(lambda: get_config().rate_limit_export)
async def export_transcripts_bulk(
    request: Request,
    export_request: BulkExportRequest,
    user: CurrentUser,
    db_session: DbSession,
    format: ExportFormat = Query(default=ExportFormat.JSON),
):
    """
    Export multiple session transcripts as a downloadable file.

    Exports up to max_sessions (default 10, max 50) transcripts in a single file.
    Useful for compliance, auditing, or data analysis.

    For very large exports, consider using the single-session export endpoint
    iteratively or exporting in smaller batches.
    """
    try:
        # Build query for sessions
        stmt = (
            select(SessionTranscriptModel)
            .join(
                ChatSessionModel,
                SessionTranscriptModel.session_id == ChatSessionModel.id,
            )
            .where(ChatSessionModel.tenant_id == user.tenant_id)
            .where(SessionTranscriptModel.deleted_at.is_(None))  # Exclude soft-deleted
        )

        # Filter by session IDs if provided
        if export_request.session_ids:
            try:
                session_uuids = [UUID(sid) for sid in export_request.session_ids]
            except ValueError as e:
                raise HTTPException(
                    status_code=400, detail=f"Invalid session ID format: {e}"
                ) from e
            stmt = stmt.where(SessionTranscriptModel.session_id.in_(session_uuids))

        # Filter by time range
        if export_request.since:
            stmt = stmt.where(SessionTranscriptModel.created_at >= export_request.since)
        if export_request.until:
            stmt = stmt.where(SessionTranscriptModel.created_at <= export_request.until)

        # Order and limit
        stmt = stmt.order_by(desc(SessionTranscriptModel.created_at))
        stmt = stmt.limit(export_request.max_sessions)

        result = await db_session.execute(stmt)
        transcripts = list(result.scalars().all())

        if not transcripts:
            raise HTTPException(status_code=404, detail="No transcripts found matching criteria")

        # Get events for each transcript
        service = TranscriptService(db_session)
        export_data = {
            "exported_at": datetime.now(tz=UTC).isoformat(),
            "sessions_count": len(transcripts),
            "sessions": [],
        }
        total_events = 0

        for transcript in transcripts:
            events = await service.get_events(
                transcript.id,
                event_types=export_request.event_types,
                limit=1000,  # Per-session limit for bulk exports
            )
            total_events += len(events)

            session_data = _transcript_to_export_dict(transcript, events)

            # Optionally strip details
            if not export_request.include_details:
                for event in session_data["events"]:
                    event["details"] = {}

            export_data["sessions"].append(session_data)

        export_data["total_events"] = total_events

        # Generate filename
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")

        if format == ExportFormat.CSV:
            # For CSV, flatten all events from all sessions
            all_events = []
            for transcript in transcripts:
                events = await service.get_events(
                    transcript.id,
                    event_types=export_request.event_types,
                    limit=1000,
                )
                all_events.extend(events)

            csv_content = _events_to_csv(all_events)
            filename = f"meho-bulk-export-{timestamp}.csv"

            return StreamingResponse(
                iter([csv_content]),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
            )
        else:
            json_content = json.dumps(export_data, indent=2, default=str)
            filename = f"meho-bulk-export-{timestamp}.json"

            return StreamingResponse(
                iter([json_content]),
                media_type="application/json",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error bulk exporting transcripts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e

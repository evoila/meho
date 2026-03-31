# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Session-related MCP tool handlers."""

import json
from typing import Optional
import httpx

from ..config import get_meho_api_url, get_auth_headers


async def list_sessions(
    limit: int = 10,
    status: Optional[str] = None,
    since_minutes: int = 60,
    format: str = "json",
) -> str:
    """
    List recent MEHO chat sessions with their status and summary.

    Use this to find a session ID for deeper introspection.

    Args:
        limit: Maximum number of sessions to return. Defaults to 10.
        status: Filter by session status ('active', 'completed', 'failed', or None for all).
        since_minutes: Only return sessions from the last N minutes. Defaults to 60.
        format: Output format. 'json' for compact machine-readable, 'text' for pretty-printed. Defaults to 'json'.

    Returns:
        JSON string with list of sessions.
    """
    url = f"{get_meho_api_url()}/api/observability/sessions"
    params = {"limit": limit, "since_minutes": since_minutes}
    if status:
        params["status"] = status

    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            params=params,
            headers=get_auth_headers(),
            timeout=30.0,
        )
        response.raise_for_status()
        indent = None if format == "json" else 2
        return json.dumps(response.json(), indent=indent)


async def get_transcript(
    session_id: str,
    include_details: bool = True,
    event_types: Optional[list[str]] = None,
    compact: bool = False,
    format: str = "json",
) -> str:
    """
    Get the complete execution transcript for a MEHO chat session.

    Returns all events with their full details: LLM prompts/responses,
    SQL queries, operation calls, tool invocations.

    Args:
        session_id: The session ID to retrieve. Use 'latest' for the most recent session.
        include_details: Include full event details (prompts, responses, payloads). Defaults to True.
        event_types: Filter to specific event types: 'llm_call', 'sql_query', 'operation_call', etc.
        compact: Return a compact summary instead of full transcript. Defaults to False.
        format: Output format. 'json' for compact machine-readable, 'text' for pretty-printed. Defaults to 'json'.

    Returns:
        JSON string with full transcript.
    """
    url = f"{get_meho_api_url()}/api/observability/sessions/{session_id}/transcript"
    params = {
        "include_details": include_details,
        "limit": 100 if compact else 1000,
    }
    if event_types:
        params["event_types"] = event_types

    indent = None if format == "json" else 2

    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            params=params,
            headers=get_auth_headers(),
            timeout=60.0,
        )
        response.raise_for_status()
        data = response.json()

        if compact:
            # Return only summary and event summaries
            compact_data = {
                "session_id": data.get("session_id"),
                "summary": data.get("summary"),
                "events": [
                    {
                        "id": e.get("id"),
                        "type": e.get("type"),
                        "summary": e.get("summary"),
                        "timestamp": e.get("timestamp"),
                    }
                    for e in data.get("events", [])
                ],
            }
            return json.dumps(compact_data, indent=indent)

        return json.dumps(data, indent=indent)


async def get_summary(session_id: str, format: str = "json") -> str:
    """
    Get execution summary for a session.

    Returns token usage, timing, success/failure status,
    and counts of LLM calls, SQL queries, HTTP calls, etc.

    Args:
        session_id: The session ID. Use 'latest' for most recent.
        format: Output format. 'json' for compact machine-readable, 'text' for pretty-printed. Defaults to 'json'.

    Returns:
        JSON string with session summary.
    """
    url = f"{get_meho_api_url()}/api/observability/sessions/{session_id}/summary"

    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            headers=get_auth_headers(),
            timeout=30.0,
        )
        response.raise_for_status()
        indent = None if format == "json" else 2
        return json.dumps(response.json(), indent=indent)

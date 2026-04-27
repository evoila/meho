# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Event-related MCP tool handlers."""

import json

import httpx

from ..config import get_auth_headers, get_meho_api_url


async def get_event_details(event_id: str, session_id: str, format: str = "json") -> str:
    """
    Get full details for a specific event by ID.

    Use after meho_get_transcript to drill into a specific event.

    Args:
        event_id: The event ID to retrieve.
        session_id: The session ID containing the event.
        format: Output format. 'json' for compact machine-readable, 'text' for pretty-printed. Defaults to 'json'.

    Returns:
        JSON string with full event details.
    """
    url = f"{get_meho_api_url()}/api/observability/sessions/{session_id}/events/{event_id}"

    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            headers=get_auth_headers(),
            timeout=30.0,
        )
        response.raise_for_status()
        indent = None if format == "json" else 2
        return json.dumps(response.json(), indent=indent)


async def search_events(
    query: str,
    event_type: str | None = None,
    since_minutes: int = 60,
    limit: int = 20,
    format: str = "json",
) -> str:
    """
    Search for events matching criteria across recent sessions.

    Useful for finding patterns or specific occurrences.

    Args:
        query: Text to search for in event content (prompts, responses, queries, etc.)
        event_type: Filter to specific event type. Optional.
        since_minutes: Search sessions from the last N minutes. Defaults to 60.
        limit: Maximum results to return. Defaults to 20.
        format: Output format. 'json' for compact machine-readable, 'text' for pretty-printed. Defaults to 'json'.

    Returns:
        JSON string with search results.
    """
    url = f"{get_meho_api_url()}/api/observability/search"
    params = {
        "query": query,
        "since_minutes": since_minutes,
        "limit": limit,
    }
    if event_type:
        params["event_type"] = event_type

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

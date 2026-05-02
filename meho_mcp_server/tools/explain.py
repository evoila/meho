# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Session explanation MCP tool handlers."""

import json

import httpx

from ..config import get_auth_headers, get_meho_api_url


async def explain_session(
    session_id: str,
    focus: str = "overview",
    format: str = "json",
) -> str:
    """
    Get a human-readable explanation of what happened during a session.

    Summarizes the flow, decisions, and any issues encountered.

    Args:
        session_id: The session ID. Use 'latest' for most recent.
        focus: What aspect to focus the explanation on:
            - 'overview': General summary of the session (default)
            - 'errors': Focus on errors and failures
            - 'performance': Analysis of timing and token usage
            - 'decisions': Focus on LLM reasoning and tool choices
        format: Output format. 'json' for compact machine-readable, 'text' for pretty-printed. Defaults to 'json'.

    Returns:
        JSON string with explanation, summary, and key events.
    """
    url = f"{get_meho_api_url()}/api/observability/sessions/{session_id}/explain"
    params = {"focus": focus}

    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            params=params,
            headers=get_auth_headers(),
            timeout=60.0,
        )
        response.raise_for_status()
        indent = None if format == "json" else 2
        return json.dumps(response.json(), indent=indent)

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""LLM call MCP tool handlers."""

import json
import httpx

from ..config import get_meho_api_url, get_auth_headers


async def get_llm_calls(
    session_id: str,
    include_system_prompt: bool = True,
    include_conversation_history: bool = True,
    limit: int = 100,
    format: str = "json",
) -> str:
    """
    Get all LLM calls from a session with full prompts and responses.

    Use this to understand MEHO's reasoning and decision-making.

    Args:
        session_id: The session ID. Use 'latest' for most recent.
        include_system_prompt: Include the full system prompt. Defaults to True.
        include_conversation_history: Include conversation context sent to LLM. Defaults to True.
        limit: Maximum number of LLM calls to return. Defaults to 100.
        format: Output format. 'json' for compact machine-readable, 'text' for pretty-printed. Defaults to 'json'.

    Returns:
        JSON string with list of LLM calls.
    """
    url = f"{get_meho_api_url()}/api/observability/sessions/{session_id}/llm-calls"
    params = {
        "include_system_prompt": include_system_prompt,
        "include_conversation_history": include_conversation_history,
        "limit": limit,
    }

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

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Operation call MCP tool handlers."""

import json
from typing import Optional
import httpx

from ..config import get_meho_api_url, get_auth_headers


async def get_operation_calls(
    session_id: str,
    connector_id: Optional[str] = None,
    include_bodies: bool = True,
    status_filter: str = "all",
    limit: int = 100,
    format: str = "json",
) -> str:
    """
    Get all operation calls (REST/SOAP/VMware) made during a session with full request/response bodies.

    Use this to debug connector API interactions.

    Args:
        session_id: The session ID. Use 'latest' for most recent.
        connector_id: Filter to a specific connector. Optional.
        include_bodies: Include request/response bodies. Defaults to True.
        status_filter: Filter by response status: 'all', 'success', or 'error'. Defaults to 'all'.
        limit: Maximum number of calls to return. Defaults to 100.
        format: Output format. 'json' for compact machine-readable, 'text' for pretty-printed. Defaults to 'json'.

    Returns:
        JSON string with list of operation calls.
    """
    url = f"{get_meho_api_url()}/api/observability/sessions/{session_id}/operation-calls"
    params = {
        "include_bodies": include_bodies,
        "limit": limit,
    }
    if connector_id:
        params["connector_id"] = connector_id
    if status_filter and status_filter != "all":
        params["status_filter"] = status_filter

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

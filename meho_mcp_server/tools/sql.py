# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""SQL query MCP tool handlers."""

import json
import httpx

from ..config import get_meho_api_url, get_auth_headers


async def get_sql_queries(
    session_id: str,
    include_results: bool = True,
    limit: int = 100,
    format: str = "json",
) -> str:
    """
    Get all SQL queries executed during a session with parameters and results.

    Use this to debug database interactions.

    Args:
        session_id: The session ID. Use 'latest' for most recent.
        include_results: Include query result samples (first 10 rows). Defaults to True.
        limit: Maximum number of queries to return. Defaults to 100.
        format: Output format. 'json' for compact machine-readable, 'text' for pretty-printed. Defaults to 'json'.

    Returns:
        JSON string with list of SQL queries.
    """
    url = f"{get_meho_api_url()}/api/observability/sessions/{session_id}/sql-queries"
    params = {
        "include_results": include_results,
        "limit": limit,
    }

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

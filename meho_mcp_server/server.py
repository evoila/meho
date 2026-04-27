# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
MEHO Introspection MCP Server.

Provides Model Context Protocol tools for introspecting MEHO's execution
behavior. Refactored to use FastMCP decorator syntax.

Usage:
    python -m meho_mcp_server.server

Environment Variables:
    MEHO_API_URL: Base URL for MEHO API (default: http://localhost:8000)
    MEHO_AUTH_TOKEN: Bearer token for authentication (optional)
"""

import asyncio
import logging

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("meho-introspection")


# ---------------------------------------------------------------------------
# Tool 1: meho_list_sessions
# ---------------------------------------------------------------------------


@mcp.tool(
    name="meho_list_sessions",
    description=(
        "List recent MEHO chat sessions with their status and summary. "
        "Use this to find a session ID for deeper introspection."
    ),
)
async def list_sessions_tool(
    limit: int = 10,
    status: str = "all",
    since_minutes: int = 60,
) -> str:
    """List recent MEHO chat sessions."""
    from .tools import list_sessions

    return await list_sessions(limit=limit, status=status, since_minutes=since_minutes)


# ---------------------------------------------------------------------------
# Tool 2: meho_get_transcript
# ---------------------------------------------------------------------------


@mcp.tool(
    name="meho_get_transcript",
    description=(
        "Get the complete execution transcript for a MEHO chat session. "
        "Returns all events with their full details: LLM prompts/responses, "
        "SQL queries, HTTP calls, tool invocations."
    ),
)
async def get_transcript_tool(
    session_id: str,
    include_details: bool = True,
    event_types: list[str] | None = None,
    compact: bool = False,
) -> str:
    """Get execution transcript for a session."""
    from .tools import get_transcript

    return await get_transcript(
        session_id=session_id,
        include_details=include_details,
        event_types=event_types,
        compact=compact,
    )


# ---------------------------------------------------------------------------
# Tool 3: meho_get_summary
# ---------------------------------------------------------------------------


@mcp.tool(
    name="meho_get_summary",
    description=(
        "Get execution summary for a session: token usage, timing, "
        "success/failure, counts of LLM calls, SQL queries, HTTP calls."
    ),
)
async def get_summary_tool(session_id: str) -> str:
    """Get execution summary for a session."""
    from .tools import get_summary

    return await get_summary(session_id=session_id)


# ---------------------------------------------------------------------------
# Tool 4: meho_get_llm_calls
# ---------------------------------------------------------------------------


@mcp.tool(
    name="meho_get_llm_calls",
    description=(
        "Get all LLM calls from a session with full prompts and responses. "
        "Use this to understand MEHO's reasoning and decision-making."
    ),
)
async def get_llm_calls_tool(
    session_id: str,
    include_system_prompt: bool = True,
    include_conversation_history: bool = True,
) -> str:
    """Get LLM calls from a session."""
    from .tools import get_llm_calls

    return await get_llm_calls(
        session_id=session_id,
        include_system_prompt=include_system_prompt,
        include_conversation_history=include_conversation_history,
    )


# ---------------------------------------------------------------------------
# Tool 5: meho_get_sql_queries
# ---------------------------------------------------------------------------


@mcp.tool(
    name="meho_get_sql_queries",
    description=(
        "Get all SQL queries executed during a session with parameters "
        "and results. Use this to debug database interactions."
    ),
)
async def get_sql_queries_tool(
    session_id: str,
    include_results: bool = True,
) -> str:
    """Get SQL queries from a session."""
    from .tools import get_sql_queries

    return await get_sql_queries(session_id=session_id, include_results=include_results)


# ---------------------------------------------------------------------------
# Tool 6: meho_get_operation_calls
# ---------------------------------------------------------------------------


@mcp.tool(
    name="meho_get_operation_calls",
    description=(
        "Get all operation calls (REST/SOAP/VMware) made during a session "
        "with full request/response bodies. Use this to debug connector "
        "API interactions."
    ),
)
async def get_operation_calls_tool(
    session_id: str,
    connector_id: str | None = None,
    include_bodies: bool = True,
    status_filter: str = "all",
) -> str:
    """Get operation calls from a session."""
    from .tools import get_operation_calls

    return await get_operation_calls(
        session_id=session_id,
        connector_id=connector_id,
        include_bodies=include_bodies,
        status_filter=status_filter,
    )


# ---------------------------------------------------------------------------
# Tool 7: meho_get_event_details
# ---------------------------------------------------------------------------


@mcp.tool(
    name="meho_get_event_details",
    description=(
        "Get full details for a specific event by ID. Use after "
        "meho_get_transcript to drill into a specific event."
    ),
)
async def get_event_details_tool(event_id: str, session_id: str) -> str:
    """Get details for a specific event."""
    from .tools import get_event_details

    return await get_event_details(event_id=event_id, session_id=session_id)


# ---------------------------------------------------------------------------
# Tool 8: meho_search_events
# ---------------------------------------------------------------------------


@mcp.tool(
    name="meho_search_events",
    description=(
        "Search for events matching criteria across recent sessions. "
        "Useful for finding patterns or specific occurrences."
    ),
)
async def search_events_tool(
    query: str,
    event_type: str | None = None,
    since_minutes: int = 60,
    limit: int = 20,
) -> str:
    """Search events across sessions."""
    from .tools import search_events

    return await search_events(
        query=query,
        event_type=event_type,
        since_minutes=since_minutes,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Tool 9: meho_explain_session
# ---------------------------------------------------------------------------


@mcp.tool(
    name="meho_explain_session",
    description=(
        "Get a human-readable explanation of what happened during a session. "
        "Summarizes the flow, decisions, and any issues encountered."
    ),
)
async def explain_session_tool(
    session_id: str,
    focus: str = "overview",
) -> str:
    """Get a human-readable session explanation."""
    from .tools import explain_session

    return await explain_session(session_id=session_id, focus=focus)


# ---------------------------------------------------------------------------
# Tool 10: meho_send_message
# ---------------------------------------------------------------------------


@mcp.tool(
    name="meho_send_message",
    description=(
        "Send a diagnostic query to MEHO and wait for the complete response. "
        "Use this to test MEHO's agent reasoning by sending queries and "
        "examining the results. Returns the final response text, session ID "
        "for follow-up queries, and execution statistics."
    ),
)
async def send_message_tool(
    message: str,
    session_id: str | None = None,
    timeout: int = 120,
) -> str:
    """Send a diagnostic query to MEHO."""
    from .tools import send_meho_message

    return await send_meho_message(message=message, session_id=session_id, timeout=timeout)


# ---------------------------------------------------------------------------
# Tool 11: meho_audit_transcript
# ---------------------------------------------------------------------------


@mcp.tool(
    name="meho_audit_transcript",
    description=(
        "Audit a session transcript for completeness and coverage. "
        "Returns structured checks: was there a final answer, did the "
        "agent use multiple connectors, how many reasoning steps, any errors."
    ),
)
async def audit_transcript_tool(session_id: str) -> str:
    """Audit a session transcript."""
    from .tools import audit_transcript

    return await audit_transcript(session_id=session_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main():
    """Run the MCP introspection server via stdio transport."""
    await mcp.run(transport="stdio")


def run():
    """Synchronous entry point for console_scripts."""
    asyncio.run(main())


if __name__ == "__main__":
    run()

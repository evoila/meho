# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Telemetry audit MCP tool handler."""

import json

import httpx

from ..config import get_auth_headers, get_meho_api_url


async def audit_transcript(  # NOSONAR (cognitive complexity)
    session_id: str,
    format: str = "json",
) -> str:
    """
    Audit a session transcript for completeness and coverage.

    Provides a structured check of whether the session completed successfully,
    which connectors were used, how many reasoning steps occurred, and any errors.
    Use as a quick health check before diving into detailed transcript analysis.

    Args:
        session_id: The session ID to audit. Use 'latest' for most recent.
        format: Output format. 'json' for compact machine-readable, 'text' for pretty-printed. Defaults to 'json'.

    Returns:
        JSON string with audit results including completeness, coverage, and signals.
    """
    base_url = get_meho_api_url()
    headers = get_auth_headers()

    async with httpx.AsyncClient() as client:
        # Fetch full transcript with details
        transcript_resp = await client.get(
            f"{base_url}/api/observability/sessions/{session_id}/transcript",
            params={"include_details": True, "limit": 1000},
            headers=headers,
            timeout=60.0,
        )
        transcript_resp.raise_for_status()
        transcript_data = transcript_resp.json()

        # Fetch session summary
        summary_resp = await client.get(
            f"{base_url}/api/observability/sessions/{session_id}/summary",
            headers=headers,
            timeout=30.0,
        )
        summary_resp.raise_for_status()
        summary_data = summary_resp.json()

    events = transcript_data.get("events", [])
    summary = summary_data

    # ====================================================================
    # Completeness checks
    # ====================================================================
    has_user_query = bool(summary.get("user_query"))

    has_final_answer = any(e.get("type") == "final_answer" for e in events)

    # Check for at least one thought -> action -> observation sequence
    has_reasoning_chain = _check_reasoning_chain(events)

    session_completed = summary.get("status") == "completed"

    all_events_typed = all(bool(e.get("type")) for e in events)

    completeness_checks = {
        "has_user_query": has_user_query,
        "has_final_answer": has_final_answer,
        "has_reasoning_chain": has_reasoning_chain,
        "session_completed": session_completed,
        "all_events_typed": all_events_typed,
    }
    score = sum(1 for v in completeness_checks.values() if v)
    completeness_checks["score"] = f"{score}/5"

    # ====================================================================
    # Coverage metrics
    # ====================================================================
    event_type_counts: dict[str, int] = {}
    unique_connector_types: set[str] = set()
    unique_operations: set[str] = set()
    total_reasoning_steps = 0
    cross_system_events = 0

    connector_keywords = ("prometheus", "loki", "tempo", "alertmanager", "kubernetes", "vmware")

    for event in events:
        # Event type counts
        etype = event.get("type", "unknown")
        event_type_counts[etype] = event_type_counts.get(etype, 0) + 1

        # Count reasoning steps (thought events)
        if etype == "thought":
            total_reasoning_steps += 1

        # Cross-system detection
        node_name = event.get("node_name") or ""
        if "cross" in node_name.lower():
            cross_system_events += 1

        # Extract connector types and operations from details
        details = event.get("details") or {}

        # From HTTP URLs
        http_url = details.get("http_url") or ""
        for ct in connector_keywords:
            if ct in http_url.lower():
                unique_connector_types.add(ct)

        # From tags (if API returns them)
        tags = event.get("tags") or {}
        if tags.get("connector_type"):
            unique_connector_types.add(tags["connector_type"])

        # Operation names from tool calls
        tool_name = details.get("tool_name")
        if tool_name:
            unique_operations.add(tool_name)

    coverage = {
        "event_type_counts": event_type_counts,
        "unique_connector_types": sorted(unique_connector_types),
        "unique_operations": sorted(unique_operations),
        "total_reasoning_steps": total_reasoning_steps,
        "cross_system_events": cross_system_events,
    }

    # ====================================================================
    # Quality signals (factual, not judgments)
    # ====================================================================
    error_events_list = [e for e in events if e.get("type") == "error"]
    error_count = len(error_events_list)
    error_summaries = [
        {"id": e.get("id"), "summary": e.get("summary", "")} for e in error_events_list[:5]
    ]

    approval_events = sum(1 for e in events if e.get("type") == "approval_required")

    # Token efficiency: total_tokens / total_reasoning_steps
    total_tokens = summary.get("total_tokens", 0)
    token_efficiency: float | None = None
    if total_reasoning_steps > 0 and total_tokens > 0:
        token_efficiency = round(total_tokens / total_reasoning_steps, 1)

    signals = {
        "error_count": error_count,
        "error_events": error_summaries,
        "approval_events": approval_events,
        "token_efficiency": token_efficiency,
    }

    # ====================================================================
    # Build result
    # ====================================================================
    resolved_session_id = transcript_data.get("session_id", session_id)

    result = {
        "session_id": resolved_session_id,
        "audit": {
            "completeness": completeness_checks,
            "coverage": coverage,
            "signals": signals,
        },
        "summary": summary,
    }

    indent = None if format == "json" else 2
    return json.dumps(result, indent=indent, default=str)


def _check_reasoning_chain(events: list[dict]) -> bool:
    """Check if events contain at least one thought -> action -> observation sequence.

    Args:
        events: List of event dicts from transcript.

    Returns:
        True if at least one reasoning chain is found.
    """
    types = [e.get("type", "") for e in events]
    for i in range(len(types) - 2):
        if types[i] == "thought" and types[i + 1] == "action" and types[i + 2] == "observation":
            return True
    return False

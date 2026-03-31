# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Chat tool handler -- send messages to MEHO and consume SSE responses."""

import json
import logging
import uuid
from typing import Optional

import httpx

from ..config import get_meho_api_url, get_auth_headers

logger = logging.getLogger(__name__)


async def send_meho_message(
    message: str,
    session_id: Optional[str] = None,
    timeout: int = 120,
) -> str:
    """
    Send a diagnostic query to MEHO and wait for the complete response.

    Calls POST /api/chat/stream, consumes the full SSE event stream,
    and returns a structured JSON result with the final answer, session ID
    for follow-up queries, and execution statistics.

    Args:
        message: The diagnostic query to send to MEHO.
        session_id: Optional session ID to continue an existing conversation.
                    Omit for a fresh session.
        timeout: Maximum seconds to wait for MEHO to complete. Defaults to 120.

    Returns:
        JSON string with response, session_id, events_summary, and stats.
    """
    base_url = get_meho_api_url()
    stream_url = f"{base_url}/api/chat/stream"
    headers = get_auth_headers()

    # Pre-generate session_id for fresh queries so we can return it
    # and the caller can chain follow-up queries
    if not session_id:
        session_id = str(uuid.uuid4())

    body: dict = {"message": message, "session_id": session_id}

    # Track events as we consume the stream
    final_answer: Optional[str] = None
    error_message: Optional[str] = None
    thought_count = 0
    action_count = 0
    observation_count = 0
    error_count = 0
    approval_required = False
    partial_content_parts: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            async with client.stream(
                "POST",
                stream_url,
                json=body,
                headers=headers,
            ) as response:
                response.raise_for_status()

                # Consume SSE stream line by line
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue

                    raw_data = line[6:]  # Strip "data: " prefix
                    try:
                        event = json.loads(raw_data)
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse SSE data: {raw_data[:100]}")
                        continue

                    event_type = event.get("type", "")

                    if event_type == "thought":
                        thought_count += 1
                        content = event.get("content", "")
                        if content:
                            partial_content_parts.append(content)

                    elif event_type == "action":
                        action_count += 1

                    elif event_type == "observation":
                        observation_count += 1

                    elif event_type == "final_answer":
                        final_answer = event.get("content", "")

                    elif event_type == "error":
                        error_count += 1
                        error_message = event.get("message", event.get("content", "Unknown error"))

                    elif event_type == "approval_required":
                        approval_required = True

                    elif event_type == "done":
                        break

        # Build events summary
        events_summary = {
            "thoughts": thought_count,
            "actions": action_count,
            "observations": observation_count,
            "errors": error_count,
            "approval_required": approval_required,
        }

        # Attempt to retrieve execution stats from observability API
        stats = None
        if session_id:
            stats = await _fetch_session_stats(base_url, headers, session_id)

        # Determine response text
        response_text = final_answer
        if response_text is None and error_message:
            response_text = f"Error: {error_message}"
        elif response_text is None:
            response_text = " ".join(partial_content_parts) if partial_content_parts else ""

        result = {
            "response": response_text,
            "session_id": session_id,
            "events_summary": events_summary,
            "stats": stats,
        }

        return json.dumps(result, indent=2)

    except httpx.TimeoutException:
        # Assemble any partial content captured before timeout
        partial = final_answer or (" ".join(partial_content_parts) if partial_content_parts else "")
        error_result = {
            "error": f"Timeout after {timeout}s",
            "partial_response": partial,
        }
        return json.dumps(error_result, indent=2)

    except httpx.ConnectError as exc:
        error_result = {
            "error": f"Connection failed: {exc}",
        }
        return json.dumps(error_result, indent=2)

    except httpx.HTTPStatusError as exc:
        error_result = {
            "error": f"HTTP {exc.response.status_code}: {exc.response.text[:500]}",
        }
        return json.dumps(error_result, indent=2)

    except Exception as exc:
        error_result = {
            "error": f"Unexpected error: {exc}",
        }
        return json.dumps(error_result, indent=2)


async def _fetch_session_stats(
    base_url: str,
    headers: dict,
    session_id: str,
) -> Optional[dict]:
    """
    Fetch execution statistics from the observability API.

    Returns None if the call fails (non-critical -- stats are supplementary).
    """
    summary_url = f"{base_url}/api/observability/sessions/{session_id}/summary"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(summary_url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            return {
                "total_tokens": data.get("total_tokens"),
                "total_tool_calls": data.get("total_tool_calls"),
                "total_llm_calls": data.get("total_llm_calls"),
                "total_operation_calls": data.get("total_operation_calls"),
                "duration_ms": data.get("total_duration_ms"),
                "connector_ids": data.get("connector_ids", []),
            }
    except Exception as exc:
        logger.debug(f"Failed to fetch session stats for {session_id}: {exc}")
        return None

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Scrubber utilities for removing sensitive data from transcripts.

This module provides utilities for:
1. Scrubbing credentials (passwords, tokens, API keys) from stored data
2. Truncating large payloads to avoid storage bloat
3. Sanitizing HTTP headers and bodies

IMPORTANT: All data passed to TranscriptCollector should go through these
utilities before storage to prevent credential leakage.

Example:
    >>> from meho_app.modules.agents.persistence.scrubber import (
    ...     scrub_sensitive_data,
    ...     truncate_payload,
    ...     sanitize_headers,
    ... )
    >>> headers = {"Authorization": "Bearer secret123", "Content-Type": "application/json"}
    >>> sanitized = sanitize_headers(headers)
    >>> # {"Authorization": "[REDACTED]", "Content-Type": "application/json"}
"""

from __future__ import annotations

from typing import Any

from meho_app.modules.agents.persistence.scrubber_patterns import ScrubPatterns

# Re-export constants for backward compatibility
MAX_PAYLOAD_SIZE = ScrubPatterns.MAX_PAYLOAD_SIZE
MAX_RESULT_SAMPLE_ROWS = ScrubPatterns.MAX_RESULT_SAMPLE_ROWS
SENSITIVE_KEYS = ScrubPatterns.SENSITIVE_KEYS
SENSITIVE_HEADERS = ScrubPatterns.SENSITIVE_HEADERS
SENSITIVE_PATTERNS = ScrubPatterns.SENSITIVE_PATTERNS
REDACTED = ScrubPatterns.REDACTED


def is_sensitive_key(key: str) -> bool:
    """Check if a key name indicates sensitive data.

    Args:
        key: The key name to check.

    Returns:
        True if the key appears to hold sensitive data.
    """
    return ScrubPatterns.is_sensitive_key(key)


def scrub_value(value: Any) -> Any:
    """Scrub sensitive patterns from a value.

    Args:
        value: The value to scrub.

    Returns:
        The value with sensitive patterns redacted.
    """
    if not isinstance(value, str):
        return value

    return ScrubPatterns.scrub_patterns_from_value(value)


def scrub_sensitive_data(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively remove sensitive data from a dictionary.

    This function:
    1. Redacts values for keys that appear sensitive
    2. Recursively processes nested dicts and lists
    3. Applies pattern matching to string values

    Args:
        data: The dictionary to scrub.

    Returns:
        A new dictionary with sensitive data redacted.
    """
    if not isinstance(data, dict):
        return data

    result: dict[str, Any] = {}

    for key, value in data.items():
        # Check if the key indicates sensitive data
        if is_sensitive_key(key):
            result[key] = REDACTED
        elif isinstance(value, dict):
            result[key] = scrub_sensitive_data(value)
        elif isinstance(value, list):
            result[key] = [
                scrub_sensitive_data(item)
                if isinstance(item, dict)
                else scrub_value(item)
                if isinstance(item, str)
                else item
                for item in value
            ]
        elif isinstance(value, str):
            result[key] = scrub_value(value)
        else:
            result[key] = value

    return result


def sanitize_headers(headers: dict[str, str] | None) -> dict[str, str] | None:
    """Sanitize HTTP headers by redacting sensitive values.

    Args:
        headers: HTTP headers dictionary.

    Returns:
        Headers with sensitive values redacted.
    """
    if headers is None:
        return None

    result: dict[str, str] = {}

    for key, value in headers.items():
        if ScrubPatterns.is_sensitive_header(key):
            result[key] = REDACTED
        else:
            result[key] = value

    return result


def truncate_payload(
    data: str,
    max_size: int = MAX_PAYLOAD_SIZE,
) -> str:
    """Truncate a large payload with a marker indicating truncation.

    Args:
        data: The payload string to truncate.
        max_size: Maximum size in bytes before truncation.

    Returns:
        The payload, truncated if necessary with a marker.
    """
    if len(data) <= max_size:
        return data

    original_size = len(data)
    truncated = data[:max_size]

    return f"{truncated}\n\n[TRUNCATED: Original size: {original_size} bytes]"


def truncate_result_sample(
    results: list[dict[str, Any]] | None,
    max_rows: int = MAX_RESULT_SAMPLE_ROWS,
) -> list[dict[str, Any]] | None:
    """Truncate SQL/API result samples to a reasonable size.

    Args:
        results: List of result rows.
        max_rows: Maximum number of rows to keep.

    Returns:
        Truncated results with a note if truncated.
    """
    if results is None:
        return None

    if len(results) <= max_rows:
        return results

    # Take first max_rows and add a note
    truncated = results[:max_rows]
    truncated.append({"_note": f"[TRUNCATED: Showing {max_rows} of {len(results)} rows]"})

    return truncated


def sanitize_http_body(
    body: str | None,
    max_size: int = MAX_PAYLOAD_SIZE,
) -> str | None:
    """Sanitize and truncate an HTTP request/response body.

    Args:
        body: The HTTP body to sanitize.
        max_size: Maximum size in bytes.

    Returns:
        Sanitized and truncated body.
    """
    if body is None:
        return None

    # First, scrub sensitive patterns
    scrubbed = scrub_value(body)

    # Then truncate if necessary
    return truncate_payload(scrubbed, max_size)


def sanitize_tool_output(
    output: Any,
    max_size: int = MAX_PAYLOAD_SIZE,
) -> Any:
    """Sanitize tool output for storage.

    Args:
        output: The tool output to sanitize.
        max_size: Maximum size for string outputs.

    Returns:
        Sanitized output.
    """
    if output is None:
        return None

    if isinstance(output, str):
        return sanitize_http_body(output, max_size)

    if isinstance(output, dict):
        return scrub_sensitive_data(output)

    if isinstance(output, list):
        # Handle list of dicts (common for API results)
        if all(isinstance(item, dict) for item in output):
            scrubbed = [scrub_sensitive_data(item) for item in output]
            return truncate_result_sample(scrubbed)
        return output

    # For other types, convert to string and truncate
    output_str = str(output)
    if len(output_str) > max_size:
        return truncate_payload(output_str, max_size)

    return output


def create_sanitized_event_details(
    llm_prompt: str | None = None,
    llm_messages: list[dict] | None = None,
    llm_response: str | None = None,
    http_headers: dict[str, str] | None = None,
    http_request_body: str | None = None,
    http_response_body: str | None = None,
    tool_input: dict | None = None,
    tool_output: Any = None,
    sql_parameters: dict | None = None,
    sql_result_sample: list[dict] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create sanitized event details for storage.

    This is a convenience function that sanitizes all sensitive fields
    before they are stored in the transcript.

    Args:
        llm_prompt: LLM system prompt.
        llm_messages: LLM conversation messages.
        llm_response: LLM response.
        http_headers: HTTP headers.
        http_request_body: HTTP request body.
        http_response_body: HTTP response body.
        tool_input: Tool input parameters.
        tool_output: Tool output.
        sql_parameters: SQL query parameters.
        sql_result_sample: SQL result sample.
        **kwargs: Additional fields to include as-is.

    Returns:
        Dictionary of sanitized event details.
    """
    from meho_app.modules.agents.base.detailed_events import EventDetails

    details = EventDetails(
        llm_prompt=truncate_payload(llm_prompt) if llm_prompt else None,
        llm_messages=[scrub_sensitive_data(m) for m in llm_messages] if llm_messages else None,
        llm_response=truncate_payload(llm_response) if llm_response else None,
        http_headers=sanitize_headers(http_headers),
        http_request_body=sanitize_http_body(http_request_body),
        http_response_body=sanitize_http_body(http_response_body),
        tool_input=scrub_sensitive_data(tool_input) if tool_input else None,
        tool_output=sanitize_tool_output(tool_output),
        sql_parameters=scrub_sensitive_data(sql_parameters) if sql_parameters else None,
        sql_result_sample=truncate_result_sample(
            [scrub_sensitive_data(r) for r in sql_result_sample]
        )
        if sql_result_sample
        else None,
        **{k: v for k, v in kwargs.items() if v is not None},
    )

    return details.to_dict()

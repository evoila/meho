# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tracing utilities for MEHO agent handlers.

Provides consistent OTEL span creation with rich context for debugging:
- Input/output logging with configurable truncation
- Data sanitization (remove sensitive fields)
- Timing and error tracking
- Structured attributes for observability

Environment Variables:
    OTEL_TRACE_LEVEL: "full", "truncated", or "summary" (default: full)
    OTEL_MAX_BODY_SIZE: Max bytes to log for large payloads (default: 10240)
"""

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from meho_app.core.otel import get_logger, span


class TracedSpan:
    """Wrapper for an OTEL span to allow setting output/errors after execution."""

    def __init__(self, otel_span: Any) -> None:
        self._span = otel_span
        self._output: Any = None
        self._error: str | None = None
        self._success: bool = True

    def set_output(self, output: Any) -> None:
        """Set the tool output for logging."""
        self._output = output
        self._success = True
        if self._span and hasattr(self._span, "set_attribute"):
            self._span.set_attribute("output", format_for_logging(output))
            self._span.set_attribute("success", True)

    def set_error(self, error: str) -> None:
        """Set an error message."""
        self._error = error
        self._success = False
        if self._span and hasattr(self._span, "set_attribute"):
            self._span.set_attribute("error", error[:1000])
            self._span.set_attribute("success", False)

    def add_attribute(self, key: str, value: Any) -> None:
        """Add an additional attribute."""
        if self._span and hasattr(self._span, "set_attribute"):
            self._span.set_attribute(key, format_for_logging(value))


logger = get_logger(__name__)

# Configuration from environment
TRACE_LEVEL = os.getenv("OTEL_TRACE_LEVEL", "full").lower()
MAX_BODY_SIZE = int(os.getenv("OTEL_MAX_BODY_SIZE", "10240"))

# Sensitive fields to redact
SENSITIVE_FIELDS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "credential",
    "private_key",
    "access_token",
    "refresh_token",
    "session_id",
    "cookie",
    "x-api-key",
}


def _should_log_full() -> bool:
    """Check if full logging is enabled."""
    return TRACE_LEVEL == "full"


def _should_log_truncated() -> bool:
    """Check if truncated logging is enabled."""
    return TRACE_LEVEL in ("full", "truncated")


def sanitize_data(data: Any, max_depth: int = 10) -> Any:
    """
    Sanitize data by redacting sensitive fields.

    Args:
        data: Any data structure (dict, list, str, etc.)
        max_depth: Maximum recursion depth to prevent infinite loops

    Returns:
        Sanitized copy of the data with sensitive fields redacted
    """
    if max_depth <= 0:
        return "[MAX_DEPTH_EXCEEDED]"

    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            key_lower = str(key).lower()
            if any(sensitive in key_lower for sensitive in SENSITIVE_FIELDS):
                result[key] = "[REDACTED]"
            else:
                result[key] = sanitize_data(value, max_depth - 1)
        return result

    elif isinstance(data, list):
        return [sanitize_data(item, max_depth - 1) for item in data]

    elif isinstance(data, str):
        # Check if string looks like a credential
        if len(data) > 20 and any(
            pattern in data.lower() for pattern in ["bearer ", "basic ", "pylf_"]
        ):
            return "[REDACTED_TOKEN]"
        return data

    else:
        return data


def truncate_data(data: Any, max_size: int = MAX_BODY_SIZE) -> tuple[Any, bool]:
    """
    Truncate large data structures for logging.

    Args:
        data: Any data structure
        max_size: Maximum size in bytes

    Returns:
        Tuple of (truncated_data, was_truncated)
    """
    try:
        json_str = json.dumps(data, default=str)
        if len(json_str) <= max_size:
            return data, False

        # For lists, return first N items
        if isinstance(data, list):
            truncated = data[:10]  # First 10 items
            return {
                "_truncated": True,
                "_total_items": len(data),
                "_shown_items": len(truncated),
                "data": truncated,
            }, True

        # For dicts, truncate string values
        elif isinstance(data, dict):
            truncated_dict: dict[str, Any] = {}
            for key, value in data.items():
                if isinstance(value, str) and len(value) > 500:
                    truncated_dict[key] = (
                        value[:500] + f"... [truncated, total: {len(value)} chars]"
                    )
                elif isinstance(value, (list, dict)):
                    truncated_dict[key], _ = truncate_data(value, max_size // 2)
                else:
                    truncated_dict[key] = value
            return truncated_dict, True

        # For strings, truncate directly
        elif isinstance(data, str):
            return data[:max_size] + f"... [truncated, total: {len(data)} chars]", True

        return data, False

    except Exception:
        return str(data)[:max_size], True


def _make_json_serializable(
    data: Any, max_depth: int = 10
) -> Any:  # NOSONAR (cognitive complexity)
    """
    Recursively convert data to JSON-serializable types.

    Handles: UUID, datetime, bytes, Pydantic models, etc.
    """
    import uuid
    from datetime import date, datetime

    if max_depth <= 0:
        return str(data)

    if data is None:
        return None

    if isinstance(data, (str, int, float, bool)):
        return data

    if isinstance(data, uuid.UUID):
        return str(data)

    if isinstance(data, (datetime, date)):
        return data.isoformat()

    if isinstance(data, bytes):
        try:
            return data.decode("utf-8")[:500]
        except UnicodeDecodeError:
            return f"[bytes: {len(data)} bytes]"

    if isinstance(data, dict):
        return {str(k): _make_json_serializable(v, max_depth - 1) for k, v in data.items()}

    if isinstance(data, (list, tuple)):
        return [_make_json_serializable(item, max_depth - 1) for item in data]

    # Handle Pydantic models
    if hasattr(data, "model_dump"):
        return _make_json_serializable(data.model_dump(), max_depth - 1)
    if hasattr(data, "dict"):
        return _make_json_serializable(data.dict(), max_depth - 1)

    # Fallback: convert to string
    return str(data)


def format_for_logging(data: Any) -> str:  # NOSONAR (cognitive complexity)
    """
    Format data for OTEL span attributes.

    CRITICAL: OpenTelemetry span attributes only support primitive types:
    - strings, integers, floats, booleans
    - arrays of the above primitives

    Nested dicts/objects are NOT supported and cause "Failed to render details panel" errors.

    This function ALWAYS returns a string to ensure compatibility.

    Args:
        data: Any data to log

    Returns:
        String representation suitable for span attributes
    """
    if data is None:
        return ""

    # Already a primitive - return as string
    if isinstance(data, bool):
        return str(data).lower()
    if isinstance(data, (int, float)):
        return str(data)
    if isinstance(data, str):
        # Truncate long strings
        if len(data) > MAX_BODY_SIZE:
            return data[:MAX_BODY_SIZE] + f"... [{len(data)} chars total]"
        return data

    # Make JSON-serializable first
    serializable = _make_json_serializable(data)

    # Sanitize sensitive data
    sanitized = sanitize_data(serializable)

    # Truncate if needed
    if TRACE_LEVEL == "truncated":
        sanitized, _ = truncate_data(sanitized)
    elif TRACE_LEVEL == "summary":
        # Summary mode: just type and size
        if isinstance(sanitized, list):
            return f"[list: {len(sanitized)} items]"
        elif isinstance(sanitized, dict):
            keys = list(sanitized.keys())[:5]
            return f"[dict: {len(sanitized)} keys - {', '.join(str(k) for k in keys)}]"
        else:
            return f"[{type(data).__name__}]"

    # Convert to JSON string - this is the key fix!
    try:
        json_str = json.dumps(sanitized, default=str, ensure_ascii=False, indent=None)
        if len(json_str) > MAX_BODY_SIZE:
            return json_str[:MAX_BODY_SIZE] + f"... [{len(json_str)} chars total]"
        return json_str
    except Exception:
        result = str(sanitized)
        if len(result) > MAX_BODY_SIZE:
            return result[:MAX_BODY_SIZE] + "..."
        return result


@asynccontextmanager
async def traced_tool_call(
    tool_name: str,
    args: dict[str, Any],
    *,
    connector_id: str | None = None,
    connector_name: str | None = None,
    connector_type: str | None = None,
    operation_id: str | None = None,
    user_id: str | None = None,
    tenant_id: str | None = None,
    session_id: str | None = None,
    extra_attrs: dict[str, Any] | None = None,
) -> AsyncIterator[TracedSpan]:
    """
    Context manager for tracing tool calls with rich context.

    Usage:
        async with traced_tool_call("search_operations", args, connector_id="abc") as traced:
            result = await do_work()
            traced.set_output(result)

    Args:
        tool_name: Name of the tool being called
        args: Tool input arguments
        connector_id: Optional connector ID
        connector_name: Optional connector name
        connector_type: Optional connector type
        operation_id: Optional operation ID
        user_id: User ID for context
        tenant_id: Tenant ID for context
        session_id: Session ID for context
        extra_attrs: Additional attributes to log

    Yields:
        TracedSpan wrapper for adding output and errors
    """
    span_name = f"meho.tool.{tool_name}"
    start_time = time.perf_counter()

    # Build attributes - ensure all values are serializable
    attrs: dict[str, Any] = {
        "tool": tool_name,
        "input": format_for_logging(args),
    }

    if connector_id:
        attrs["connector_id"] = str(connector_id)
    if connector_name:
        attrs["connector_name"] = str(connector_name)
    if connector_type:
        attrs["connector_type"] = str(connector_type)
    if operation_id:
        attrs["operation_id"] = str(operation_id)
    if user_id:
        attrs["user_id"] = str(user_id)
    if tenant_id:
        attrs["tenant_id"] = str(tenant_id)
    if session_id:
        attrs["session_id"] = str(session_id)
    if extra_attrs:
        # Ensure extra_attrs values are strings (primitives) for OpenTelemetry
        for key, value in extra_attrs.items():
            if value is not None:
                attrs[key] = format_for_logging(value)

    with span(span_name, **attrs) as otel_span:
        traced = TracedSpan(otel_span)
        try:
            yield traced
        except Exception as e:
            traced.set_error(str(e))
            raise
        finally:
            duration_ms = (time.perf_counter() - start_time) * 1000
            otel_span.set_attribute("duration_ms", round(duration_ms, 2))
            otel_span.set_attribute("success", traced._success)


def trace_llm_interaction(  # NOSONAR (cognitive complexity)
    *,
    step: int,
    system_prompt: str,
    user_message: str,
    raw_response: str,
    parsed_thought: str | None = None,
    parsed_action: str | None = None,
    parsed_action_input: str | None = None,
    parsed_final_answer: str | None = None,
    token_usage: dict[str, int] | None = None,
    user_id: str | None = None,
    tenant_id: str | None = None,
    session_id: str | None = None,
    duration_ms: float | None = None,
) -> None:
    """
    Log a complete LLM interaction with all context.

    This creates a detailed log entry for a single reasoning step,
    capturing the full prompt, response, and parsed output.

    Args:
        step: Step number in the reasoning loop
        system_prompt: Full system prompt sent to LLM
        user_message: User's original message
        raw_response: Raw LLM response text
        parsed_thought: Extracted Thought from response
        parsed_action: Extracted Action from response
        parsed_action_input: Extracted Action Input from response
        parsed_final_answer: Extracted Final Answer from response
        token_usage: Token counts (prompt, completion, total)
        user_id: User ID for context
        tenant_id: Tenant ID for context
        session_id: Session ID for context
        duration_ms: LLM call duration in milliseconds
    """
    # Format prompts based on trace level
    if _should_log_full():
        prompt_data = system_prompt
        response_data = raw_response
    elif _should_log_truncated():
        prompt_data = system_prompt[:5000] + ("..." if len(system_prompt) > 5000 else "")
        response_data = raw_response[:2000] + ("..." if len(raw_response) > 2000 else "")
    else:
        prompt_data = f"[{len(system_prompt)} chars]"
        response_data = f"[{len(raw_response)} chars]"

    attrs: dict[str, Any] = {
        "step": step,
        "system_prompt": prompt_data,
        "system_prompt_length": len(system_prompt),
        "user_message": user_message[:500] if user_message else None,
        "raw_response": response_data,
        "raw_response_length": len(raw_response),
    }

    # Add parsed fields
    if parsed_thought:
        attrs["parsed_thought"] = (
            parsed_thought[:1000] if len(parsed_thought) > 1000 else parsed_thought
        )
    if parsed_action:
        attrs["parsed_action"] = parsed_action
    if parsed_action_input:
        attrs["parsed_action_input"] = (
            parsed_action_input[:500] if len(parsed_action_input) > 500 else parsed_action_input
        )
    if parsed_final_answer:
        attrs["parsed_final_answer"] = (
            parsed_final_answer[:2000] if len(parsed_final_answer) > 2000 else parsed_final_answer
        )
        attrs["has_final_answer"] = True

    # Add token usage
    if token_usage:
        attrs["tokens_prompt"] = token_usage.get("prompt", 0)
        attrs["tokens_completion"] = token_usage.get("completion", 0)
        attrs["tokens_total"] = token_usage.get("total", 0)

    # Add context - ensure all IDs are strings
    if user_id:
        attrs["user_id"] = str(user_id)
    if tenant_id:
        attrs["tenant_id"] = str(tenant_id)
    if session_id:
        attrs["session_id"] = str(session_id)
    if duration_ms:
        attrs["duration_ms"] = round(duration_ms, 2)

    # Remove step from attrs since we pass it explicitly in the format string
    attrs.pop("step", None)

    action_or_answer = parsed_action or ("Final Answer" if parsed_final_answer else "unknown")
    logger.info(
        f"LLM reasoning step {step}: {action_or_answer}",
        step_num=step,
        action_or_answer=action_or_answer,
        **attrs,
    )


def trace_operation_call(  # NOSONAR (cognitive complexity)
    *,
    method: str,
    url: str,
    request_headers: dict[str, str] | None = None,
    request_body: Any | None = None,
    response_status: int | None = None,
    response_headers: dict[str, str] | None = None,
    response_body: Any | None = None,
    duration_ms: float | None = None,
    error: str | None = None,
    connector_id: str | None = None,
    connector_name: str | None = None,
) -> None:
    """
    Log an operation call (REST/SOAP/VMware) with request/response details.

    Args:
        method: HTTP method (GET, POST, etc.)
        url: Full URL
        request_headers: Request headers (will be sanitized)
        request_body: Request body (will be sanitized)
        response_status: HTTP status code
        response_headers: Response headers
        response_body: Response body (will be truncated if large)
        duration_ms: Request duration
        error: Error message if failed
        connector_id: Associated connector ID
        connector_name: Associated connector name
    """
    attrs: dict[str, Any] = {
        "method": method,
        "url": url,
    }

    # Sanitize and add headers
    if request_headers:
        attrs["request_headers"] = sanitize_data(request_headers)

    # Add request body
    if request_body is not None:
        attrs["request_body"] = format_for_logging(request_body)

    # Add response info
    if response_status is not None:
        attrs["response_status"] = response_status

    if response_headers:
        attrs["response_headers"] = sanitize_data(response_headers)

    if response_body is not None:
        attrs["response_body"] = format_for_logging(response_body)

    if duration_ms is not None:
        attrs["duration_ms"] = round(duration_ms, 2)

    if error:
        attrs["error"] = error
        attrs["success"] = False
    else:
        attrs["success"] = response_status is not None and response_status < 400

    if connector_id:
        attrs["connector_id"] = str(connector_id)
    if connector_name:
        attrs["connector_name"] = str(connector_name)

    is_error = error or (response_status and response_status >= 400)
    log_func = logger.error if is_error else logger.info

    truncated_url = url[:100] + ("..." if len(url) > 100 else "")
    status = response_status or "error"
    log_func(
        f"HTTP {method} {truncated_url} -> {status}",
        method=method,
        url=truncated_url,
        status=status,
        **attrs,
    )


def trace_sql_query(
    *,
    operation: str,
    sql: str,
    parameters: Any | None = None,
    row_count: int | None = None,
    duration_ms: float | None = None,
    error: str | None = None,
) -> None:
    """
    Log a SQL query with parameters and results.

    Args:
        operation: SQL operation type (SELECT, INSERT, UPDATE, etc.)
        sql: SQL statement
        parameters: Query parameters
        row_count: Number of rows affected/returned
        duration_ms: Query duration
        error: Error message if failed
    """
    attrs: dict[str, Any] = {
        # Note: operation is passed explicitly, not via attrs
        "sql": (sql if _should_log_full() else sql[:500] + ("..." if len(sql) > 500 else "")),
        "sql_length": len(sql),
    }

    if parameters is not None:
        attrs["parameters"] = format_for_logging(parameters)

    if row_count is not None:
        attrs["row_count"] = row_count

    if duration_ms is not None:
        attrs["duration_ms"] = round(duration_ms, 2)

    if error:
        attrs["error"] = error
        attrs["success"] = False
    else:
        attrs["success"] = True

    rows = row_count if row_count is not None else "?"
    log_func = logger.error if error else logger.info
    log_func(f"SQL {operation}: {rows} rows", operation=operation, rows=rows, **attrs)


def trace_topology_lookup(
    *,
    query: str,
    entities_extracted: list,
    entities_found: list,
    context_injected: str | None = None,
    duration_ms: float | None = None,
    tenant_id: str | None = None,
) -> None:
    """
    Log a topology lookup operation.

    Args:
        query: User's query being analyzed
        entities_extracted: Entity references extracted from query
        entities_found: Entities found in topology database
        context_injected: Context string injected into state
        duration_ms: Lookup duration
        tenant_id: Tenant ID
    """
    # Ensure entities are serializable (convert to simple strings/dicts)
    extracted_simple = [str(e) if not isinstance(e, (str, dict)) else e for e in entities_extracted]
    found_simple = [e.get("name") if isinstance(e, dict) else str(e) for e in entities_found]

    attrs: dict[str, Any] = {
        "query": query[:200] if query else None,
        "entities_extracted": format_for_logging(extracted_simple),
        "entities_extracted_count": len(entities_extracted),
        "entities_found": found_simple,
        "entities_found_count": len(entities_found),
    }

    if context_injected:
        # context_injected can be a bool (True/False) or a string (the actual context)
        if isinstance(context_injected, str):
            attrs["context_injected"] = (
                context_injected[:1000] if _should_log_truncated() else context_injected
            )
            attrs["context_length"] = len(context_injected)
        else:
            attrs["context_injected"] = context_injected  # Just pass the bool

    if duration_ms is not None:
        attrs["duration_ms"] = round(duration_ms, 2)

    if tenant_id:
        attrs["tenant_id"] = str(tenant_id)

    logger.info(
        f"Topology lookup: {len(entities_extracted)} extracted, {len(entities_found)} found",
        extracted=len(entities_extracted),
        found=len(entities_found),
        **attrs,
    )

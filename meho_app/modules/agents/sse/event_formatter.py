# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Event Formatter - Creates EventDetails and generates summaries.

This module extracts the event formatting logic from EventEmitter to provide
cleaner separation of concerns. The formatter handles:
1. Creating EventDetails with appropriate fields
2. Generating default summaries
3. Creating TokenUsage objects
4. Sanitization and truncation

Example:
    >>> from meho_app.modules.agents.sse.event_formatter import EventFormatter
    >>> details = EventFormatter.format_thought_details(
    ...     prompt="System prompt",
    ...     response="LLM response",
    ...     prompt_tokens=100,
    ...     completion_tokens=50,
    ... )
"""

from __future__ import annotations

from typing import Any

from meho_app.modules.agents.base.event_builders import estimate_cost
from meho_app.modules.agents.base.event_types import (
    DetailedEvent,
    EventDetails,
    TokenUsage,
)


class EventFormatter:
    """Formats events for transcript persistence.

    This class provides static methods for creating EventDetails objects
    and generating appropriate summaries for different event types.

    Claude ThinkingPart handling:
    - ThinkingPart content from Claude's adaptive thinking is routed through
      the standard 'thought' event pipeline via EventEmitter.thinking_part()
    - format_thought_details() works identically for both regular thoughts
      and ThinkingPart-sourced content (the content is already extracted
      by the time it reaches the formatter)
    - Token usage includes thinking tokens when reported by Anthropic API
    - Cost estimation in create_token_usage() uses Anthropic pricing from
      event_builders.MODEL_COSTS
    See: RESEARCH.md Pitfall 4, CONTEXT.md "stream everything" decision.
    """

    @staticmethod
    def create_token_usage(
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str | None = None,
    ) -> TokenUsage | None:
        """Create a TokenUsage object if tokens are provided.

        Args:
            prompt_tokens: Number of prompt tokens.
            completion_tokens: Number of completion tokens.
            model: Model name for cost estimation.

        Returns:
            TokenUsage object or None if no tokens.
        """
        if not prompt_tokens and not completion_tokens:
            return None

        total_tokens = prompt_tokens + completion_tokens
        cost = estimate_cost(model or "", prompt_tokens, completion_tokens)
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=cost,
        )

    @staticmethod
    def format_thought_details(
        prompt: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        response: str | None = None,
        parsed: dict[str, Any] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str | None = None,
        duration_ms: float | None = None,
    ) -> EventDetails:
        """Format details for a thought/LLM event.

        Args:
            prompt: Full system prompt.
            messages: Conversation history.
            response: Raw LLM response.
            parsed: Parsed response (thought/action/final_answer).
            prompt_tokens: Number of prompt tokens.
            completion_tokens: Number of completion tokens.
            model: Model name.
            duration_ms: LLM call duration.

        Returns:
            EventDetails populated with LLM fields.
        """
        token_usage = EventFormatter.create_token_usage(prompt_tokens, completion_tokens, model)

        return EventDetails(
            llm_prompt=prompt,
            llm_messages=messages,
            llm_response=response,
            llm_parsed=parsed,
            token_usage=token_usage,
            llm_duration_ms=duration_ms,
            model=model,
        )

    @staticmethod
    def format_action_details(
        tool: str,
        args: dict[str, Any],
    ) -> EventDetails:
        """Format details for an action event (tool being called).

        Args:
            tool: Tool name.
            args: Tool arguments.

        Returns:
            EventDetails populated with tool input fields.
        """
        return EventDetails(
            tool_name=tool,
            tool_input=args,
        )

    @staticmethod
    def format_observation_details(
        tool: str,
        result: Any,
        duration_ms: float | None = None,
        error: str | None = None,
    ) -> EventDetails:
        """Format details for an observation event (tool result).

        Args:
            tool: Tool name.
            result: Tool output (will be sanitized).
            duration_ms: Tool execution duration.
            error: Error message if tool failed.

        Returns:
            EventDetails populated with tool output fields.
        """
        from meho_app.modules.agents.persistence.scrubber import sanitize_tool_output

        sanitized_output = sanitize_tool_output(result)

        return EventDetails(
            tool_name=tool,
            tool_output=sanitized_output,
            tool_duration_ms=duration_ms,
            tool_error=error,
        )

    @staticmethod
    def format_operation_call_details(
        method: str,
        url: str,
        status_code: int | None = None,
        request_body: str | None = None,
        response_body: str | None = None,
        headers: dict[str, str] | None = None,
        duration_ms: float | None = None,
    ) -> EventDetails:
        """Format details for an operation call event.

        Args:
            method: HTTP method.
            url: Request URL.
            status_code: HTTP status code.
            request_body: Request body.
            response_body: Response body.
            headers: Request headers.
            duration_ms: Request duration.

        Returns:
            EventDetails populated with operation call fields.
        """
        from meho_app.modules.agents.persistence.scrubber import (
            sanitize_headers,
            sanitize_http_body,
        )

        return EventDetails(
            http_method=method,
            http_url=url,
            http_status_code=status_code,
            http_headers=sanitize_headers(headers),
            http_request_body=sanitize_http_body(request_body),
            http_response_body=sanitize_http_body(response_body),
            http_duration_ms=duration_ms,
        )

    @staticmethod
    def format_tool_call_details(
        tool: str,
        args: dict[str, Any],
        result: Any,
        duration_ms: float | None = None,
        error: str | None = None,
    ) -> EventDetails:
        """Format details for a consolidated tool call event.

        Args:
            tool: Tool name.
            args: Tool arguments (input).
            result: Tool execution result (output).
            duration_ms: Tool execution duration.
            error: Error message if tool failed.

        Returns:
            EventDetails populated with both input and output fields.
        """
        from meho_app.modules.agents.persistence.scrubber import sanitize_tool_output

        sanitized_output = sanitize_tool_output(result) if result is not None else None

        return EventDetails(
            tool_name=tool,
            tool_input=args,
            tool_output=sanitized_output,
            tool_duration_ms=duration_ms,
            tool_error=error,
        )

    @staticmethod
    def format_sql_query_details(
        query: str,
        parameters: dict[str, Any] | None = None,
        row_count: int | None = None,
        result_sample: list[dict[str, Any]] | None = None,
        duration_ms: float | None = None,
    ) -> EventDetails:
        """Format details for a SQL query event.

        Args:
            query: SQL query string.
            parameters: Query parameters.
            row_count: Number of rows returned/affected.
            result_sample: First few rows of results.
            duration_ms: Query duration.

        Returns:
            EventDetails populated with SQL fields.
        """
        from meho_app.modules.agents.persistence.scrubber import (
            scrub_sensitive_data,
            truncate_result_sample,
        )

        return EventDetails(
            sql_query=query,
            sql_parameters=scrub_sensitive_data(parameters) if parameters else None,
            sql_row_count=row_count,
            sql_result_sample=truncate_result_sample(result_sample),
            sql_duration_ms=duration_ms,
        )

    @staticmethod
    def generate_summary(
        event_type: str,
        *,
        tool: str | None = None,
        method: str | None = None,
        url: str | None = None,
        status_code: int | None = None,
        row_count: int | None = None,
        duration_ms: float | None = None,
        custom_summary: str | None = None,
    ) -> str:
        """Generate a default summary for an event.

        Args:
            event_type: Type of event (action, observation, operation_call, etc.).
            tool: Tool name (for tool events).
            method: HTTP method (for HTTP events).
            url: Request URL (for HTTP events).
            status_code: HTTP status code (for HTTP events).
            row_count: Number of rows (for SQL events).
            duration_ms: Duration in milliseconds.
            custom_summary: Custom summary to use instead of generated one.

        Returns:
            Human-readable summary string.
        """
        if custom_summary:
            return custom_summary

        if event_type == "action":
            return f"Calling {tool}"

        if event_type == "observation":
            return f"Result from {tool}"

        if event_type == "operation_call":
            return f"{method} {url} -> {status_code or '?'}"

        if event_type == "tool_call":
            if duration_ms is not None:
                return f"{tool} completed in {duration_ms:.0f}ms"
            return f"{tool} completed"

        if event_type == "sql_query":
            return f"SQL query: {row_count or '?'} rows"

        return event_type

    @staticmethod
    def create_detailed_event(
        event_type: str,
        summary: str,
        details: EventDetails,
        session_id: str | None = None,
        step_number: int | None = None,
        node_name: str | None = None,
        agent_name: str | None = None,
    ) -> DetailedEvent:
        """Create a DetailedEvent with the given context.

        Args:
            event_type: Type of event.
            summary: Human-readable summary.
            details: EventDetails object.
            session_id: Session ID for correlation.
            step_number: ReAct step number.
            node_name: Current graph node.
            agent_name: Name of the emitting agent.

        Returns:
            DetailedEvent instance.
        """
        return DetailedEvent.create(
            event_type=event_type,
            summary=summary,
            details=details,
            session_id=session_id,
            step_number=step_number,
            node_name=node_name,
            agent_name=agent_name,
        )

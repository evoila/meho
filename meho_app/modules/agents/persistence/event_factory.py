# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Factory for creating DetailedEvent instances.

This module provides a factory class for creating various types of DetailedEvent
objects used in transcript persistence. Separating event creation from collection
logic makes it easier to:
1. Test event creation independently
2. Reuse event creation patterns
3. Ensure consistent event structure

Example:
    >>> from meho_app.modules.agents.persistence.event_factory import EventFactory
    >>> event = EventFactory.create_llm_event(
    ...     session_id=session_id,
    ...     summary="LLM reasoning step",
    ...     prompt="System prompt",
    ...     response="LLM response",
    ... )
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from meho_app.modules.agents.base.detailed_events import (
    DetailedEvent,
    EventDetails,
    TokenUsage,
    estimate_cost,
)


class EventFactory:
    """Factory for creating DetailedEvent instances.

    This class provides static methods for creating various types of events
    that can be persisted to the transcript. Each method handles the proper
    construction of EventDetails with the appropriate fields.
    """

    @staticmethod
    def create_llm_event(
        session_id: str | UUID,
        summary: str,
        prompt: str | None = None,
        messages: list[dict] | None = None,
        response: str | None = None,
        parsed: dict | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str | None = None,
        duration_ms: float | None = None,
        step_number: int | None = None,
        node_name: str | None = None,
        agent_name: str | None = None,
    ) -> DetailedEvent:
        """Create a detailed LLM call event.

        Args:
            session_id: The chat session ID.
            summary: Brief summary for timeline display.
            prompt: Full system prompt.
            messages: Conversation history.
            response: Raw LLM response.
            parsed: Parsed response (thought/action/final_answer).
            prompt_tokens: Number of prompt tokens.
            completion_tokens: Number of completion tokens.
            model: Model name.
            duration_ms: Call duration in milliseconds.
            step_number: ReAct step number.
            node_name: Current graph node.
            agent_name: Agent name.

        Returns:
            DetailedEvent ready to be added to the collector.
        """
        token_usage = None
        if prompt_tokens or completion_tokens:
            total_tokens = prompt_tokens + completion_tokens
            cost = estimate_cost(model or "", prompt_tokens, completion_tokens)
            token_usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=cost,
            )

        details = EventDetails(
            llm_prompt=prompt,
            llm_messages=messages,
            llm_response=response,
            llm_parsed=parsed,
            token_usage=token_usage,
            llm_duration_ms=duration_ms,
            model=model,
        )

        return DetailedEvent.create(
            event_type="thought",
            summary=summary,
            details=details,
            session_id=str(session_id),
            step_number=step_number,
            node_name=node_name,
            agent_name=agent_name,
        )

    @staticmethod
    def create_tool_event(
        session_id: str | UUID,
        summary: str,
        tool_name: str,
        tool_input: dict | None = None,
        tool_output: Any = None,
        tool_error: str | None = None,
        duration_ms: float | None = None,
        step_number: int | None = None,
        node_name: str | None = None,
        agent_name: str | None = None,
    ) -> DetailedEvent:
        """Create a detailed tool call event.

        Args:
            session_id: The chat session ID.
            summary: Brief summary for timeline display.
            tool_name: Name of the tool.
            tool_input: Input parameters.
            tool_output: Return value.
            tool_error: Error message if failed.
            duration_ms: Execution duration in milliseconds.
            step_number: ReAct step number.
            node_name: Current graph node.
            agent_name: Agent name.

        Returns:
            DetailedEvent ready to be added to the collector.
        """
        details = EventDetails(
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
            tool_error=tool_error,
            tool_duration_ms=duration_ms,
        )

        event_type = "observation" if tool_output is not None else "action"

        return DetailedEvent.create(
            event_type=event_type,
            summary=summary,
            details=details,
            session_id=str(session_id),
            step_number=step_number,
            node_name=node_name,
            agent_name=agent_name,
        )

    @staticmethod
    def create_operation_event(
        session_id: str | UUID,
        summary: str,
        method: str,
        url: str,
        headers: dict | None = None,
        request_body: str | None = None,
        response_body: str | None = None,
        status_code: int | None = None,
        duration_ms: float | None = None,
        step_number: int | None = None,
        node_name: str | None = None,
        agent_name: str | None = None,
    ) -> DetailedEvent:
        """Create a detailed operation call event (REST/SOAP/VMware).

        Args:
            session_id: The chat session ID.
            summary: Brief summary for timeline display.
            method: HTTP method (GET, POST, etc.) for REST operations.
            url: Request URL.
            headers: Request headers (should be sanitized).
            request_body: Request body.
            response_body: Response body.
            status_code: HTTP status code (for REST operations).
            duration_ms: Request duration in milliseconds.
            step_number: ReAct step number.
            node_name: Current graph node.
            agent_name: Agent name.

        Returns:
            DetailedEvent ready to be added to the collector.
        """
        details = EventDetails(
            http_method=method,
            http_url=url,
            http_headers=headers,
            http_request_body=request_body,
            http_response_body=response_body,
            http_status_code=status_code,
            http_duration_ms=duration_ms,
        )

        return DetailedEvent.create(
            event_type="operation_call",
            summary=summary,
            details=details,
            session_id=str(session_id),
            step_number=step_number,
            node_name=node_name,
            agent_name=agent_name,
        )

    @staticmethod
    def create_sql_event(
        session_id: str | UUID,
        summary: str,
        query: str,
        parameters: dict | None = None,
        row_count: int | None = None,
        result_sample: list[dict] | None = None,
        duration_ms: float | None = None,
        step_number: int | None = None,
        node_name: str | None = None,
        agent_name: str | None = None,
    ) -> DetailedEvent:
        """Create a detailed SQL query event.

        Args:
            session_id: The chat session ID.
            summary: Brief summary for timeline display.
            query: SQL query string.
            parameters: Query parameters.
            row_count: Number of rows returned/affected.
            result_sample: First few rows of results.
            duration_ms: Query duration in milliseconds.
            step_number: ReAct step number.
            node_name: Current graph node.
            agent_name: Agent name.

        Returns:
            DetailedEvent ready to be added to the collector.
        """
        details = EventDetails(
            sql_query=query,
            sql_parameters=parameters,
            sql_row_count=row_count,
            sql_result_sample=result_sample,
            sql_duration_ms=duration_ms,
        )

        return DetailedEvent.create(
            event_type="sql_query",
            summary=summary,
            details=details,
            session_id=str(session_id),
            step_number=step_number,
            node_name=node_name,
            agent_name=agent_name,
        )

    @staticmethod
    def create_knowledge_search_event(
        session_id: str | UUID,
        summary: str,
        query: str,
        search_type: str,
        results_count: int,
        top_scores: list[float] | None = None,
        result_snippets: list[dict] | None = None,
        duration_ms: float | None = None,
        filters: dict | None = None,
        step_number: int | None = None,
        node_name: str | None = None,
        agent_name: str | None = None,
    ) -> DetailedEvent:
        """Create a detailed knowledge search event.

        Args:
            session_id: The chat session ID.
            summary: Brief summary for timeline display.
            query: Search query string.
            search_type: Search type ("semantic", "bm25", "hybrid").
            results_count: Number of results returned.
            top_scores: Top relevance scores (first few).
            result_snippets: Snippets of top results for preview.
            duration_ms: Search duration in milliseconds.
            filters: Metadata filters applied (connector_id, etc.).
            step_number: ReAct step number.
            node_name: Current graph node.
            agent_name: Agent name.

        Returns:
            DetailedEvent ready to be added to the collector.
        """
        details = EventDetails(
            search_query=query,
            search_type=search_type,
            search_results=result_snippets,
            search_scores=top_scores,
        )

        return DetailedEvent.create(
            event_type="knowledge_search",
            summary=summary,
            details=details,
            session_id=str(session_id),
            step_number=step_number,
            node_name=node_name,
            agent_name=agent_name,
        )

    @staticmethod
    def create_topology_lookup_event(
        session_id: str | UUID,
        summary: str,
        query: str,
        found: bool,
        entity_type: str | None = None,
        entity_name: str | None = None,
        connector_type: str | None = None,
        chain_length: int = 0,
        same_as_count: int = 0,
        possibly_related_count: int = 0,
        duration_ms: float | None = None,
        step_number: int | None = None,
        node_name: str | None = None,
        agent_name: str | None = None,
    ) -> DetailedEvent:
        """Create a detailed topology lookup event.

        Args:
            session_id: The chat session ID.
            summary: Brief summary for timeline display.
            query: Entity query string.
            found: Whether the entity was found.
            entity_type: Type of entity found (Pod, VM, etc.).
            entity_name: Name of entity found.
            connector_type: Connector type (kubernetes, vmware, etc.).
            chain_length: Number of items in traversal chain.
            same_as_count: Number of confirmed SAME_AS correlations.
            possibly_related_count: Number of possibly related entities.
            duration_ms: Lookup duration in milliseconds.
            step_number: ReAct step number.
            node_name: Current graph node.
            agent_name: Agent name.

        Returns:
            DetailedEvent ready to be added to the collector.
        """
        # Build entities_found list if entity was found
        entities_found = None
        if found and entity_name:
            entities_found = [
                {
                    "name": entity_name,
                    "type": entity_type,
                    "connector_type": connector_type,
                }
            ]

        details = EventDetails(
            entities_extracted=[query] if query else None,
            entities_found=entities_found,
        )

        return DetailedEvent.create(
            event_type="topology_lookup",
            summary=summary,
            details=details,
            session_id=str(session_id),
            step_number=step_number,
            node_name=node_name,
            agent_name=agent_name,
        )

    @staticmethod
    def create_error_event(
        session_id: str | UUID,
        message: str,
        traceback_str: str | None = None,
    ) -> DetailedEvent:
        """Create an error event for capturing fatal errors.

        Args:
            session_id: The chat session ID.
            message: Error message to capture.
            traceback_str: Optional traceback string.

        Returns:
            DetailedEvent ready to be added to the collector.
        """
        details_dict: dict = {"error": message}
        if traceback_str:
            details_dict["traceback"] = traceback_str

        return DetailedEvent.create(
            event_type="error",
            summary=f"Fatal: {message[:100]}",
            details=EventDetails(
                tool_error=message,
                tool_output=details_dict,
            ),
            session_id=str(session_id),
        )

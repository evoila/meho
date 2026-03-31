# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Event Type Definitions for Deep Observability.

This module defines the core dataclasses for representing detailed events
that capture full execution context for LLM calls, SQL queries, operation calls
(REST/SOAP/VMware), and tool invocations.

These types are used by:
- Persistence layer (TranscriptCollector, TranscriptService)
- Event factory for creating typed events
- Frontend for displaying expandable details

Example:
    >>> details = EventDetails(
    ...     llm_prompt="You are MEHO...",
    ...     llm_response="I will search for VMs...",
    ...     token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
    ... )
    >>> event = DetailedEvent.create(
    ...     event_type="thought",
    ...     summary="Analyzing request for VM inventory",
    ...     details=details,
    ... )
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


@dataclass
class TokenUsage:
    """Token usage metrics for LLM calls.

    Attributes:
        prompt_tokens: Number of tokens in the prompt.
        completion_tokens: Number of tokens in the completion.
        total_tokens: Total tokens used (prompt + completion).
        estimated_cost_usd: Estimated cost in USD (based on model pricing).
        cache_read_tokens: Tokens served from Anthropic prompt cache (10% cost).
        cache_write_tokens: Tokens written to Anthropic prompt cache (125% cost).
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenUsage:
        """Create TokenUsage from dictionary."""
        return cls(
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            total_tokens=data.get("total_tokens", 0),
            estimated_cost_usd=data.get("estimated_cost_usd"),
        )


@dataclass
class EventDetails:
    """Expandable details for each event type.

    This is a flexible container that holds different types of details
    depending on the event type. Only relevant fields are populated.

    Attributes:
        llm_prompt: Full system prompt sent to LLM.
        llm_messages: Conversation history/context sent to LLM.
        llm_response: Raw LLM response text.
        llm_parsed: Parsed response (thought/action/final_answer).
        token_usage: Token usage metrics.
        llm_duration_ms: LLM call duration in milliseconds.
        model: Model name used (e.g., "gpt-4.1-mini").

        sql_query: SQL query string.
        sql_parameters: Query parameters.
        sql_row_count: Number of rows returned/affected.
        sql_result_sample: First N rows of results.
        sql_duration_ms: Query execution duration in milliseconds.

        http_method: HTTP method (GET, POST, etc.) — populated for REST operations.
        http_url: Request URL — populated for REST operations.
        http_headers: Request headers (sanitized) — populated for REST operations.
        http_request_body: Request body — populated for REST operations.
        http_response_body: Response body — populated for REST operations.
        http_status_code: HTTP status code — populated for REST operations.
        http_duration_ms: Request duration in milliseconds.

        tool_name: Tool/function name.
        tool_input: Tool input parameters.
        tool_output: Tool return value.
        tool_duration_ms: Tool execution duration in milliseconds.
        tool_error: Error message if tool failed.

        search_query: Knowledge search query.
        search_type: Search type (vector, bm25, hybrid).
        search_results: Search result snippets.
        search_scores: Relevance scores.

        entities_extracted: Entities extracted from query (topology).
        entities_found: Entities found in database.
        context_injected: Context string injected into prompt.
    """

    # LLM call details
    llm_prompt: str | None = None
    llm_messages: list[dict[str, Any]] | None = None
    llm_response: str | None = None
    llm_parsed: dict[str, Any] | None = None
    token_usage: TokenUsage | None = None
    llm_duration_ms: float | None = None
    model: str | None = None

    # SQL query details
    sql_query: str | None = None
    sql_parameters: dict[str, Any] | None = None
    sql_row_count: int | None = None
    sql_result_sample: list[dict[str, Any]] | None = None
    sql_duration_ms: float | None = None

    # Operation call details (HTTP transport fields, populated for REST connectors)
    http_method: str | None = None
    http_url: str | None = None
    http_headers: dict[str, str] | None = None
    http_request_body: str | None = None
    http_response_body: str | None = None
    http_status_code: int | None = None
    http_duration_ms: float | None = None

    # Tool call details
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: Any = None
    tool_duration_ms: float | None = None
    tool_error: str | None = None

    # Knowledge search details
    search_query: str | None = None
    search_type: str | None = None
    search_results: list[dict[str, Any]] | None = None
    search_scores: list[float] | None = None

    # Topology details
    entities_extracted: list[str] | None = None
    entities_found: list[dict[str, Any]] | None = None
    context_injected: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary, excluding None values for compact storage."""
        result: dict[str, Any] = {}

        # LLM fields
        if self.llm_prompt is not None:
            result["llm_prompt"] = self.llm_prompt
        if self.llm_messages is not None:
            result["llm_messages"] = self.llm_messages
        if self.llm_response is not None:
            result["llm_response"] = self.llm_response
        if self.llm_parsed is not None:
            result["llm_parsed"] = self.llm_parsed
        if self.token_usage is not None:
            result["token_usage"] = self.token_usage.to_dict()
        if self.llm_duration_ms is not None:
            result["llm_duration_ms"] = self.llm_duration_ms
        if self.model is not None:
            result["model"] = self.model

        # SQL fields
        if self.sql_query is not None:
            result["sql_query"] = self.sql_query
        if self.sql_parameters is not None:
            result["sql_parameters"] = self.sql_parameters
        if self.sql_row_count is not None:
            result["sql_row_count"] = self.sql_row_count
        if self.sql_result_sample is not None:
            result["sql_result_sample"] = self.sql_result_sample
        if self.sql_duration_ms is not None:
            result["sql_duration_ms"] = self.sql_duration_ms

        # Operation call fields (HTTP transport)
        if self.http_method is not None:
            result["http_method"] = self.http_method
        if self.http_url is not None:
            result["http_url"] = self.http_url
        if self.http_headers is not None:
            result["http_headers"] = self.http_headers
        if self.http_request_body is not None:
            result["http_request_body"] = self.http_request_body
        if self.http_response_body is not None:
            result["http_response_body"] = self.http_response_body
        if self.http_status_code is not None:
            result["http_status_code"] = self.http_status_code
        if self.http_duration_ms is not None:
            result["http_duration_ms"] = self.http_duration_ms

        # Tool fields
        if self.tool_name is not None:
            result["tool_name"] = self.tool_name
        if self.tool_input is not None:
            result["tool_input"] = self.tool_input
        if self.tool_output is not None:
            # Handle non-serializable outputs
            try:
                json.dumps(self.tool_output, default=str)
                result["tool_output"] = self.tool_output
            except (TypeError, ValueError):
                result["tool_output"] = str(self.tool_output)
        if self.tool_duration_ms is not None:
            result["tool_duration_ms"] = self.tool_duration_ms
        if self.tool_error is not None:
            result["tool_error"] = self.tool_error

        # Knowledge search fields
        if self.search_query is not None:
            result["search_query"] = self.search_query
        if self.search_type is not None:
            result["search_type"] = self.search_type
        if self.search_results is not None:
            result["search_results"] = self.search_results
        if self.search_scores is not None:
            result["search_scores"] = self.search_scores

        # Topology fields
        if self.entities_extracted is not None:
            result["entities_extracted"] = self.entities_extracted
        if self.entities_found is not None:
            result["entities_found"] = self.entities_found
        if self.context_injected is not None:
            result["context_injected"] = self.context_injected

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventDetails:
        """Create EventDetails from dictionary."""
        token_usage = None
        if "token_usage" in data:
            token_usage = TokenUsage.from_dict(data["token_usage"])

        return cls(
            llm_prompt=data.get("llm_prompt"),
            llm_messages=data.get("llm_messages"),
            llm_response=data.get("llm_response"),
            llm_parsed=data.get("llm_parsed"),
            token_usage=token_usage,
            llm_duration_ms=data.get("llm_duration_ms"),
            model=data.get("model"),
            sql_query=data.get("sql_query"),
            sql_parameters=data.get("sql_parameters"),
            sql_row_count=data.get("sql_row_count"),
            sql_result_sample=data.get("sql_result_sample"),
            sql_duration_ms=data.get("sql_duration_ms"),
            http_method=data.get("http_method"),
            http_url=data.get("http_url"),
            http_headers=data.get("http_headers"),
            http_request_body=data.get("http_request_body"),
            http_response_body=data.get("http_response_body"),
            http_status_code=data.get("http_status_code"),
            http_duration_ms=data.get("http_duration_ms"),
            tool_name=data.get("tool_name"),
            tool_input=data.get("tool_input"),
            tool_output=data.get("tool_output"),
            tool_duration_ms=data.get("tool_duration_ms"),
            tool_error=data.get("tool_error"),
            search_query=data.get("search_query"),
            search_type=data.get("search_type"),
            search_results=data.get("search_results"),
            search_scores=data.get("search_scores"),
            entities_extracted=data.get("entities_extracted"),
            entities_found=data.get("entities_found"),
            context_injected=data.get("context_injected"),
        )

    def is_empty(self) -> bool:
        """Check if all fields are None/empty."""
        return len(self.to_dict()) == 0


@dataclass
class DetailedEvent:
    """Rich event with full context for observability.

    This extends the basic AgentEvent with expandable details that are
    persisted for later retrieval.

    Attributes:
        id: Unique event identifier.
        timestamp: When the event occurred (UTC).
        type: Event type (thought, action, observation, etc.).
        summary: Brief human-readable summary for timeline display.
        details: Full expandable details.
        parent_event_id: ID of parent event (for nested events).
        session_id: Chat session ID for correlation.
        step_number: ReAct step number.
        node_name: Current graph node name.
        agent_name: Name of the agent that emitted this event.
    """

    id: str
    timestamp: datetime
    type: str  # EventType but we use str for flexibility
    summary: str
    details: EventDetails = field(default_factory=EventDetails)
    parent_event_id: str | None = None
    session_id: str | None = None
    step_number: int | None = None
    node_name: str | None = None
    agent_name: str | None = None

    @classmethod
    def create(
        cls,
        event_type: str,
        summary: str,
        details: EventDetails | None = None,
        session_id: str | None = None,
        step_number: int | None = None,
        node_name: str | None = None,
        agent_name: str | None = None,
        parent_event_id: str | None = None,
    ) -> DetailedEvent:
        """Factory method to create a new DetailedEvent with auto-generated ID."""
        return cls(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(tz=UTC),
            type=event_type,
            summary=summary,
            details=details or EventDetails(),
            parent_event_id=parent_event_id,
            session_id=session_id,
            step_number=step_number,
            node_name=node_name,
            agent_name=agent_name,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "type": self.type,
            "summary": self.summary,
            "details": self.details.to_dict(),
        }
        if self.parent_event_id:
            result["parent_event_id"] = self.parent_event_id
        if self.session_id:
            result["session_id"] = self.session_id
        if self.step_number is not None:
            result["step_number"] = self.step_number
        if self.node_name:
            result["node_name"] = self.node_name
        if self.agent_name:
            result["agent_name"] = self.agent_name
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DetailedEvent:
        """Create DetailedEvent from dictionary."""
        timestamp = data.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        elif timestamp is None:
            timestamp = datetime.now(tz=UTC)

        details_data = data.get("details", {})
        details = (
            EventDetails.from_dict(details_data)
            if isinstance(details_data, dict)
            else EventDetails()
        )

        return cls(
            id=data.get("id", str(uuid.uuid4())),
            timestamp=timestamp,
            type=data.get("type", "unknown"),
            summary=data.get("summary", ""),
            details=details,
            parent_event_id=data.get("parent_event_id"),
            session_id=data.get("session_id"),
            step_number=data.get("step_number"),
            node_name=data.get("node_name"),
            agent_name=data.get("agent_name"),
        )

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_json(cls, json_str: str) -> DetailedEvent:
        """Deserialize from JSON string."""
        return cls.from_dict(json.loads(json_str))

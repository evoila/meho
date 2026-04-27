# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Transcript Collector for async buffered event persistence.

This module provides the TranscriptCollector class that buffers events
during agent execution and batch-inserts them to the database.

Key features:
- Async buffering to avoid blocking SSE stream
- Automatic flush when buffer is full
- Graceful shutdown with final flush

Example:
    >>> async with TranscriptCollector(transcript_id, service) as collector:
    ...     await collector.add(event1)
    ...     await collector.add(event2)
    ...     # Events are automatically flushed on exit
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any
from uuid import UUID

from meho_app.core.otel import get_logger
from meho_app.modules.agents.base.detailed_events import DetailedEvent
from meho_app.modules.agents.persistence.event_factory import EventFactory

if TYPE_CHECKING:
    from meho_app.modules.agents.persistence.transcript_service import (
        TranscriptService,
    )

logger = get_logger(__name__)

# Configuration
DEFAULT_BUFFER_SIZE = 10  # Flush after this many events
DEFAULT_FLUSH_INTERVAL_MS = 100  # Flush at least every 100ms


class TranscriptCollector:
    """Collects events during agent execution and persists them asynchronously.

    This class buffers DetailedEvents and batch-inserts them to the database
    to minimize the performance impact on the SSE stream.

    Attributes:
        transcript_id: The transcript ID to add events to.
        session_id: The chat session ID (for event correlation).
        service: The TranscriptService for database operations.
        buffer_size: Maximum events to buffer before flush.
    """

    def __init__(
        self,
        transcript_id: UUID,
        session_id: UUID,
        service: TranscriptService,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
    ) -> None:
        """Initialize the collector.

        Args:
            transcript_id: The transcript to add events to.
            session_id: The chat session ID.
            service: TranscriptService for database operations.
            buffer_size: Maximum events to buffer before auto-flush.
        """
        self._transcript_id = transcript_id
        self._session_id = session_id
        self._service = service
        self._buffer_size = buffer_size
        self._buffer: list[DetailedEvent] = []
        self._lock = asyncio.Lock()
        self._closed = False

        # Statistics tracking
        self._llm_calls = 0
        self._sql_queries = 0
        self._operation_calls = 0
        self._tool_calls = 0
        self._knowledge_searches = 0
        self._topology_lookups = 0
        self._total_tokens = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0
        self._total_cost_usd: float = 0.0
        self._start_time = datetime.now(tz=UTC)

    @property
    def transcript_id(self) -> UUID:
        """Get the transcript ID."""
        return self._transcript_id

    @property
    def session_id(self) -> UUID:
        """Get the session ID."""
        return self._session_id

    async def add(self, event: DetailedEvent) -> None:
        """Add an event to the buffer.

        If the buffer is full, triggers an automatic flush.

        Args:
            event: The DetailedEvent to buffer.

        Raises:
            RuntimeError: If the collector has been closed.
        """
        if self._closed:
            logger.warning(
                f"Attempted to add event to closed collector for transcript {self._transcript_id}"
            )
            return

        async with self._lock:
            # Ensure session_id is set on the event
            if event.session_id is None:
                event.session_id = str(self._session_id)

            self._buffer.append(event)
            self._update_stats(event)

            # Auto-flush if buffer is full
            if len(self._buffer) >= self._buffer_size:
                await self._flush_buffer()

    def _update_stats(self, event: DetailedEvent) -> None:
        """Update statistics based on event type and details."""
        details = event.details

        # LLM calls
        if details.llm_prompt is not None or details.llm_response is not None:
            self._llm_calls += 1
            if details.token_usage is not None:
                self._total_tokens += details.token_usage.total_tokens
                self._prompt_tokens += details.token_usage.prompt_tokens
                self._completion_tokens += details.token_usage.completion_tokens
                self._cache_read_tokens += details.token_usage.cache_read_tokens
                self._cache_write_tokens += details.token_usage.cache_write_tokens
                if details.token_usage.estimated_cost_usd is not None:
                    self._total_cost_usd += details.token_usage.estimated_cost_usd

        # SQL queries
        if details.sql_query is not None:
            self._sql_queries += 1

        # Operation calls (REST/SOAP/VMware)
        if details.http_url is not None:
            self._operation_calls += 1

        # Tool calls
        if details.tool_name is not None:
            self._tool_calls += 1

        # Knowledge searches
        if details.search_query is not None:
            self._knowledge_searches += 1

        # Topology lookups
        if details.entities_extracted is not None:
            self._topology_lookups += 1

    async def _flush_buffer(self) -> None:
        """Flush buffered events to the database.

        This is an internal method called with the lock held.
        """
        if not self._buffer:
            return

        events_to_flush = self._buffer.copy()
        self._buffer.clear()

        try:
            await self._service.batch_add_events(
                self._transcript_id,
                events_to_flush,
            )
            logger.debug(
                f"Flushed {len(events_to_flush)} events to transcript {self._transcript_id}"
            )
        except Exception as e:
            logger.error(f"Failed to flush events to transcript {self._transcript_id}: {e}")
            # CRITICAL: Rollback session to prevent contaminating other operations
            # that share this session (e.g., message persistence)
            try:  # noqa: SIM105 -- explicit error handling preferred
                await self._service.session.rollback()
            except Exception:  # noqa: S110 -- intentional silent exception handling
                pass  # Session may already be closed
            # Re-add events to buffer for retry (won't succeed until session is healthy)
            self._buffer.extend(events_to_flush)

    async def flush(self) -> None:
        """Manually flush any buffered events.

        Call this to ensure all events are persisted before a checkpoint.
        """
        async with self._lock:
            await self._flush_buffer()

    async def close(self, status: str = "completed") -> None:
        """Close the collector and finalize the transcript.

        Flushes any remaining events and updates the transcript statistics.

        Args:
            status: Final transcript status (completed, failed).
        """
        if self._closed:
            return

        self._closed = True

        async with self._lock:
            # Flush remaining events
            await self._flush_buffer()

            # Calculate total duration
            duration_ms = (datetime.now(tz=UTC) - self._start_time).total_seconds() * 1000

            # Update transcript statistics
            try:
                await self._service.update_stats(
                    self._transcript_id,
                    llm_calls=self._llm_calls,
                    sql_queries=self._sql_queries,
                    http_calls=self._operation_calls,
                    tool_calls=self._tool_calls,
                    tokens=self._total_tokens,
                    cost_usd=self._total_cost_usd if self._total_cost_usd > 0 else None,
                    duration_ms=duration_ms,
                )

                # Mark transcript as complete
                await self._service.complete_transcript(
                    self._transcript_id,
                    status=status,
                )

                # CRITICAL: Commit the transaction to persist the updates
                # Without this, the status and stats updates will be rolled back
                await self._service.session.commit()

                logger.debug(
                    f"Closed collector for transcript {self._transcript_id}: "
                    f"{self._llm_calls} LLM calls, {self._tool_calls} tool calls, "
                    f"{self._operation_calls} operation calls, {self._knowledge_searches} searches, "
                    f"{self._topology_lookups} lookups, {self._total_tokens} tokens, {duration_ms:.0f}ms"
                )
            except Exception as e:
                logger.error(f"Failed to close transcript {self._transcript_id}: {e}")

    async def __aenter__(self) -> TranscriptCollector:
        """Enter async context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit async context manager, ensuring flush and close."""
        status = "failed" if exc_type is not None else "completed"
        await self.close(status=status)

    async def add_error_event(
        self,
        message: str,
        include_traceback: bool = False,
    ) -> None:
        """Add an error event for capturing fatal errors.

        This method is designed to be called during exception handling
        to ensure error information is captured in the transcript even
        when the generator is cancelled (e.g., client disconnect).

        Args:
            message: Error message to capture.
            include_traceback: If True, includes the current traceback.

        Example:
            >>> except GeneratorExit:
            ...     await collector.add_error_event("Client disconnected")
            ...     raise
        """
        import traceback as tb

        traceback_str = tb.format_exc() if include_traceback else None
        event = EventFactory.create_error_event(
            session_id=self._session_id,
            message=message,
            traceback_str=traceback_str,
        )
        await self.add(event)

    # Convenience methods for creating detailed events
    # These delegate to EventFactory for consistent event creation

    def create_llm_event(
        self,
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
        return EventFactory.create_llm_event(
            session_id=self._session_id,
            summary=summary,
            prompt=prompt,
            messages=messages,
            response=response,
            parsed=parsed,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
            duration_ms=duration_ms,
            step_number=step_number,
            node_name=node_name,
            agent_name=agent_name,
        )

    def create_tool_event(
        self,
        summary: str,
        tool_name: str,
        tool_input: dict | None = None,
        tool_output: Any | None = None,
        tool_error: str | None = None,
        duration_ms: float | None = None,
        step_number: int | None = None,
        node_name: str | None = None,
        agent_name: str | None = None,
    ) -> DetailedEvent:
        """Create a detailed tool call event.

        Args:
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
        return EventFactory.create_tool_event(
            session_id=self._session_id,
            summary=summary,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
            tool_error=tool_error,
            duration_ms=duration_ms,
            step_number=step_number,
            node_name=node_name,
            agent_name=agent_name,
        )

    def create_operation_event(
        self,
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
        return EventFactory.create_operation_event(
            session_id=self._session_id,
            summary=summary,
            method=method,
            url=url,
            headers=headers,
            request_body=request_body,
            response_body=response_body,
            status_code=status_code,
            duration_ms=duration_ms,
            step_number=step_number,
            node_name=node_name,
            agent_name=agent_name,
        )

    def create_sql_event(
        self,
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
        return EventFactory.create_sql_event(
            session_id=self._session_id,
            summary=summary,
            query=query,
            parameters=parameters,
            row_count=row_count,
            result_sample=result_sample,
            duration_ms=duration_ms,
            step_number=step_number,
            node_name=node_name,
            agent_name=agent_name,
        )

    def create_knowledge_search_event(
        self,
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
        return EventFactory.create_knowledge_search_event(
            session_id=self._session_id,
            summary=summary,
            query=query,
            search_type=search_type,
            results_count=results_count,
            top_scores=top_scores,
            result_snippets=result_snippets,
            duration_ms=duration_ms,
            filters=filters,
            step_number=step_number,
            node_name=node_name,
            agent_name=agent_name,
        )

    def create_topology_lookup_event(
        self,
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
        return EventFactory.create_topology_lookup_event(
            session_id=self._session_id,
            summary=summary,
            query=query,
            found=found,
            entity_type=entity_type,
            entity_name=entity_name,
            connector_type=connector_type,
            chain_length=chain_length,
            same_as_count=same_as_count,
            possibly_related_count=possibly_related_count,
            duration_ms=duration_ms,
            step_number=step_number,
            node_name=node_name,
            agent_name=agent_name,
        )

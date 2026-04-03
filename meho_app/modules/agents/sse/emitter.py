# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""SSE Event Emitter - Easy event emission from anywhere in agent code.

This module provides the EventEmitter class for emitting SSE events
from agents, tools, and nodes.

With transcript collection enabled, events are also persisted to the database
for historical access and deep observability.

Example:
    >>> emitter = EventEmitter(agent_name="react")
    >>> await emitter.thought("Analyzing the request...")
    >>> await emitter.action("search_operations", {"connector_id": "abc"})

    # With transcript collection:
    >>> emitter.set_transcript_collector(collector)
    >>> await emitter.emit_detailed(
    ...     "thought",
    ...     summary="Analyzing request",
    ...     details=EventDetails(llm_prompt="...", llm_response="..."),
    ... )
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.agents.base.events import AgentEvent, EventType

if TYPE_CHECKING:
    from meho_app.modules.agents.base.detailed_events import EventDetails
    from meho_app.modules.agents.persistence.transcript_collector import (
        TranscriptCollector,
    )

logger = get_logger(__name__)

# Type for async callback
EmitCallback = Callable[[AgentEvent], Awaitable[None]]


@dataclass
class EventEmitter:
    """Emits SSE events to the frontend with optional transcript persistence.

    Usage in tools/nodes:
        await emitter.emit("thought", {"content": "Thinking..."})
        await emitter.emit("tool_start", {"tool": "list_connectors"})

    Or use typed helpers:
        await emitter.thought("Analyzing the request...")
        await emitter.action("search_operations", {"connector_id": "abc"})

    For deep observability, use emit_detailed:
        await emitter.emit_detailed("thought", summary, EventDetails(...))

    Attributes:
        agent_name: Name of the agent emitting events.
        session_id: Optional session ID for event correlation.
    """

    agent_name: str
    session_id: str | None = None
    _callback: EmitCallback | None = field(default=None, repr=False)
    _queue: asyncio.Queue[AgentEvent] | None = field(default=None, repr=False)
    _step: int = field(default=0, repr=False)
    _current_node: str | None = field(default=None, repr=False)
    _transcript_collector: TranscriptCollector | None = field(default=None, repr=False)

    def set_callback(self, callback: EmitCallback) -> None:
        """Set the async callback for event delivery.

        Args:
            callback: Async function that receives AgentEvent objects.
        """
        self._callback = callback

    def set_queue(self, queue: asyncio.Queue[AgentEvent]) -> None:
        """Set the queue for event delivery (alternative to callback).

        Args:
            queue: asyncio.Queue to put events into.
        """
        self._queue = queue

    def set_context(
        self,
        step: int | None = None,
        node: str | None = None,
    ) -> None:
        """Update context for subsequent events.

        Args:
            step: Current ReAct step number.
            node: Current node name.
        """
        if step is not None:
            self._step = step
        if node is not None:
            self._current_node = node

    def increment_step(self) -> int:
        """Increment and return the step counter.

        Returns:
            The new step number.
        """
        self._step += 1
        return self._step

    def set_transcript_collector(self, collector: TranscriptCollector | None) -> None:
        """Enable or disable transcript persistence.

        When a collector is set, detailed events will be persisted
        to the database for later retrieval.

        Args:
            collector: TranscriptCollector instance, or None to disable.
        """
        self._transcript_collector = collector

    @property
    def has_transcript_collector(self) -> bool:
        """Check if transcript collection is enabled."""
        return self._transcript_collector is not None

    async def _persist_basic_event(
        self,
        event_type: EventType,
        summary: str,
        details_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Persist a basic event to the transcript collector.

        Called by simple helper methods (thought, action, observation, etc.)
        to auto-persist events when a collector is available. The
        ``*_detailed()`` methods handle their own persistence and do NOT
        call this -- they invoke ``self.emit()`` directly, so there is no
        double-persistence risk.

        Args:
            event_type: Event type string.
            summary: Human-readable summary (truncated to 500 chars).
            details_kwargs: Optional extra fields to set on EventDetails.
        """
        if self._transcript_collector is None:
            return

        from meho_app.modules.agents.base.detailed_events import (
            DetailedEvent,
            EventDetails,
        )

        details = EventDetails(**(details_kwargs or {}))
        event = DetailedEvent.create(
            event_type=event_type,
            summary=summary[:500],
            details=details,
            session_id=self.session_id,
            step_number=self._step if self._step > 0 else None,
            node_name=self._current_node,
            agent_name=self.agent_name,
        )
        try:
            await self._transcript_collector.add(event)
        except Exception as e:
            logger.warning(f"Failed to persist {event_type} event: {e}")

    async def emit(
        self,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Emit an event.

        Args:
            event_type: One of the defined EventType values.
            data: Event-specific data payload.
        """
        event = AgentEvent(
            type=event_type,
            agent=self.agent_name,
            data=data or {},
            session_id=self.session_id,
            step=self._step if self._step > 0 else None,
            node=self._current_node,
        )

        if self._callback:
            result = self._callback(event)
            if inspect.isawaitable(result):
                await result
        elif self._queue:
            await self._queue.put(event)

    async def emit_detailed(
        self,
        event_type: EventType,
        summary: str,
        details: EventDetails,
    ) -> None:
        """Emit an event with full details for transcript persistence.

        This method:
        1. Emits a standard SSE event with the summary
        2. Persists full details to the transcript (if collector is set)

        Args:
            event_type: One of the defined EventType values.
            summary: Brief human-readable summary for SSE.
            details: Full EventDetails for transcript storage.
        """
        # Always emit the standard SSE event
        await self.emit(event_type, {"content": summary, "summary": summary})

        # Persist to transcript if collector is available
        if self._transcript_collector is not None:
            from meho_app.modules.agents.base.detailed_events import DetailedEvent

            detailed_event = DetailedEvent.create(
                event_type=event_type,
                summary=summary,
                details=details,
                session_id=self.session_id,
                step_number=self._step if self._step > 0 else None,
                node_name=self._current_node,
                agent_name=self.agent_name,
            )

            try:
                await self._transcript_collector.add(detailed_event)
            except Exception as e:
                logger.warning(f"Failed to persist event to transcript: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # Typed helper methods - IDE autocomplete + type safety
    # ─────────────────────────────────────────────────────────────────────

    async def thought(self, content: str) -> None:
        """Emit a thought event.

        Handles both regular thought content and Claude ThinkingPart content.
        Claude ThinkingPart -> SSE "thought" event (maps adaptive thinking to
        existing frontend event type). See: RESEARCH.md Pitfall 4, CONTEXT.md
        "stream everything" decision.

        Args:
            content: The thought content (may originate from ThinkingPart).
        """
        await self.emit("thought", {"content": content})
        await self._persist_basic_event("thought", content)

    async def thinking_part(self, content: str) -> None:
        """Emit a thought event from Claude's adaptive thinking (ThinkingPart).

        Maps Claude's internal reasoning (ThinkingPart objects from adaptive
        thinking) to the existing SSE 'thought' event type. This preserves
        backward-compatibility with the frontend useSSEStream hook that
        already renders 'thought' events.

        When adaptive thinking is disabled or a non-Anthropic provider is used,
        this method is never called -- no regression.

        Args:
            content: The thinking content from ThinkingPart.
        """
        # Route through the standard thought() method for consistency
        await self.thought(content)

    async def action(self, tool: str, args: dict[str, Any]) -> None:
        """Emit an action event (tool about to be called).

        Args:
            tool: Tool name being called.
            args: Tool arguments.
        """
        await self.emit("action", {"tool": tool, "args": args})
        await self._persist_basic_event(
            "action", f"Calling {tool}", {"tool_name": tool, "tool_input": args}
        )

    async def observation(self, tool: str, result: Any, *, summary: str | None = None) -> None:
        """Emit an observation event (tool result).

        Args:
            tool: Tool name that was called.
            result: Tool execution result (string or dict with structured data).
            summary: Optional human-readable summary for UI display.
                     Use this to provide meaningful context without exposing
                     full data in the event stream. Example: "44 namespaces found"
        """
        import json

        # Handle structured results (dict with message + data)
        if isinstance(result, dict):
            # Keep structured data but ensure it's serializable
            try:
                # Verify it's JSON serializable
                result_json = json.dumps(result, default=str)
                payload: dict[str, Any] = {"tool": tool, "result": result}
                if summary:
                    payload["summary"] = summary
                await self.emit("observation", payload)
                await self._persist_basic_event(
                    "observation",
                    summary or f"Result from {tool}",
                    {"tool_name": tool, "tool_output": result_json[:2000]},
                )
                return
            except (TypeError, ValueError):
                # Fall through to string conversion
                pass

        # For simple results, truncate large strings
        result_str = str(result)
        if len(result_str) > 5000:
            result_str = result_str[:5000] + "... [truncated]"
        payload = {"tool": tool, "result": result_str}
        if summary:
            payload["summary"] = summary
        await self.emit("observation", payload)
        await self._persist_basic_event(
            "observation",
            summary or f"Result from {tool}",
            {"tool_name": tool, "tool_output": result_str[:2000]},
        )

    async def synthesis_chunk(self, content: str, accumulated: str = "") -> None:
        """Emit a synthesis chunk event for progressive streaming.

        Args:
            content: The new chunk of synthesis text.
            accumulated: The full accumulated text so far (for reconnection).
        """
        await self.emit("synthesis_chunk", {"content": content, "accumulated": accumulated})

    async def final_answer(self, content: str) -> None:
        """Emit a final answer event.

        Args:
            content: The final answer content.
        """
        await self.emit("final_answer", {"content": content})
        await self._persist_basic_event("final_answer", content[:200])

    async def error(
        self,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Emit an error event.

        Args:
            message: Error message.
            details: Optional additional error details.
        """
        data: dict[str, Any] = {"message": message}
        if details:
            data["details"] = details
        await self.emit("error", data)
        await self._persist_basic_event("error", message, {"tool_error": message})

    async def classified_error(
        self,
        error: Any,
        *,
        message: str | None = None,
    ) -> None:
        """Emit a classified error event with full metadata for frontend rendering.

        Args:
            error: A ClassifiedError instance (LLMError, ConnectorError, InternalError).
            message: Optional override message. Defaults to error.message.
        """
        from meho_app.core.errors import ClassifiedError, get_current_trace_id

        error_message = message or (error.message if hasattr(error, "message") else str(error))

        data: dict[str, Any] = {"message": error_message}

        if isinstance(error, ClassifiedError):
            data.update(error.classification_dict())
            data["recoverable"] = error.is_transient
        else:
            # Fallback for non-classified errors
            data["error_source"] = "internal"
            data["error_type"] = "unknown"
            data["severity"] = "permanent"
            data["recoverable"] = False
            data["trace_id"] = get_current_trace_id()

        await self.emit("error", data)
        await self._persist_basic_event("error", error_message, {"tool_error": error_message})

    async def warning(
        self,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Emit a warning event.

        Args:
            message: Warning message.
            details: Optional additional warning details.
        """
        data: dict[str, Any] = {"message": message}
        if details:
            data["details"] = details
        await self.emit("warning", data)

    async def tool_start(self, tool: str) -> None:
        """Emit tool execution starting.

        Args:
            tool: Tool name starting execution.
        """
        await self.emit("tool_start", {"tool": tool})

    async def tool_complete(self, tool: str, success: bool = True) -> None:
        """Emit tool execution complete.

        Args:
            tool: Tool name that completed.
            success: Whether the tool succeeded.
        """
        await self.emit("tool_complete", {"tool": tool, "success": success})

    async def tool_error(
        self,
        tool: str,
        error: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Emit tool execution error.

        Args:
            tool: Tool name that errored.
            error: Error message.
            details: Optional additional error details.
        """
        data: dict[str, Any] = {"tool": tool, "error": error}
        if details:
            data["details"] = details
        await self.emit("tool_error", data)

    async def node_enter(self, node: str) -> None:
        """Emit entering a node (for tracing).

        Args:
            node: Node name being entered.
        """
        self._current_node = node
        await self.emit("node_enter", {"node": node})

    async def node_exit(self, node: str, next_node: str | None = None) -> None:
        """Emit exiting a node (for tracing).

        Args:
            node: Node name being exited.
            next_node: Name of next node (if any).
        """
        await self.emit("node_exit", {"node": node, "next_node": next_node})

    async def approval_required(
        self,
        tool: str,
        args: dict[str, Any],
        danger_level: str,
        description: str,
        approval_id: str | None = None,
    ) -> None:
        """Emit approval required event.

        Args:
            tool: Tool requiring approval.
            args: Tool arguments.
            danger_level: Level of danger (safe/caution/dangerous/critical).
            description: Human-readable description of the action.
            approval_id: UUID of the approval request for frontend resume flow.
        """
        op_id = args.get("operation_id") or ""
        await self.emit(
            "approval_required",
            {
                "approval_id": approval_id,
                "tool": tool,
                "args": args,
                "tool_args": args,
                "danger_level": danger_level,
                "description": description,
                "message": description,
                "details": {
                    "method": args.get("method"),
                    "path": op_id or None,
                    "description": description,
                },
            },
        )

    async def approval_resolved(
        self,
        tool: str,
        approved: bool,
        reason: str | None = None,
    ) -> None:
        """Emit approval resolved event.

        Args:
            tool: Tool that was approved/rejected.
            approved: Whether the action was approved.
            reason: Optional reason for the decision.
        """
        data: dict[str, Any] = {"tool": tool, "approved": approved}
        if reason:
            data["reason"] = reason
        await self.emit("approval_resolved", data)

    async def keepalive(self) -> None:
        """Emit SSE keepalive comment to prevent connection timeout.

        Sent every 15 seconds during approval wait to keep the SSE
        connection alive through proxies and load balancers.
        """
        await self.emit("keepalive", {})

    async def audit_entry(self, data: dict[str, Any]) -> None:
        """Emit audit trail entry for approved/denied operations.

        Carries operation outcome (success/failure/skipped) alongside
        trust tier and decision metadata for inline chat audit cards.

        Args:
            data: Audit entry payload with approval_id, tool, trust_tier,
                  decision, outcome_status, outcome_summary, connector_name,
                  and timestamp fields.
        """
        await self.emit("audit_entry", data)

    async def hypothesis_update(
        self,
        hypothesis_id: str,
        text: str,
        status: str,
        connector_id: str | None = None,
        connector_name: str | None = None,
    ) -> None:
        """Emit hypothesis status update for agent pane tracking (Phase 62).

        Args:
            hypothesis_id: Unique hypothesis identifier (e.g., "h-1").
            text: Hypothesis text description.
            status: One of: investigating, validated, invalidated, inconclusive.
            connector_id: Optional connector that surfaced this hypothesis.
            connector_name: Optional human-readable connector name.
        """
        await self.emit(
            "hypothesis_update",
            {
                "hypothesis_id": hypothesis_id,
                "text": text,
                "status": status,
                "connector_id": connector_id,
                "connector_name": connector_name,
            },
        )

    async def orchestrator_thinking(self, content: str, accumulated: str = "") -> None:
        """Emit orchestrator thinking chunk for progressive streaming (Phase 110 D-01).

        Args:
            content: The new chunk of thinking text.
            accumulated: The full accumulated text so far.
        """
        await self.emit(
            "orchestrator_thinking",
            {
                "content": content,
                "accumulated": accumulated,
            },
        )

    async def follow_up_suggestions(self, suggestions: list[str]) -> None:
        """Emit follow-up question suggestions after synthesis (Phase 62).

        Args:
            suggestions: List of 2-3 suggested follow-up questions.
        """
        await self.emit(
            "follow_up_suggestions",
            {
                "suggestions": suggestions,
            },
        )

    async def citation_map(self, citations: dict[str, Any]) -> None:
        """Emit citation marker -> data_ref mapping after synthesis (Phase 62).

        Args:
            citations: Dict mapping citation number (string) to citation data
                       with step_id, connector_id, connector_name, connector_type, data_ref.
        """
        await self.emit(
            "citation_map",
            {
                "citations": citations,
            },
        )

    async def agent_start(self, user_message: str) -> None:
        """Emit agent start event.

        Args:
            user_message: The user's input message.
        """
        await self.emit("agent_start", {"user_message": user_message})

    async def agent_complete(self, success: bool = True) -> None:
        """Emit agent complete event.

        Args:
            success: Whether the agent completed successfully.
        """
        await self.emit("agent_complete", {"success": success})

    async def progress(
        self,
        message: str,
        percentage: int | None = None,
    ) -> None:
        """Emit a progress event.

        Args:
            message: Progress message.
            percentage: Optional completion percentage (0-100).
        """
        data: dict[str, Any] = {"message": message}
        if percentage is not None:
            data["percentage"] = percentage
        await self.emit("progress", data)

    # ─────────────────────────────────────────────────────────────────────
    # Detailed helper methods - Emit with full transcript persistence
    # ─────────────────────────────────────────────────────────────────────

    async def thought_detailed(
        self,
        summary: str,
        prompt: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        response: str | None = None,
        parsed: dict[str, Any] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        """Emit a thought event with full LLM details.

        Args:
            summary: Brief summary for SSE stream.
            prompt: Full system prompt.
            messages: Conversation history.
            response: Raw LLM response.
            parsed: Parsed response (thought/action/final_answer).
            prompt_tokens: Number of prompt tokens.
            completion_tokens: Number of completion tokens.
            model: Model name.
            duration_ms: LLM call duration.
        """
        # Standard SSE event
        await self.emit("thought", {"content": summary})

        # Persist detailed event if collector is available
        if self._transcript_collector is not None:
            from meho_app.modules.agents.base.detailed_events import (
                EventDetails,
                TokenUsage,
                estimate_cost,
            )

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

            await self.emit_detailed("thought", summary, details)

    async def action_detailed(
        self,
        tool: str,
        args: dict[str, Any],
        summary: str | None = None,
    ) -> None:
        """Emit an action event with full tool details.

        Args:
            tool: Tool name being called.
            args: Tool arguments.
            summary: Optional summary (defaults to "Calling {tool}").
        """
        # Standard SSE event
        await self.emit("action", {"tool": tool, "args": args})

        # Persist detailed event if collector is available
        if self._transcript_collector is not None:
            from meho_app.modules.agents.base.detailed_events import (
                DetailedEvent,
                EventDetails,
            )

            details = EventDetails(
                tool_name=tool,
                tool_input=args,
            )

            event_summary = summary or f"Calling {tool}"
            detailed_event = DetailedEvent.create(
                event_type="action",
                summary=event_summary,
                details=details,
                session_id=self.session_id,
                step_number=self._step if self._step > 0 else None,
                node_name=self._current_node,
                agent_name=self.agent_name,
            )

            try:
                await self._transcript_collector.add(detailed_event)
            except Exception as e:
                logger.warning(f"Failed to persist action event: {e}")

    async def observation_detailed(
        self,
        tool: str,
        result: Any,
        summary: str | None = None,
        duration_ms: float | None = None,
        error: str | None = None,
    ) -> None:
        """Emit an observation event with full tool result.

        Args:
            tool: Tool name that was called.
            result: Full tool output (will be sanitized for storage).
            summary: Human-readable summary for SSE.
            duration_ms: Tool execution duration.
            error: Error message if tool failed.
        """
        # Standard SSE event (truncated for stream)
        await self.observation(tool, result, summary=summary)

        # Persist detailed event if collector is available
        if self._transcript_collector is not None:
            from meho_app.modules.agents.base.detailed_events import (
                DetailedEvent,
                EventDetails,
            )
            from meho_app.modules.agents.persistence.scrubber import (
                sanitize_tool_output,
            )

            # Sanitize the output before storage
            sanitized_output = sanitize_tool_output(result)

            details = EventDetails(
                tool_name=tool,
                tool_output=sanitized_output,
                tool_duration_ms=duration_ms,
                tool_error=error,
            )

            event_summary = summary or f"Result from {tool}"
            detailed_event = DetailedEvent.create(
                event_type="observation",
                summary=event_summary,
                details=details,
                session_id=self.session_id,
                step_number=self._step if self._step > 0 else None,
                node_name=self._current_node,
                agent_name=self.agent_name,
            )

            try:
                await self._transcript_collector.add(detailed_event)
            except Exception as e:
                logger.warning(f"Failed to persist observation event: {e}")

    async def operation_call_detailed(
        self,
        method: str,
        url: str,
        status_code: int | None = None,
        request_body: str | None = None,
        response_body: str | None = None,
        headers: dict[str, str] | None = None,
        duration_ms: float | None = None,
        summary: str | None = None,
    ) -> None:
        """Emit an operation call event with full request/response details.

        Args:
            method: HTTP method (GET, POST, etc.) for REST operations.
            url: Request URL.
            status_code: HTTP status code (for REST operations).
            request_body: Request body.
            response_body: Response body.
            headers: Request headers.
            duration_ms: Request duration.
            summary: Human-readable summary.
        """
        # Persist detailed event if collector is available
        if self._transcript_collector is not None:
            from meho_app.modules.agents.base.detailed_events import (
                DetailedEvent,
                EventDetails,
            )
            from meho_app.modules.agents.persistence.scrubber import (
                sanitize_headers,
                sanitize_http_body,
            )

            details = EventDetails(
                http_method=method,
                http_url=url,
                http_status_code=status_code,
                http_headers=sanitize_headers(headers),
                http_request_body=sanitize_http_body(request_body),
                http_response_body=sanitize_http_body(response_body),
                http_duration_ms=duration_ms,
            )

            event_summary = summary or f"{method} {url} -> {status_code or '?'}"
            detailed_event = DetailedEvent.create(
                event_type="operation_call",
                summary=event_summary,
                details=details,
                session_id=self.session_id,
                step_number=self._step if self._step > 0 else None,
                node_name=self._current_node,
                agent_name=self.agent_name,
            )

            try:
                await self._transcript_collector.add(detailed_event)
            except Exception as e:
                logger.warning(f"Failed to persist HTTP call event: {e}")

    async def tool_call_detailed(
        self,
        tool: str,
        args: dict[str, Any],
        result: Any,
        summary: str | None = None,
        duration_ms: float | None = None,
        error: str | None = None,
    ) -> None:
        """Emit a consolidated tool call event with both input and output.

        This replaces the separate action_detailed + observation_detailed pattern
        with a single event that contains both the tool input and result.

        Args:
            tool: Tool name that was called.
            args: Tool arguments (input).
            result: Tool execution result (output).
            summary: Human-readable summary for SSE.
            duration_ms: Tool execution duration.
            error: Error message if tool failed.
        """
        event_summary = summary or f"{tool} completed"
        if duration_ms is not None:
            event_summary = f"{tool} completed in {duration_ms:.0f}ms"

        # Standard SSE event for live stream
        await self.emit("tool_call", {"tool": tool, "summary": event_summary})

        # Persist detailed event if collector is available
        if self._transcript_collector is not None:
            from meho_app.modules.agents.base.detailed_events import (
                DetailedEvent,
                EventDetails,
            )
            from meho_app.modules.agents.persistence.scrubber import (
                sanitize_tool_output,
            )

            # Sanitize the output before storage
            sanitized_output = sanitize_tool_output(result) if result is not None else None

            details = EventDetails(
                tool_name=tool,
                tool_input=args,
                tool_output=sanitized_output,
                tool_duration_ms=duration_ms,
                tool_error=error,
            )

            detailed_event = DetailedEvent.create(
                event_type="tool_call",
                summary=event_summary,
                details=details,
                session_id=self.session_id,
                step_number=self._step if self._step > 0 else None,
                node_name=self._current_node,
                agent_name=self.agent_name,
            )

            try:
                await self._transcript_collector.add(detailed_event)
            except Exception as e:
                logger.warning(f"Failed to persist tool_call event: {e}")

    async def sql_query_detailed(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        row_count: int | None = None,
        result_sample: list[dict[str, Any]] | None = None,
        duration_ms: float | None = None,
        summary: str | None = None,
    ) -> None:
        """Emit a SQL query event with full query details.

        Args:
            query: SQL query string.
            parameters: Query parameters.
            row_count: Number of rows returned/affected.
            result_sample: First few rows of results.
            duration_ms: Query duration.
            summary: Human-readable summary.
        """
        # Persist detailed event if collector is available
        if self._transcript_collector is not None:
            from meho_app.modules.agents.base.detailed_events import (
                DetailedEvent,
                EventDetails,
            )
            from meho_app.modules.agents.persistence.scrubber import (
                scrub_sensitive_data,
                truncate_result_sample,
            )

            details = EventDetails(
                sql_query=query,
                sql_parameters=scrub_sensitive_data(parameters) if parameters else None,
                sql_row_count=row_count,
                sql_result_sample=truncate_result_sample(result_sample),
                sql_duration_ms=duration_ms,
            )

            event_summary = summary or f"SQL query: {row_count or '?'} rows"
            detailed_event = DetailedEvent.create(
                event_type="sql_query",
                summary=event_summary,
                details=details,
                session_id=self.session_id,
                step_number=self._step if self._step > 0 else None,
                node_name=self._current_node,
                agent_name=self.agent_name,
            )

            try:
                await self._transcript_collector.add(detailed_event)
            except Exception as e:
                logger.warning(f"Failed to persist SQL query event: {e}")

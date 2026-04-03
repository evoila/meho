# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Chat streaming bridge for agent execution.

This module provides:
1. Streaming wrappers that convert AgentEvent to SSE dict format
2. Session state management for multi-turn conversations
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.agents.persistence import (
    AgentStateStore,
    OrchestratorSessionState,
)

if TYPE_CHECKING:
    from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent

logger = get_logger(__name__)


def _format_conversation_history(history: list[dict[str, Any]]) -> str:
    """Format conversation history as text for agent context.

    Phase 39: User messages with a sender_name are prefixed so the agent
    can address users by name in war room sessions.

    Args:
        history: List of message dicts with 'role', 'content', and
            optional 'sender_name' keys.

    Returns:
        Formatted string of conversation history.
    """
    if not history:
        return ""

    lines = []
    for msg in history:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        sender_name = msg.get("sender_name")
        if role == "USER" and sender_name:
            lines.append(f"USER ({sender_name}): {content}")
        else:
            lines.append(f"{role}: {content}")

    return "\n".join(lines)


def _convert_event_to_sse_format(event: Any) -> dict[str, Any]:
    """Convert AgentEvent to SSE-compatible dict format.

    Flattens AgentEvent into a simple dict for Server-Sent Events:
        {"type": "thought", "content": "..."}

    AgentEvent carries additional metadata (agent, timestamp, step, node)
    which is stripped for the SSE stream -- only type + data are sent.

    Args:
        event: AgentEvent from OrchestratorAgent.

    Returns:
        Flat dict suitable for JSON serialization in SSE stream.
    """
    result = {"type": event.type}
    result.update(event.data)
    return result


# =============================================================================
# Orchestrator Streaming
# =============================================================================


async def run_orchestrator_streaming(  # NOSONAR (cognitive complexity)
    agent: OrchestratorAgent,
    user_message: str,
    session_id: str | None,
    conversation_history: list[dict[str, Any]],
    state_store: AgentStateStore | None = None,
    session_mode: str = "agent",
) -> AsyncIterator[dict[str, Any]]:
    """Run OrchestratorAgent with streaming, converting events to SSE format.

    Handles session state lifecycle: loads state at start, updates during
    streaming based on connector events, saves state on completion (even on error).

    Args:
        agent: The OrchestratorAgent instance.
        user_message: The user's input message.
        session_id: Optional session ID for conversation tracking.
        conversation_history: List of previous messages for context.
        state_store: Optional AgentStateStore for state persistence.
            If provided with a session_id, state will be loaded at start
            and saved at end of streaming.
        session_mode: Session mode ("ask" or "agent"). Phase 65.

    Yields:
        Dict events in SSE-compatible format for streaming.
    """
    # Load or create session state
    session_state: OrchestratorSessionState | None = None
    if session_id and state_store:
        try:
            session_state = await state_store.load_state(session_id)
            if session_state:
                logger.info(
                    f"Loaded session state for {session_id[:8]}... "
                    f"(turn {session_state.turn_count}, {len(session_state.connectors)} connectors)"
                )
            else:
                session_state = OrchestratorSessionState()
                logger.info(f"Created new session state for {session_id[:8]}...")
        except Exception as e:
            logger.warning(f"Failed to load session state: {e}. Creating fresh state.")
            session_state = OrchestratorSessionState()
    else:
        # No session_id or state_store - work without persistence
        session_state = OrchestratorSessionState()

    # Format conversation history for the agent context
    history_text = _format_conversation_history(conversation_history)

    # Add session context to history if we have prior state
    if session_state and session_state.turn_count > 0:
        state_context = session_state.get_context_summary()
        if state_context != "New conversation":
            history_text = f"[Session context: {state_context}]\n\n{history_text}"
            logger.info(f"Added session context to history: {state_context[:100]}...")

    logger.info(f"Running OrchestratorAgent with message: {user_message[:50]}...")
    logger.info(f"Session ID: {session_id}, History: {len(conversation_history)} messages")

    try:
        # Stream from the orchestrator
        async for event in agent.run_streaming(
            user_message=user_message,
            session_id=session_id,
            context={
                "history": history_text,
                "session_state": session_state,
                "session_mode": session_mode,
            },
        ):
            # Update session state based on events
            if session_state:
                _update_state_from_event(session_state, event)

            # Convert AgentEvent to SSE dict format
            sse_event = _convert_event_to_sse_format(event)
            yield sse_event

        logger.info("OrchestratorAgent streaming complete")

    finally:
        # Always save state at the end (even on error)
        if session_id and state_store and session_state:
            try:
                await state_store.save_state(session_id, session_state)
                logger.info(
                    f"Saved session state for {session_id[:8]}... "
                    f"(turn {session_state.turn_count}, {len(session_state.connectors)} connectors)"
                )
            except Exception as e:
                logger.error(f"Failed to save session state: {e}")


def _update_state_from_event(
    state: OrchestratorSessionState,
    event: Any,
) -> None:
    """Update session state based on agent events.

    This tracks connector usage and errors for multi-turn context.

    Args:
        state: The session state to update.
        event: AgentEvent from OrchestratorAgent.
    """
    event_type = event.type
    data = event.data

    if event_type == "connector_complete":
        # Track successful connector usage
        connector_id = data.get("connector_id", "")
        if connector_id:
            state.remember_connector(
                connector_id=connector_id,
                connector_name=data.get("connector_name", "Unknown"),
                connector_type=data.get("connector_type", "unknown"),
                query=data.get("query"),
                status=data.get("status", "success"),
            )
            logger.debug(f"Remembered connector: {connector_id}")

    elif event_type == "error":
        # Track errors for avoiding retries
        connector_id = data.get("connector_id")
        if connector_id:
            state.record_error(
                connector_id=connector_id,
                error_type=data.get("error_type", "unknown"),
                message=data.get("message", ""),
            )
            logger.debug(f"Recorded error for connector: {connector_id}")

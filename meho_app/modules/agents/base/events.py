# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""SSE Event Definitions for MEHO Agents.

All events that can be streamed to the frontend are defined here.
This serves as:
1. Type safety for event data
2. Documentation of available events
3. Single source of truth for frontend/backend contract

QUICK REFERENCE (see EventRegistry for full list):
+----------------------+-------------------------------------------------+
| Event Type           | Purpose                                         |
+----------------------+-------------------------------------------------+
| agent_start          | Agent begins processing                         |
| agent_complete       | Agent finished (success or error)               |
| thought              | LLM reasoning/thinking                          |
| action               | Tool about to be called                         |
| observation          | Tool result received                            |
| final_answer         | Response ready for user                         |
| approval_required    | Dangerous action needs approval                 |
| error                | Something went wrong                            |
| progress             | Generic progress update                         |
| step_progress        | ReAct step counter (step N/max)                  |
| tool_start           | Tool execution starting                         |
| tool_complete        | Tool execution finished                         |
| node_enter           | Entering a graph node                           |
| node_exit            | Exiting a graph node                            |
| orchestrator_start   | Orchestrator begins processing (TASK-181)       |
| orchestrator_complete| Orchestrator finished                           |
| iteration_start      | Orchestrator iteration starting                 |
| iteration_complete   | Orchestrator iteration finished                 |
| dispatch_start       | Parallel dispatch to connectors starting        |
| connector_complete   | Single connector agent finished                 |
| early_findings       | Partial findings while agents still running     |
| synthesis_start      | Final answer synthesis starting                 |
| synthesis_chunk      | Progressive synthesis text chunk                |
| usage_summary        | Token usage + cost at end of conversation       |
| agent_event          | Wrapped event from child agent                  |
| hypothesis_update    | Hypothesis status tracking during investigation |
| orchestrator_plan    | Investigation plan with classification/strategy |
| orchestrator_thinking| Routing LLM chain-of-thought streaming (Phase 110) |
| follow_up_suggestions| Follow-up question suggestions after synthesis  |
| citation_map         | Citation marker -> data_ref mapping             |
+----------------------+-------------------------------------------------+
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

# All valid event types
EventType = Literal[
    # Agent lifecycle
    "agent_start",
    "agent_complete",
    # ReAct loop
    "thought",
    "action",
    "observation",
    "final_answer",
    # Approval flow
    "approval_required",
    "approval_resolved",
    # Errors
    "error",
    "warning",
    # Progress
    "progress",
    "step_progress",
    "budget_extended",  # Step budget extension granted (Phase 36)
    # Tool lifecycle
    "tool_start",
    "tool_complete",
    "tool_error",
    # Node lifecycle (for debugging/tracing)
    "node_enter",
    "node_exit",
    # Orchestrator events (TASK-181)
    "orchestrator_start",
    "orchestrator_complete",
    "iteration_start",
    "iteration_complete",
    "dispatch_start",
    "connector_complete",
    "early_findings",
    "synthesis_start",
    "synthesis_chunk",  # Progressive synthesis streaming
    "usage_summary",  # Token usage + cost at end of conversation
    "agent_event",  # Wrapped event from child agent
    "orchestrator_plan",  # Phase 99: Investigation plan with classification/strategy
    "orchestrator_thinking",  # Phase 110: Routing LLM chain-of-thought streaming
    # Phase 62: Investigation visualization
    "hypothesis_update",  # Hypothesis status tracking (CHUX-01)
    "follow_up_suggestions",  # Suggested follow-up questions (CHUX-04)
    "citation_map",  # Citation marker -> data_ref mapping (CHUX-02)
    # Internal / extended events
    "keepalive",  # SSE keepalive ping
    "audit_entry",  # Audit trail entry
    "tool_call",  # Tool call event (raw)
    "knowledge_search_start",  # Knowledge search beginning (ask mode)
    "knowledge_search_complete",  # Knowledge search finished (ask mode)
]


@dataclass
class AgentEvent:
    """Base event emitted during agent execution.

    All events follow this structure for SSE streaming.

    Attributes:
        type: The event type (from EventType).
        agent: Which agent emitted this (e.g., "react", "k8s").
        data: Event-specific data payload.
        timestamp: When the event was created.
        session_id: Optional session ID for correlation.
        step: Optional ReAct step number.
        node: Optional current node name.
    """

    type: EventType
    agent: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Optional metadata
    session_id: str | None = None
    step: int | None = None
    node: str | None = None

    def to_sse(self) -> str:
        """Format as Server-Sent Event string.

        Returns:
            SSE-formatted string with data: prefix and double newline.
        """
        payload: dict[str, Any] = {
            "type": self.type,
            "agent": self.agent,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.session_id:
            payload["session_id"] = self.session_id
        if self.step is not None:
            payload["step"] = self.step
        if self.node:
            payload["node"] = self.node
        return f"data: {json.dumps(payload)}\n\n"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary.

        Returns:
            Dictionary representation of the event.
        """
        result = asdict(self)
        # Convert datetime to ISO string for serialization
        result["timestamp"] = self.timestamp.isoformat()
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Typed event factory functions for common events
# ─────────────────────────────────────────────────────────────────────────────


def thought_event(
    agent: str,
    content: str,
    **kwargs: Any,
) -> AgentEvent:
    """Create a thought event (LLM reasoning).

    Args:
        agent: Agent name.
        content: The thought content.
        **kwargs: Additional AgentEvent attributes.

    Returns:
        AgentEvent with type="thought".
    """
    return AgentEvent(
        type="thought",
        agent=agent,
        data={"content": content},
        **kwargs,
    )


def action_event(
    agent: str,
    tool: str,
    args: dict[str, Any],
    **kwargs: Any,
) -> AgentEvent:
    """Create an action event (tool about to be called).

    Args:
        agent: Agent name.
        tool: Tool name being called.
        args: Tool arguments.
        **kwargs: Additional AgentEvent attributes.

    Returns:
        AgentEvent with type="action".
    """
    return AgentEvent(
        type="action",
        agent=agent,
        data={"tool": tool, "args": args},
        **kwargs,
    )


def observation_event(
    agent: str,
    tool: str,
    result: Any,
    **kwargs: Any,
) -> AgentEvent:
    """Create an observation event (tool result).

    Args:
        agent: Agent name.
        tool: Tool name that was called.
        result: Tool execution result.
        **kwargs: Additional AgentEvent attributes.

    Returns:
        AgentEvent with type="observation".
    """
    return AgentEvent(
        type="observation",
        agent=agent,
        data={"tool": tool, "result": result},
        **kwargs,
    )


def final_answer_event(
    agent: str,
    content: str,
    **kwargs: Any,
) -> AgentEvent:
    """Create a final answer event.

    Args:
        agent: Agent name.
        content: The final answer content.
        **kwargs: Additional AgentEvent attributes.

    Returns:
        AgentEvent with type="final_answer".
    """
    return AgentEvent(
        type="final_answer",
        agent=agent,
        data={"content": content},
        **kwargs,
    )


def error_event(
    agent: str,
    message: str,
    details: dict[str, Any] | None = None,
    **kwargs: Any,
) -> AgentEvent:
    """Create an error event.

    Args:
        agent: Agent name.
        message: Error message.
        details: Optional additional error details.
        **kwargs: Additional AgentEvent attributes.

    Returns:
        AgentEvent with type="error".
    """
    data: dict[str, Any] = {"message": message}
    if details:
        data["details"] = details
    return AgentEvent(
        type="error",
        agent=agent,
        data=data,
        **kwargs,
    )


def approval_required_event(
    agent: str,
    tool: str,
    args: dict[str, Any],
    danger_level: str,
    description: str,
    **kwargs: Any,
) -> AgentEvent:
    """Create an approval required event.

    Args:
        agent: Agent name.
        tool: Tool requiring approval.
        args: Tool arguments.
        danger_level: Level of danger (safe/caution/dangerous/critical).
        description: Human-readable description of the action.
        **kwargs: Additional AgentEvent attributes.

    Returns:
        AgentEvent with type="approval_required".
    """
    return AgentEvent(
        type="approval_required",
        agent=agent,
        data={
            "tool": tool,
            "args": args,
            "danger_level": danger_level,
            "description": description,
        },
        **kwargs,
    )


def tool_start_event(
    agent: str,
    tool: str,
    **kwargs: Any,
) -> AgentEvent:
    """Create a tool start event.

    Args:
        agent: Agent name.
        tool: Tool name starting.
        **kwargs: Additional AgentEvent attributes.

    Returns:
        AgentEvent with type="tool_start".
    """
    return AgentEvent(
        type="tool_start",
        agent=agent,
        data={"tool": tool},
        **kwargs,
    )


def tool_complete_event(
    agent: str,
    tool: str,
    success: bool = True,
    **kwargs: Any,
) -> AgentEvent:
    """Create a tool complete event.

    Args:
        agent: Agent name.
        tool: Tool name that completed.
        success: Whether the tool succeeded.
        **kwargs: Additional AgentEvent attributes.

    Returns:
        AgentEvent with type="tool_complete".
    """
    return AgentEvent(
        type="tool_complete",
        agent=agent,
        data={"tool": tool, "success": success},
        **kwargs,
    )


def node_enter_event(
    agent: str,
    node_name: str,
    **kwargs: Any,
) -> AgentEvent:
    """Create a node enter event.

    Args:
        agent: Agent name.
        node_name: Name of node being entered.
        **kwargs: Additional AgentEvent attributes.

    Returns:
        AgentEvent with type="node_enter".
    """
    return AgentEvent(
        type="node_enter",
        agent=agent,
        data={"node": node_name},
        node=node_name,
        **kwargs,
    )


def node_exit_event(
    agent: str,
    node_name: str,
    next_node: str | None = None,
    **kwargs: Any,
) -> AgentEvent:
    """Create a node exit event.

    Args:
        agent: Agent name.
        node_name: Name of node being exited.
        next_node: Name of next node (if any).
        **kwargs: Additional AgentEvent attributes.

    Returns:
        AgentEvent with type="node_exit".
    """
    return AgentEvent(
        type="node_exit",
        agent=agent,
        data={"node": node_name, "next_node": next_node},
        node=node_name,
        **kwargs,
    )


def agent_start_event(
    agent: str,
    user_message: str,
    **kwargs: Any,
) -> AgentEvent:
    """Create an agent start event.

    Args:
        agent: Agent name.
        user_message: The user's input message.
        **kwargs: Additional AgentEvent attributes.

    Returns:
        AgentEvent with type="agent_start".
    """
    return AgentEvent(
        type="agent_start",
        agent=agent,
        data={"user_message": user_message},
        **kwargs,
    )


def agent_complete_event(
    agent: str,
    success: bool = True,
    **kwargs: Any,
) -> AgentEvent:
    """Create an agent complete event.

    Args:
        agent: Agent name.
        success: Whether the agent completed successfully.
        **kwargs: Additional AgentEvent attributes.

    Returns:
        AgentEvent with type="agent_complete".
    """
    return AgentEvent(
        type="agent_complete",
        agent=agent,
        data={"success": success},
        **kwargs,
    )


def progress_event(
    agent: str,
    message: str,
    percentage: int | None = None,
    **kwargs: Any,
) -> AgentEvent:
    """Create a progress event.

    Args:
        agent: Agent name.
        message: Progress message.
        percentage: Optional completion percentage (0-100).
        **kwargs: Additional AgentEvent attributes.

    Returns:
        AgentEvent with type="progress".
    """
    data: dict[str, Any] = {"message": message}
    if percentage is not None:
        data["percentage"] = percentage
    return AgentEvent(
        type="progress",
        agent=agent,
        data=data,
        **kwargs,
    )

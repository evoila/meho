# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Shared Agent State - Common state classes for all agent types.

This module defines shared state dataclasses used during a single request
within a specific connector context. These are used by specialist_agent
and react_agent to avoid code duplication.

Classes:
    WorkflowState: For the deterministic workflow (nodes-based)
    WorkflowResult: Result of the deterministic workflow
    BaseAgentState: Base class for ReAct loop state (extended by agents)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meho_app.modules.agents.persistence import OrchestratorSessionState


@dataclass
class WorkflowState:
    """State for deterministic workflow (nodes-based).

    Simple state that accumulates results as nodes execute.
    Each node can read/write to this state.

    Attributes:
        user_goal: The original user message/question.
        connector_id: The connector this agent is scoped to.
        connector_name: Human-readable connector name.
        steps_executed: List of step descriptions for debugging.
        session_state: Persistent session state for multi-turn context.
        session_id: Session ID for multi-turn context awareness.
        cached_tables: Info about cached tables from previous queries.
            Format: {"table_name": {"row_count": N, "columns": [...]}, ...}
    """

    user_goal: str
    connector_id: str = ""
    connector_name: str = ""
    steps_executed: list[str] = field(default_factory=list)
    session_state: OrchestratorSessionState | None = None

    # Session context for multi-turn awareness
    session_id: str | None = None
    cached_tables: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowResult:
    """Result of the deterministic workflow."""

    success: bool
    findings: str
    steps_executed: list[str]
    error: str | None = None


@dataclass
class BaseAgentState:
    """Base state for connector-scoped ReAct reasoning loop (ephemeral - one request).

    This state is created at the start of each request and discarded at the end.
    It tracks the agent's progress through the ReAct loop within a specific connector.

    Subclasses can extend this with additional fields if needed.

    Attributes:
        user_goal: The original user message/question.
        connector_id: The connector this agent is scoped to.
        connector_name: Human-readable connector name.
        connector_type: Type of connector (kubernetes, vmware, etc.).
        routing_description: Description for routing decisions.
        iteration: Which orchestrator iteration this is part of.
        prior_findings: Findings from previous iterations for context.
        scratchpad: Accumulated thoughts and observations for this request.
        step_count: Number of Action->Observation cycles completed.
        pending_tool: Tool name that needs to be executed.
        pending_args: Arguments for the pending tool.
        last_observation: Result of the most recent tool execution.
        final_answer: The final response to return.
        error_message: Error message if something went wrong.
        approval_granted: Whether user approval has been granted.
    """

    # User input
    user_goal: str

    # Connector scoping
    connector_id: str
    connector_name: str = ""
    connector_type: str = ""  # kubernetes, vmware, etc.
    routing_description: str = ""
    iteration: int = 1
    prior_findings: list[str] = field(default_factory=list)

    # ReAct loop state
    scratchpad: list[str] = field(default_factory=list)
    step_count: int = 0
    pending_tool: str | None = None
    pending_args: dict[str, Any] | None = None
    last_observation: str | None = None
    final_answer: str | None = None

    # Error handling
    error_message: str | None = None

    # Approval flow
    approval_granted: bool = False

    def add_to_scratchpad(self, entry: str) -> None:
        """Append an entry to the scratchpad."""
        self.scratchpad.append(entry)

    def get_scratchpad_text(self) -> str:
        """Return the scratchpad as a formatted string."""
        return "\n".join(self.scratchpad)

    def clear_pending_action(self) -> None:
        """Clear the pending tool and arguments after execution."""
        self.pending_tool = None
        self.pending_args = None

    def is_complete(self) -> bool:
        """Check if the agent has finished processing."""
        return self.final_answer is not None or self.error_message is not None

    def get_prior_findings_text(self) -> str:
        """Format prior findings for prompt context."""
        if not self.prior_findings:
            return "No prior findings from previous iterations."

        lines = [
            f"[Previous Finding {i + 1}]: {finding}"
            for i, finding in enumerate(self.prior_findings)
        ]
        return "\n".join(lines)

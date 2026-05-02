# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""React Agent State - Ephemeral state for the ReAct reasoning loop.

This module defines the state dataclass used during a single request.
The state tracks the current goal, scratchpad, pending actions, and results.

Note: This module depends on base/ only (no config/, sse/, or other modules).

Example:
    >>> state = ReactAgentState(user_goal="List all VMs")
    >>> state.add_to_scratchpad("Thought: I need to find connectors first")
    >>> state.add_to_scratchpad("Action: list_connectors")
    >>> print(state.get_scratchpad_text())
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReactAgentState:
    """State for the ReAct reasoning loop (ephemeral - one request).

    This state is created at the start of each request and discarded at the end.
    It tracks the agent's progress through the ReAct loop.

    Attributes:
        user_goal: The original user message/question.
        scratchpad: Accumulated thoughts and observations for this request.
        step_count: Number of Action->Observation cycles completed.
        pending_tool: Tool name that needs to be executed.
        pending_args: Arguments for the pending tool.
        last_observation: Result of the most recent tool execution.
        final_answer: The final response to return to user.
        error_message: Error message if something went wrong.

    Example:
        >>> state = ReactAgentState(user_goal="List all namespaces")
        >>> state.step_count = 1
        >>> state.pending_tool = "list_connectors"
        >>> state.pending_args = {}
    """

    # User input
    user_goal: str

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
    approval_granted: bool = False  # Set by service layer when user approves

    def add_to_scratchpad(self, entry: str) -> None:
        """Append an entry to the scratchpad.

        Args:
            entry: The text to append (thought, action, or observation).

        Example:
            >>> state.add_to_scratchpad("Thought: I should search for VMs")
            >>> state.add_to_scratchpad("Action: search_operations")
        """
        self.scratchpad.append(entry)

    def get_scratchpad_text(self) -> str:
        """Return the scratchpad as a formatted string.

        Returns:
            All scratchpad entries joined with newlines.

        Example:
            >>> state.add_to_scratchpad("Thought: test")
            >>> state.get_scratchpad_text()
            'Thought: test'
        """
        return "\n".join(self.scratchpad)

    def clear_pending_action(self) -> None:
        """Clear the pending tool and arguments after execution."""
        self.pending_tool = None
        self.pending_args = None

    def is_complete(self) -> bool:
        """Check if the agent has finished processing.

        Returns:
            True if there's a final answer or error, False otherwise.
        """
        return self.final_answer is not None or self.error_message is not None

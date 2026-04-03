# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Specialist Agent State - Ephemeral state for connector-scoped agents.

This module defines the SpecialistReActState for the ReAct loop, and
re-exports shared state classes for backward compatibility.

Classes:
    SpecialistReActState: State for the specialist agent's ReAct loop
    WorkflowState: Legacy deterministic workflow state (deprecated -- kept for imports)
    WorkflowResult: Result of the deterministic workflow - from shared
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meho_app.modules.agents.persistence import OrchestratorSessionState

from meho_app.modules.agents.shared import WorkflowResult
from meho_app.modules.agents.specialist_agent.summarizer import StepRecord

__all__ = [
    "SpecialistReActState",
    "WorkflowResult",
    "WorkflowState",
]

# Circuit breaker limit for individual observations (memory safety).
# Models have 1M token context windows, so we do NOT truncate aggressively.
# This limit only protects against runaway SQL results or bad API responses.
_OBSERVATION_CIRCUIT_BREAKER = 200_000


@dataclass
class SpecialistReActState:
    """State for the specialist agent's ReAct loop.

    Replaces the deterministic WorkflowState with a dynamic investigation
    model. The LLM decides what tools to call, in what order, and when to
    stop. The scratchpad passes full observations to the LLM (no aggressive
    truncation) -- models have 1M token context windows.

    Attributes:
        user_goal: The original user message/question.
        scratchpad: Accumulated thoughts and observations (flat list).
        completed_steps: Structured step records for step tracking.
        window_size: Number of recent steps to keep at full detail (min 2).
        step_count: Number of action-observe cycles completed.
        max_steps: Budget limit (default 8, extendable to 12 via extend_budget).
        budget_extended: Whether the +4 step extension has been granted (once only).
        final_answer: Set when the LLM decides it has enough info.
        error_message: Set on unrecoverable error.
        message_history: PydanticAI message objects for stateful agent.run() (Phase 35).
        connector_id: The connector this agent is scoped to.
        connector_name: Human-readable connector name.
        skill_content: Markdown skill content for domain knowledge injection.
        session_id: Session ID for multi-turn context awareness.
        session_state: Persistent session state for multi-turn context.
        cached_tables: Info about cached tables from previous queries.
        discovered_correlations: SAME_AS entity mappings from topology lookups.
        discovered_entities: Key entities from operation results (cross-system).
        steps_executed: List of step descriptions for debugging.
        action_history: List of (tool, args_hash) tuples for loop detection.
    """

    # Core ReAct
    user_goal: str
    scratchpad: list[str] = field(default_factory=list)
    completed_steps: list[StepRecord] = field(default_factory=list)
    window_size: int = 3  # Recent steps at full detail (min: 2)
    step_count: int = 0
    max_steps: int = 8  # Default budget (Phase 36: reduced from 15)
    budget_extended: bool = False  # Whether +4 extension has been granted (Phase 36)
    final_answer: str | None = None
    error_message: str | None = None

    # Stateful message history (Phase 35) -- accumulated PydanticAI messages
    # for incremental conversation. Each agent.run() returns all_messages()
    # which is captured and passed to the next agent.run() call.
    message_history: list[Any] = field(default_factory=list)

    # Specialist context (connector scoping)
    connector_id: str = ""
    connector_name: str = ""
    skill_content: str = ""

    # Session context (preserved from current state)
    session_id: str | None = None
    session_state: Any = None
    cached_tables: dict[str, Any] = field(default_factory=dict)

    # Investigation intelligence (Phase 80: persistent state block)
    discovered_correlations: list[dict] = field(default_factory=list)
    discovered_entities: list[dict] = field(default_factory=list)

    # Tracking
    steps_executed: list[str] = field(default_factory=list)
    action_history: list[tuple[str, str]] = field(
        default_factory=list
    )  # (tool, args_hash) for loop detection

    def __post_init__(self) -> None:
        """Enforce minimum window size of 2."""
        self.window_size = max(self.window_size, 2)

    def add_observation(self, tool: str, result: str, action_input_key: str = "") -> None:
        """Append action and observation to the scratchpad.

        Populates both the flat scratchpad list (for get_observations_summary()
        and circuit breaker) and the structured completed_steps list (for step
        tracking and potential future windowed rendering).

        Does NOT truncate aggressively -- our target models (Opus 4.6, Sonnet
        4.6) have 1M token context windows. Applies only a circuit-breaker
        limit of 200,000 characters to prevent memory crashes from bad SQL
        queries; otherwise passes the full observation through.

        This is critical for reduce_data results: the LLM must see the
        complete filtered dataset it asked for.

        Args:
            tool: The tool name that was called.
            result: The raw observation text from the tool.
            action_input_key: Primary identifying parameter value for summaries.
        """
        # Circuit breaker only -- no aggressive truncation
        if len(result) > _OBSERVATION_CIRCUIT_BREAKER:
            truncated = result[:_OBSERVATION_CIRCUIT_BREAKER]
            result = (
                f"{truncated}\n\n"
                f"[TRUNCATED: observation was {len(result):,} chars, "
                f"showing first {_OBSERVATION_CIRCUIT_BREAKER:,} for memory safety]"
            )

        # Flat scratchpad (for get_observations_summary + circuit breaker)
        self.scratchpad.append(f"Action: {tool}")
        self.scratchpad.append(f"Observation: {result}")

        # Structured step tracking (Phase 34)
        step = StepRecord(
            step_number=len(self.completed_steps) + 1,
            tool=tool,
            action_input_key=action_input_key,
            observation=result,
        )
        self.completed_steps.append(step)

    def is_complete(self) -> bool:
        """Check if the agent has finished processing.

        Returns:
            True if final_answer or error_message is set.
        """
        return self.final_answer is not None or self.error_message is not None

    def has_duplicate_action(self, tool: str, action_input: dict) -> bool:
        """Check if this exact tool+args combination has been called before.

        Prevents infinite loops where the LLM calls the same tool with the
        same arguments repeatedly (Research Pitfall 2).

        Args:
            tool: The tool name.
            action_input: The tool arguments dict.

        Returns:
            True if this exact action was already recorded.
        """
        args_hash = hashlib.md5(json.dumps(action_input, sort_keys=True).encode()).hexdigest()  # noqa: S324 -- non-security hash context
        return (tool, args_hash) in self.action_history

    def record_action(self, tool: str, action_input: dict) -> None:
        """Record a tool+args combination for loop detection.

        Args:
            tool: The tool name.
            action_input: The tool arguments dict.
        """
        args_hash = hashlib.md5(json.dumps(action_input, sort_keys=True).encode()).hexdigest()  # noqa: S324 -- non-security hash context
        self.action_history.append((tool, args_hash))

    def get_scratchpad_text(self) -> str:
        """Return the scratchpad as a two-section windowed format.

        Completed steps (older than window) show one-line summaries.
        Recent steps (within window) show full detail.
        Both sections are always present for consistent LLM parsing.

        Returns:
            Two-section windowed scratchpad, or empty string if no steps.
        """
        if not self.completed_steps:
            return ""

        boundary = max(0, len(self.completed_steps) - self.window_size)
        summarized = self.completed_steps[:boundary]
        recent = self.completed_steps[boundary:]

        # Header line
        if summarized:
            last_summarized = summarized[-1].step_number
            first_recent = recent[0].step_number if recent else last_summarized + 1
            last_recent = recent[-1].step_number if recent else first_recent
            header = f"Steps 1-{last_summarized} summarized | Steps {first_recent}-{last_recent} full detail"
        else:
            first_recent = recent[0].step_number
            last_recent = recent[-1].step_number
            header = f"Steps (none summarized) | Steps {first_recent}-{last_recent} full detail"

        # Completed section
        if summarized:
            completed_lines = [
                step.summary or f"Step {step.step_number}: {step.tool} -> (summary pending)"
                for step in summarized
            ]
            completed_section = "\n".join(completed_lines)
        else:
            completed_section = "(No completed steps yet)"

        # Recent section
        recent_lines = []
        for step in recent:
            recent_lines.append(
                f"Step {step.step_number}:\nAction: {step.tool}\nObservation: {step.observation}"
            )
        recent_section = "\n\n".join(recent_lines)

        return (
            f"{header}\n\n"
            f"<completed_steps>\n{completed_section}\n</completed_steps>\n\n"
            f"<recent_steps>\n{recent_section}\n</recent_steps>"
        )

    def get_observations_summary(self) -> str:
        """Return a formatted summary of all observations.

        Used for best-effort budget exhaustion answer. Extracts only the
        observation entries from the scratchpad.

        Returns:
            Formatted summary of all observations.
        """
        observations = []
        for i, entry in enumerate(self.scratchpad):
            if entry.startswith("Observation: "):
                # Find the preceding action
                action = ""
                if i > 0 and self.scratchpad[i - 1].startswith("Action: "):
                    action = self.scratchpad[i - 1].replace("Action: ", "")
                obs_text = entry.replace("Observation: ", "", 1)
                observations.append(f"### {action}\n{obs_text}")

        if not observations:
            return "No observations were collected."

        return "\n\n".join(observations)


# ──────────────────────────────────────────────────────────────────────────────
# Legacy state (deprecated -- kept for backward compatibility during migration)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class WorkflowState:
    """State for deterministic workflow with skill content injection.

    .. deprecated::
        Use SpecialistReActState instead. This class is retained only for
        backward compatibility with imports from flow.py and nodes/.

    Attributes:
        user_goal: The original user message/question.
        connector_id: The connector this agent is scoped to.
        connector_name: Human-readable connector name.
        steps_executed: List of step descriptions for debugging.
        session_state: Persistent session state for multi-turn context.
        session_id: Session ID for multi-turn context awareness.
        cached_tables: Info about cached tables from previous queries.
        skill_content: Markdown skill content for domain knowledge injection.
    """

    user_goal: str
    connector_id: str = ""
    connector_name: str = ""
    steps_executed: list[str] = field(default_factory=list)
    session_state: OrchestratorSessionState | None = None

    # Session context for multi-turn awareness
    session_id: str | None = None
    cached_tables: dict[str, Any] = field(default_factory=dict)

    # Skill content for node-level prompt injection
    skill_content: str = ""

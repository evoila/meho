# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""State management for the Orchestrator Agent.

Contains dataclasses for tracking orchestrator state across iterations:
- ConnectorSelection: A connector selected for querying
- OrchestratorState: Full state maintained across the orchestrator loop
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput
    from meho_app.modules.agents.persistence import OrchestratorSessionState


@dataclass
class ConnectorSelection:
    """A connector selected for querying in an iteration.

    Contains metadata about why the connector was selected and what
    to investigate.

    Attributes:
        connector_id: UUID of the connector
        connector_name: Human-readable connector name
        connector_type: Type of connector (kubernetes, vmware, etc.)
        routing_description: What the connector manages (used for LLM context)
        relevance_score: How relevant the connector is to the query (0.0-1.0)
        reason: Why the orchestrator selected this connector
        generated_skill: Auto-generated skill content from DB (Phase 7.1)
        custom_skill: User-edited skill content from DB (Phase 7.1)
        skill_name: Skill filename from DB (Phase 7.1)
    """

    connector_id: str
    connector_name: str
    connector_type: str
    routing_description: str
    relevance_score: float
    reason: str
    # Skill fields (Phase 7.1: wired from _get_available_connectors)
    generated_skill: str | None = None
    custom_skill: str | None = None
    skill_name: str | None = None
    # Connection details for error messaging (Phase 23)
    base_url: str | None = None
    # Phase 99: Tiered dispatch fields from routing classification
    priority: int = 1  # 1 = dispatch immediately, 2 = conditional
    max_steps: int | None = None  # Per-specialist step budget override (None = use default)
    conditional: bool = False  # Whether this connector is conditionally dispatched
    # Phase 32: Investigation context for follow-up rounds (internal orchestrator state)
    _investigation_context: dict[str, Any] | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "connector_id": self.connector_id,
            "connector_name": self.connector_name,
            "connector_type": self.connector_type,
            "routing_description": self.routing_description,
            "relevance_score": self.relevance_score,
            "reason": self.reason,
            "base_url": self.base_url,
            "priority": self.priority,
            "max_steps": self.max_steps,
            "conditional": self.conditional,
        }


@dataclass
class OrchestratorState:
    """State maintained across the orchestrator loop.

    Tracks the user's goal, iteration progress, accumulated findings,
    and final answer.

    Attributes:
        user_goal: The original user question/goal
        session_id: Optional session ID for conversation tracking
        conversation_history: Recent conversation messages for multi-turn context
        current_iteration: Current iteration number (0-indexed internally)
        max_iterations: Maximum iterations before forcing a response
        selected_connectors: Connectors selected for the current iteration
        all_findings: Accumulated findings across all iterations
        final_answer: The synthesized final answer (set when complete)
        should_continue: Whether to continue iterating
        start_time_ms: When the orchestrator started (epoch milliseconds)
        session_state: Persistent session state for multi-turn context (Phase 3)
    """

    user_goal: str
    session_id: str | None = None
    conversation_history: list[dict[str, str]] | None = None
    current_iteration: int = 0
    max_iterations: int = 3
    selected_connectors: list[ConnectorSelection] = field(default_factory=list)
    all_findings: list[SubgraphOutput] = field(default_factory=list)
    final_answer: str | None = None
    should_continue: bool = True
    start_time_ms: float = 0.0
    session_state: OrchestratorSessionState | None = None
    # Phase 77: Investigation budget and convergence
    dispatch_count: int = 0
    investigation_budget: int = 30  # Set from session_type: 20 automated, 30 interactive
    visited_connectors: dict[str, int] = field(default_factory=dict)  # connector_id -> visit_count
    no_novelty_count: int = 0
    convergence_window: int = 2  # Force synthesis after N consecutive no-novelty dispatches
    budget_exhaustion_reason: str | None = None  # "budget_exhausted" or "converged" or None
    # Phase 99: Deferred connectors for progressive dispatch (standard mode)
    _deferred_connectors: list[ConnectorSelection] = field(default_factory=list)

    def add_iteration_findings(self, outputs: list[SubgraphOutput]) -> None:
        """Add findings from a completed iteration.

        Appends the outputs to all_findings and increments the iteration counter.

        Args:
            outputs: List of SubgraphOutput from the completed iteration
        """
        self.all_findings.extend(outputs)
        self.current_iteration += 1

    def get_findings_summary(self) -> str:
        """Get accumulated findings as text for LLM context.

        Formats all findings with status indicators for use in prompts.

        Returns:
            Formatted string with all findings, or empty string if none.
        """
        if not self.all_findings:
            return ""

        summaries = []
        for finding in self.all_findings:
            # Status indicator
            if finding.status == "success":
                status_icon = "✓"
            elif finding.status == "partial":
                status_icon = "⚠"
            else:
                status_icon = "✗"

            # Format finding
            summary = f"[{status_icon} {finding.connector_name}]: {finding.findings}"
            if finding.error_message:
                summary += f" (Error: {finding.error_message})"

            summaries.append(summary)

        return "\n\n".join(summaries)

    def get_queried_connector_ids(self) -> set[str]:
        """Get set of connector IDs that have already been queried.

        Used to avoid re-querying connectors that already provided findings.

        Returns:
            Set of connector IDs from all_findings.
        """
        return {f.connector_id for f in self.all_findings}

    def has_sufficient_findings(self) -> bool:
        """Check if there are enough findings to attempt an answer.

        Returns True if there's at least one successful finding.

        Returns:
            True if at least one finding has status "success" or "partial".
        """
        return any(f.status in ("success", "partial") for f in self.all_findings)

    def is_last_iteration(self) -> bool:
        """Check if this is the last allowed iteration.

        Returns:
            True if current_iteration + 1 >= max_iterations.
        """
        return self.current_iteration + 1 >= self.max_iterations

    @property
    def successful_findings(self) -> list[SubgraphOutput]:
        """Get only successful findings."""
        # Import here to avoid circular import at module level
        return [f for f in self.all_findings if f.status == "success"]

    @property
    def failed_findings(self) -> list[SubgraphOutput]:
        """Get only failed/errored findings."""
        return [f for f in self.all_findings if f.status in ("failed", "timeout", "cancelled")]

    # Phase 77: Investigation budget and convergence methods

    def record_dispatches(self, connector_ids: list[str]) -> None:
        """Record specialist dispatches against budget.

        Each connector dispatch counts as one unit against the investigation budget.
        Tracks per-connector visit counts for re-query detection.

        Args:
            connector_ids: List of connector IDs dispatched in this round.
        """
        self.dispatch_count += len(connector_ids)
        for cid in connector_ids:
            self.visited_connectors[cid] = self.visited_connectors.get(cid, 0) + 1

    def is_budget_exhausted(self) -> bool:
        """Check if investigation budget is exhausted.

        Returns:
            True if dispatch_count >= investigation_budget.
        """
        return self.dispatch_count >= self.investigation_budget

    def is_converged(self) -> bool:
        """Check if investigation has converged (no new findings).

        Returns:
            True if no_novelty_count >= convergence_window.
        """
        return self.no_novelty_count >= self.convergence_window

    def should_force_synthesis(self) -> bool:
        """Check if synthesis should be forced due to budget or convergence.

        Sets budget_exhaustion_reason when returning True so callers can
        include the reason in the synthesis prompt.

        Returns:
            True if budget exhausted or investigation converged.
        """
        if self.is_budget_exhausted():
            self.budget_exhaustion_reason = "budget_exhausted"
            return True
        if self.is_converged():
            self.budget_exhaustion_reason = "converged"
            return True
        return False

    def get_requery_connectors(self) -> set[str]:
        """Get connector IDs that have been queried more than once.

        Used by the orchestrator to detect re-queries that need justification
        (D-08 re-query justification).

        Returns:
            Set of connector IDs with visit_count > 1.
        """
        return {cid for cid, count in self.visited_connectors.items() if count > 1}

    def remaining_budget(self) -> int:
        """Get remaining dispatch budget.

        Returns:
            Non-negative integer of remaining dispatches.
        """
        return max(0, self.investigation_budget - self.dispatch_count)

    def record_novelty(self, has_novelty: bool) -> None:
        """Record whether the latest dispatch round produced novel findings.

        Resets the no-novelty counter when new information is found,
        increments it when findings are repetitive. Used by convergence
        detection to trigger forced synthesis.

        Args:
            has_novelty: True if new information was found, False if repetitive.
        """
        if has_novelty:
            self.no_novelty_count = 0
        else:
            self.no_novelty_count += 1

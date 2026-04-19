# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Contracts for orchestrator-subagent communication.

These dataclasses define the interface between the orchestrator and
connector-specific agents (subgraphs).

SubgraphInput: What the orchestrator sends to a connector's agent
SubgraphOutput: What a connector's agent returns
WrappedEvent: SSE event wrapped with source metadata
IterationResult: Result of one parallel dispatch iteration
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SubgraphInput:
    """What the orchestrator sends to a connector's agent.

    Contains the goal to investigate and context from previous iterations
    to help the agent focus on what's still needed.

    Attributes:
        goal: What to investigate (the user's question or a focused sub-query)
        connector_id: Which connector to use for this investigation
        iteration: Current iteration number (1-indexed)
        prior_findings: Summaries from previous iterations for context
        entities_of_interest: Specific entities mentioned or discovered
        max_steps: Maximum ReAct steps the subgraph can take
        timeout_seconds: How long the subgraph has to complete
    """

    goal: str
    connector_id: str
    iteration: int = 1
    prior_findings: list[str] = field(default_factory=list)
    entities_of_interest: list[str] = field(default_factory=list)
    max_steps: int = 10
    timeout_seconds: float = 30.0
    # Phase 32: Cross-system investigation context for follow-up rounds
    investigation_context: dict[str, Any] | None = None
    # Structure: {"time_window": {...}, "entities": {...}, "round": int, "triggering_connector": str, "investigation_summary": str}

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubgraphInput:
        """Create from dictionary."""
        return cls(**data)


@dataclass
class SubgraphOutput:
    """What a connector's agent returns.

    Contains the findings from investigating the connector, along with
    metadata about the execution status and any entities discovered.

    Attributes:
        connector_id: Which connector was queried
        connector_name: Human-readable connector name
        findings: Natural language summary of what was found
        status: Execution status (success, partial, failed, timeout)
        confidence: How confident the agent is in its findings (0.0-1.0)
        entities_discovered: Structured entities found during investigation.
            Phase 77: Each entry follows {"name": str, "type": str,
            "identifiers": {"hostname": ..., "ip": ..., "provider_id": ...},
            "connector_id": str | None, "context": str}
        error_message: Error details if status is not success
        execution_time_ms: How long the subgraph took to execute
        data_refs: References to raw data tables for frontend lazy-loading
    """

    connector_id: str
    connector_name: str
    findings: str
    status: str = "success"  # success, partial, failed, timeout, cancelled
    confidence: float = 0.5
    entities_discovered: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None
    execution_time_ms: float = 0.0
    data_refs: list[dict[str, Any]] = field(default_factory=list)
    # Each entry: {"table": "namespaces", "session_id": "abc-123", "row_count": 44}
    # Frontend can call /api/data/{session_id}/{table} to fetch raw data
    # Error classification metadata (Phase 23)
    error_classification: dict[str, Any] | None = None
    # Example: {"error_source": "connector", "error_type": "timeout", "severity": "transient", ...}
    # Phase 32: Cross-system diagnostic handoff
    unresolved_entities: list[dict[str, Any]] = field(default_factory=list)
    # Each entry: {"entity_type": str, "value": str, "context": str, "suggested_domain": str}

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubgraphOutput:
        """Create from dictionary."""
        return cls(**data)

    @property
    def is_success(self) -> bool:
        """Check if execution was successful."""
        return self.status == "success"

    @property
    def is_partial(self) -> bool:
        """Check if execution had partial results."""
        return self.status == "partial"

    @property
    def has_error(self) -> bool:
        """Check if execution had an error."""
        return self.status in ("failed", "timeout", "cancelled")

    @property
    def has_discovered_entities(self) -> bool:
        """Check if specialist discovered entities for cross-system traversal.

        Phase 77: Used by the orchestrator to determine if topology-driven
        traversal should continue to connected systems.

        Returns:
            True if entities_discovered is non-empty.
        """
        return len(self.entities_discovered) > 0


@dataclass
class WrappedEvent:
    """SSE event wrapped with agent source metadata.

    Allows the frontend to attribute events to specific agents/connectors
    when multiple agents are running in parallel.

    Attributes:
        agent_source: Metadata about which agent emitted the event
            - agent_name: Display name (e.g., "specialist_agent_k8s_prod")
            - connector_id: Connector UUID
            - connector_name: Human-readable connector name
            - iteration: Which iteration this event belongs to
        inner_event: The original event from the child agent
            - type: Event type (thought, action, observation, etc.)
            - data: Event-specific data
            - timestamp: When the event was created
    """

    agent_source: dict[str, Any]
    inner_event: dict[str, Any]

    def to_sse(self) -> str:
        """Serialize for SSE streaming.

        Returns:
            SSE-formatted string with data: prefix and double newline.
        """
        payload = {
            "type": "agent_event",
            "agent_source": self.agent_source,
            "inner_event": self.inner_event,
        }
        return f"data: {json.dumps(payload)}\n\n"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "type": "agent_event",
            "agent_source": self.agent_source,
            "inner_event": self.inner_event,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WrappedEvent:
        """Create from dictionary."""
        return cls(
            agent_source=data.get("agent_source", {}),
            inner_event=data.get("inner_event", {}),
        )


@dataclass
class IterationResult:
    """Result of one parallel dispatch iteration.

    Aggregates outputs from all agents that ran in a single iteration.

    Attributes:
        iteration: Which iteration this result is for (1-indexed)
        outputs: List of SubgraphOutput from each agent
        total_time_ms: Total wall-clock time for the iteration
    """

    iteration: int
    outputs: list[SubgraphOutput]
    total_time_ms: float

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "iteration": self.iteration,
            "outputs": [o.to_dict() for o in self.outputs],
            "total_time_ms": self.total_time_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IterationResult:
        """Create from dictionary."""
        return cls(
            iteration=data["iteration"],
            outputs=[SubgraphOutput.from_dict(o) for o in data.get("outputs", [])],
            total_time_ms=data.get("total_time_ms", 0.0),
        )

    @property
    def success_count(self) -> int:
        """Count of successful outputs."""
        return sum(1 for o in self.outputs if o.is_success)

    @property
    def error_count(self) -> int:
        """Count of failed/errored outputs."""
        return sum(1 for o in self.outputs if o.has_error)

    @property
    def all_succeeded(self) -> bool:
        """Check if all outputs were successful."""
        return all(o.is_success for o in self.outputs)

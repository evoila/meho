# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Event wrapper for orchestrator SSE streaming.

Wraps events from child agents with source metadata so the frontend
can attribute events to specific connectors when multiple agents are
running in parallel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from meho_app.modules.agents.orchestrator.contracts import WrappedEvent

if TYPE_CHECKING:
    from meho_app.modules.agents.base.events import AgentEvent


class EventWrapper:
    """Wraps events from child agents with source metadata.

    Used by the orchestrator to tag events with which connector/agent
    they came from, enabling the frontend to show events grouped by source.

    Attributes:
        connector_id: UUID of the connector
        connector_name: Human-readable connector name
        iteration: Which iteration this wrapper is for

    Example:
        >>> wrapper = EventWrapper("k8s-prod", "Production K8s", iteration=1)
        >>> wrapped = wrapper.wrap(agent_event)
        >>> sse_string = wrapped.to_sse()
    """

    def __init__(
        self,
        connector_id: str,
        connector_name: str,
        iteration: int,
    ) -> None:
        """Initialize the event wrapper.

        Args:
            connector_id: UUID of the connector being queried
            connector_name: Human-readable name for display
            iteration: Current orchestrator iteration (1-indexed)
        """
        self.connector_id = connector_id
        self.connector_name = connector_name
        self.iteration = iteration

    def wrap(self, event: AgentEvent) -> WrappedEvent:
        """Wrap an event from a child agent with source metadata.

        Takes an AgentEvent from a child agent and wraps it with
        connector/iteration metadata for SSE streaming.

        Args:
            event: The original event from the child agent

        Returns:
            WrappedEvent with agent_source and inner_event fields
        """
        return WrappedEvent(
            agent_source={
                "agent_name": f"{event.agent}_{self.connector_id}",
                "connector_id": self.connector_id,
                "connector_name": self.connector_name,
                "iteration": self.iteration,
            },
            inner_event={
                "type": event.type,
                "data": event.data,
                "timestamp": event.timestamp.isoformat() if event.timestamp else None,
                "step": event.step,
                "node": event.node,
            },
        )

    def wrap_dict(self, event_dict: dict) -> WrappedEvent:
        """Wrap an event dictionary with source metadata.

        Alternative to wrap() when you have a dict instead of AgentEvent.
        Useful for wrapping events that come from non-standard sources.

        Args:
            event_dict: Dictionary with at least 'type' and 'data' keys

        Returns:
            WrappedEvent with agent_source and inner_event fields
        """
        agent_name = event_dict.get("agent", "unknown")
        return WrappedEvent(
            agent_source={
                "agent_name": f"{agent_name}_{self.connector_id}",
                "connector_id": self.connector_id,
                "connector_name": self.connector_name,
                "iteration": self.iteration,
            },
            inner_event={
                "type": event_dict.get("type", "unknown"),
                "data": event_dict.get("data", {}),
                "timestamp": event_dict.get("timestamp"),
                "step": event_dict.get("step"),
                "node": event_dict.get("node"),
            },
        )

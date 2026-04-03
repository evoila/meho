# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""ApprovalCheckNode - Handles user approval for dangerous operations.

This node is triggered when a tool requires user approval before execution.
It pauses the agent flow and waits for user confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from meho_app.core.otel import get_logger
from meho_app.modules.agents.base.node import BaseNode, NodeResult

if TYPE_CHECKING:
    from meho_app.modules.agents.react_agent.state import ReactAgentState
    from meho_app.modules.agents.sse.emitter import EventEmitter

logger = get_logger(__name__)

# Danger levels for operations
DANGER_LEVELS = {
    "safe": "safe",
    "caution": "caution",
    "dangerous": "dangerous",
    "critical": "critical",
}


@dataclass
class ApprovalCheckNode(BaseNode["ReactAgentState"]):
    """Approval check node - requests user confirmation for dangerous actions.

    This node:
    1. Assesses the danger level of the pending operation
    2. Emits an approval_required event
    3. Pauses execution until user responds
    4. On approval: flow resumes with tool_dispatch
    5. On rejection: flow resumes with reason (LLM adapts)

    Flow resumption is handled by the agent service layer, not this node.
    This node only emits the event and pauses.

    Attributes:
        NODE_NAME: Unique identifier for this node type.
    """

    NODE_NAME: ClassVar[str] = "approval_check"

    async def run(
        self,
        state: ReactAgentState,
        deps: Any,
        emitter: EventEmitter,
    ) -> NodeResult:
        """Check for and handle approval requirements.

        Args:
            state: Current agent state with pending action.
            deps: Agent dependencies (services, config, etc.).
            emitter: SSE event emitter for streaming updates.

        Returns:
            NodeResult with next_node=None (pauses for approval).
        """
        await emitter.node_enter(self.NODE_NAME)

        tool_name = state.pending_tool
        tool_args = state.pending_args or {}

        if not tool_name:
            # Shouldn't happen, but handle gracefully
            logger.warning("ApprovalCheckNode called with no pending tool")
            await emitter.node_exit(self.NODE_NAME, next_node="reason")
            return NodeResult(next_node="reason")

        try:
            # Assess danger level
            danger_level = self._assess_danger_level(tool_name, tool_args)

            # Generate human-readable description
            description = self._generate_description(tool_name, tool_args, danger_level)

            logger.info(f"Requesting approval for {tool_name} (danger: {danger_level})")

            # Emit approval required event
            await emitter.approval_required(
                tool=tool_name,
                args=tool_args,
                danger_level=danger_level,
                description=description,
            )

            # Pause flow - service layer handles resumption
            # When resumed:
            # - If approved: state.approval_granted = True, next_node = "tool_dispatch"
            # - If rejected: next_node = "reason" (LLM will see rejection)
            await emitter.node_exit(self.NODE_NAME, next_node=None)
            return NodeResult(
                next_node=None,
                data={
                    "awaiting_approval": True,
                    "tool": tool_name,
                    "danger_level": danger_level,
                },
            )

        except Exception as e:
            error_msg = f"Approval check error: {e}"
            logger.exception(error_msg)
            await emitter.error(error_msg)
            await emitter.node_exit(self.NODE_NAME, next_node=None)
            return NodeResult(next_node=None, data={"error": error_msg})

    def _assess_danger_level(self, tool_name: str, args: dict[str, Any]) -> str:
        """Classify operation using Phase 5 trust classifier.

        Replaces the old four-tier danger assessment with the unified
        three-tier trust classification pipeline. Returns the trust
        tier value string for backward compatibility.

        Args:
            tool_name: Name of the tool.
            args: Tool arguments.

        Returns:
            Trust tier value string (read/write/destructive).
        """
        from meho_app.modules.agents.approval.trust_classifier import (
            classify_operation,
        )

        if tool_name == "call_operation":
            http_method = args.get("method")
            operation_id = args.get("operation_id", "")
            tier = classify_operation(
                connector_type="rest",  # ReactAgent is connector-agnostic
                operation_id=operation_id,
                http_method=http_method,
            )
            return tier.value

        # invalidate_topology modifies cached data -> WRITE
        if tool_name == "invalidate_topology":
            return "write"

        # Default for unknown tools -> READ (no approval needed)
        return "read"

    def _generate_description(
        self,
        tool_name: str,
        args: dict[str, Any],
        danger_level: str,
    ) -> str:
        """Generate a human-readable description of the operation.

        Args:
            tool_name: Name of the tool.
            args: Tool arguments.
            danger_level: Assessed danger level.

        Returns:
            Human-readable description string.
        """
        if tool_name == "call_operation":
            operation_id = args.get("operation_id", "unknown")
            connector_id = args.get("connector_id", "unknown")
            param_sets = args.get("parameter_sets", [])
            batch_size = len(param_sets) if isinstance(param_sets, list) else 1

            if batch_size > 1:
                return (
                    f"Execute operation '{operation_id}' on connector '{connector_id}' "
                    f"for {batch_size} items. Danger level: {danger_level}."
                )
            return (
                f"Execute operation '{operation_id}' on connector '{connector_id}'. "
                f"Danger level: {danger_level}."
            )

        if tool_name == "invalidate_topology":
            entity_name = args.get("entity_name", "unknown")
            return f"Invalidate topology cache for entity '{entity_name}'."

        # Generic description
        return f"Execute {tool_name} with provided arguments. Danger level: {danger_level}."

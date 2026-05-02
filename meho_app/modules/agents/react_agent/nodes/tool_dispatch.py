# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""ToolDispatchNode - Routes to appropriate tool execution.

This node receives the pending tool and arguments from ReasonNode
and dispatches to the appropriate tool handler. It checks for dangerous
tools that require approval before execution.
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

# Tools that require approval before execution (modify state)
DANGEROUS_TOOLS: set[str] = {"call_operation"}


@dataclass
class ToolDispatchNode(BaseNode["ReactAgentState"]):
    """Tool dispatch node - executes tools and returns observations.

    This node:
    1. Gets the pending tool and arguments from state
    2. Checks if tool requires approval (dangerous operations)
    3. If dangerous and not approved, routes to approval_check
    4. Validates the tool input
    5. Executes the tool
    6. Records the observation in the scratchpad
    7. Returns to ReasonNode for next step

    Attributes:
        NODE_NAME: Unique identifier for this node type.
    """

    NODE_NAME: ClassVar[str] = "tool_dispatch"

    async def run(
        self,
        state: ReactAgentState,
        deps: Any,
        emitter: EventEmitter,
    ) -> NodeResult:
        """Execute the pending tool and return observation.

        Args:
            state: Current agent state with pending tool and arguments.
            deps: Agent dependencies (services, config, etc.).
            emitter: SSE event emitter for streaming updates.

        Returns:
            NodeResult pointing to approval_check (if dangerous) or reason node.
        """
        await emitter.node_enter(self.NODE_NAME)

        tool_name = state.pending_tool
        tool_args = state.pending_args or {}

        if not tool_name:
            error_msg = "No pending tool to execute"
            logger.warning(error_msg)
            state.error_message = error_msg
            await emitter.error(error_msg)
            await emitter.node_exit(self.NODE_NAME, next_node=None)
            return NodeResult(next_node=None, data={"error": error_msg})

        try:
            # Check if tool requires approval and not yet approved
            requires_approval = self._requires_approval(tool_name, tool_args, deps)
            if requires_approval and not state.approval_granted:
                logger.info(f"Tool {tool_name} requires approval - routing to approval_check")
                await emitter.node_exit(self.NODE_NAME, next_node="approval_check")
                return NodeResult(next_node="approval_check")

            # Get tool from registry
            from meho_app.modules.agents.react_agent.tools import TOOL_REGISTRY

            if tool_name not in TOOL_REGISTRY:
                error_msg = f"Unknown tool: {tool_name}"
                logger.warning(error_msg)
                state.add_to_scratchpad(f"Observation: Error - {error_msg}")
                state.last_observation = error_msg
                state.clear_pending_action()
                await emitter.error(error_msg)
                await emitter.node_exit(self.NODE_NAME, next_node="reason")
                return NodeResult(next_node="reason", data={"error": error_msg})

            tool_class = TOOL_REGISTRY[tool_name]
            tool = tool_class()

            # SERVER-SIDE INJECTION: If running in connector-scoped agent context,
            # inject the connector_id into tools that need it. This ensures the LLM
            # can't accidentally use wrong connector_id or template placeholders.
            if hasattr(state, "connector_id") and state.connector_id:
                # Tools that need connector_id injection
                connector_scoped_tools = {"search_operations", "call_operation", "search_types"}
                if tool_name in connector_scoped_tools:
                    tool_args["connector_id"] = state.connector_id
                    logger.debug(f"Injected connector_id={state.connector_id} into {tool_name}")

            # Validate input
            await emitter.tool_start(tool_name)
            validated_input = tool.InputSchema(**tool_args)

            # Execute tool
            logger.info(f"Executing tool: {tool_name} with args: {tool_args}")
            result = await tool.execute(validated_input, deps, emitter)

            # Convert result to string for scratchpad
            result_str = str(result)
            if hasattr(result, "model_dump"):
                result_str = str(result.model_dump())

            # Truncate very long results for scratchpad
            if len(result_str) > 2000:
                result_str = result_str[:2000] + "... [truncated]"

            # Store observation in state
            state.last_observation = result_str
            state.add_to_scratchpad(f"Observation: {result_str}")
            state.clear_pending_action()
            state.approval_granted = False  # Reset for next action
            state.step_count += 1

            # Emit events
            await emitter.observation(tool_name, result_str)
            await emitter.tool_complete(tool_name, success=True)

            await emitter.node_exit(self.NODE_NAME, next_node="reason")
            return NodeResult(next_node="reason")

        except Exception as e:
            error_msg = f"Tool execution error: {e}"
            logger.exception(error_msg)

            # Store error as observation
            state.last_observation = error_msg
            state.add_to_scratchpad(f"Observation: Error - {error_msg}")
            state.clear_pending_action()
            state.approval_granted = False
            state.step_count += 1

            await emitter.tool_error(tool_name, str(e))
            await emitter.tool_complete(tool_name, success=False)
            await emitter.node_exit(self.NODE_NAME, next_node="reason")

            # Continue to reason so LLM can handle the error
            return NodeResult(next_node="reason", data={"error": error_msg})

    def _requires_approval(self, tool_name: str, tool_args: dict[str, Any], deps: Any) -> bool:
        """Check if a tool requires approval using trust classification.

        Uses the Phase 5 three-tier trust model instead of the old
        four-tier danger levels. READ operations pass through; WRITE
        and DESTRUCTIVE require operator approval.

        Args:
            tool_name: Name of the tool to check.
            tool_args: Tool arguments (used to classify operation).
            deps: Agent dependencies with config.

        Returns:
            True if tool requires approval, False otherwise.
        """
        if tool_name not in DANGEROUS_TOOLS:
            return False

        # Check config for approval setting
        if hasattr(deps, "agent_config"):
            tools_config = deps.agent_config.tools
            tool_config = tools_config.get(tool_name, {})
            if not tool_config.get("require_approval_for_dangerous", True):
                return False

        from meho_app.modules.agents.approval.trust_classifier import (
            classify_operation,
        )
        from meho_app.modules.agents.approval.trust_classifier import (
            requires_approval as needs_approval,
        )

        # Determine connector type from deps or args
        connector_type = "rest"  # Default for ReactAgent (orchestrator level)
        http_method = tool_args.get("method")
        operation_id = tool_args.get("operation_id", "")

        tier = classify_operation(
            connector_type=connector_type,
            operation_id=operation_id,
            http_method=http_method,
        )
        return needs_approval(tier)

    def _assess_operation_danger(self, args: dict[str, Any]) -> str:
        """Assess operation trust tier using Phase 5 classifier.

        Returns the trust tier value string (read/write/destructive)
        for backward compatibility with code that expects a danger
        level string.

        Args:
            args: Tool arguments with operation_id and optional method.

        Returns:
            Trust tier value string.
        """
        from meho_app.modules.agents.approval.trust_classifier import (
            classify_operation,
        )

        http_method = args.get("method")
        operation_id = args.get("operation_id", "")
        tier = classify_operation(
            connector_type="rest",
            operation_id=operation_id,
            http_method=http_method,
        )
        return tier.value

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""ReasonNode - LLM reasoning step in the ReAct loop.

This node is the "brain" of the ReAct loop. It prompts the LLM to generate
a Thought and either an Action (tool to call) or a Final Answer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from meho_app.core.otel import get_logger
from meho_app.modules.agents.base.node import BaseNode, NodeResult

if TYPE_CHECKING:
    from meho_app.modules.agents.config.loader import AgentConfig
    from meho_app.modules.agents.react_agent.state import ReactAgentState
    from meho_app.modules.agents.sse.emitter import EventEmitter

logger = get_logger(__name__)


@dataclass
class ParsedResponse:
    """Parsed ReAct response from LLM."""

    thought: str | None = None
    action: str | None = None
    action_input: dict[str, Any] | None = None
    final_answer: str | None = None


@dataclass
class ReasonNode(BaseNode["ReactAgentState"]):
    """LLM reasoning node - generates thoughts and decides actions.

    This node:
    1. Builds a system prompt with context
    2. Calls the LLM to reason about what to do
    3. Parses the output into Thought/Action/Final Answer
    4. Returns the next node to execute

    Attributes:
        NODE_NAME: Unique identifier for this node type.
    """

    NODE_NAME: ClassVar[str] = "reason"

    async def run(
        self,
        state: ReactAgentState,
        deps: Any,
        emitter: EventEmitter,
    ) -> NodeResult:
        """Execute LLM reasoning step.

        Args:
            state: Current agent state with user goal and scratchpad.
            deps: Agent dependencies (services, config, etc.).
            emitter: SSE event emitter for streaming updates.

        Returns:
            NodeResult indicating next node (tool_dispatch or None for terminal).
        """
        await emitter.node_enter(self.NODE_NAME)

        try:
            # Build the prompt with context
            prompt = self._build_prompt(state, deps)

            # Call LLM
            response = await self._call_llm(prompt, deps)

            # Parse the response
            parsed = self._parse_response(response)

            # Add thought to scratchpad
            if parsed.thought:
                state.add_to_scratchpad(f"Thought: {parsed.thought}")
                await emitter.thought(parsed.thought)

            # Handle action or final answer
            if parsed.final_answer:
                state.final_answer = parsed.final_answer
                state.add_to_scratchpad(f"Final Answer: {parsed.final_answer}")
                await emitter.final_answer(parsed.final_answer)
                await emitter.node_exit(self.NODE_NAME, next_node=None)
                return NodeResult(next_node=None)

            if parsed.action and parsed.action_input is not None:
                state.pending_tool = parsed.action
                state.pending_args = parsed.action_input
                state.add_to_scratchpad(f"Action: {parsed.action}")
                state.add_to_scratchpad(f"Action Input: {json.dumps(parsed.action_input)}")
                await emitter.action(parsed.action, parsed.action_input)
                await emitter.node_exit(self.NODE_NAME, next_node="tool_dispatch")
                return NodeResult(next_node="tool_dispatch")

            # No valid action or answer - error
            error_msg = "LLM response did not contain valid Action or Final Answer"
            logger.warning(f"{error_msg}: {response[:200]}")
            state.error_message = error_msg
            await emitter.error(error_msg)
            await emitter.node_exit(self.NODE_NAME, next_node=None)
            return NodeResult(next_node=None, data={"error": error_msg})

        except Exception as e:
            error_msg = f"ReasonNode error: {e}"
            logger.exception(error_msg)
            state.error_message = error_msg
            await emitter.error(error_msg)
            await emitter.node_exit(self.NODE_NAME, next_node=None)
            return NodeResult(next_node=None, data={"error": error_msg})

    def _build_prompt(self, state: ReactAgentState, deps: Any) -> str:
        """Build the full prompt with context variables.

        Args:
            state: Current agent state.
            deps: Agent dependencies with config.

        Returns:
            Rendered prompt string.
        """
        # Get the system prompt template from config
        config: AgentConfig = deps.agent_config
        template = config.system_prompt

        # Build tool list
        tool_list = self._build_tool_list()

        # Build context sections
        tables_context = self._build_tables_context(deps)
        topology_context = self._build_topology_context(deps)
        history_context = self._build_history_context(deps)
        request_guidance = ""  # Can be expanded for request-type specific guidance

        # Build scratchpad from state
        scratchpad = ""
        if state.scratchpad:
            scratchpad = "\n## Current Progress\n" + state.get_scratchpad_text()

        # Render template with variables
        # Use double braces in template, so we need to handle {{var}} format
        prompt = template
        replacements = {
            "{{tool_list}}": tool_list,
            "{{tables_context}}": tables_context,
            "{{topology_context}}": topology_context,
            "{{history_context}}": history_context,
            "{{request_guidance}}": request_guidance,
            "{{scratchpad}}": scratchpad,
            "{{user_goal}}": state.user_goal,
        }

        # Add connector-scoped agent variables if present in state
        # (BaseAgentState has connector_id, connector_name, etc.)
        if hasattr(state, "connector_id"):
            replacements["{{connector_id}}"] = state.connector_id
        if hasattr(state, "connector_name"):
            replacements["{{connector_name}}"] = state.connector_name
        if hasattr(state, "routing_description"):
            replacements["{{routing_description}}"] = state.routing_description or ""
        if hasattr(state, "iteration"):
            replacements["{{iteration}}"] = str(state.iteration)

        # Build prior findings context
        if hasattr(state, "get_prior_findings_text"):
            prior_text = state.get_prior_findings_text()
            if prior_text and "No prior" not in prior_text:
                replacements["{{prior_findings_context}}"] = f"\n## Prior Findings\n{prior_text}"
            else:
                replacements["{{prior_findings_context}}"] = ""
        else:
            replacements["{{prior_findings_context}}"] = ""

        # Get connector_type from deps or state
        if hasattr(deps, "connector_type") and deps.connector_type:
            replacements["{{connector_type}}"] = deps.connector_type
        elif hasattr(state, "connector_type") and state.connector_type:
            replacements["{{connector_type}}"] = state.connector_type
        else:
            replacements["{{connector_type}}"] = "unknown"

        for key, value in replacements.items():
            prompt = prompt.replace(key, str(value))

        return prompt

    def _build_tool_list(self) -> str:
        """Build a formatted list of available tools.

        Returns:
            Formatted tool list string.
        """
        from meho_app.modules.agents.react_agent.tools import TOOL_REGISTRY

        lines = []
        for tool_name, tool_class in TOOL_REGISTRY.items():
            description = getattr(tool_class, "TOOL_DESCRIPTION", "No description")
            # Clean up multiline descriptions
            description = " ".join(description.split())
            lines.append(f"- {tool_name}: {description}")

        return "\n".join(lines)

    def _build_tables_context(self, deps: Any) -> str:
        """Build context about available cached data tables.

        Args:
            deps: Agent dependencies.

        Returns:
            Tables context string or empty string if none.
        """
        # Check if data reduction service has tables
        if hasattr(deps, "data_reduction_context") and deps.data_reduction_context:
            tables = deps.data_reduction_context
            if tables:
                lines = ["\n## Available Data Tables"]
                for table_name, table_info in tables.items():
                    columns = table_info.get("columns", [])
                    row_count = table_info.get("row_count", 0)
                    lines.append(f"- `{table_name}`: {row_count} rows, columns: {columns}")
                return "\n".join(lines)
        return ""

    def _build_topology_context(self, deps: Any) -> str:
        """Build context about known topology entities.

        Args:
            deps: Agent dependencies.

        Returns:
            Topology context string or empty string if none.
        """
        if hasattr(deps, "topology_context") and deps.topology_context:
            return f"\n## Known Topology\n{deps.topology_context}"
        return ""

    def _build_history_context(self, deps: Any) -> str:
        """Build context from conversation history.

        Args:
            deps: Agent dependencies.

        Returns:
            History context string or empty string if none.
        """
        if hasattr(deps, "conversation_history") and deps.conversation_history:
            return f"\n## Previous Messages\n{deps.conversation_history}"
        return ""

    async def _call_llm(self, prompt: str, deps: Any) -> str:
        """Call the LLM with the given prompt.

        Args:
            prompt: The full system prompt.
            deps: Agent dependencies with model config.

        Returns:
            LLM response string.
        """
        from meho_app.modules.agents.base.inference import infer

        config: AgentConfig = deps.agent_config

        # Call LLM using the inference utility
        response = await infer(
            system_prompt=prompt,
            message="Please analyze and respond.",
            model=config.model.name,
            temperature=config.model.temperature,
        )

        return response

    def _parse_response(self, response: str) -> ParsedResponse:
        """Parse the LLM response into structured format.

        Extracts Thought, Action, Action Input, or Final Answer from
        the ReAct-formatted response.

        Args:
            response: Raw LLM response string.

        Returns:
            ParsedResponse with extracted components.
        """
        parsed = ParsedResponse()

        # Extract Thought
        thought_match = re.search(
            r"Thought:\s*(.+?)(?=\n(?:Action:|Final Answer:)|$)",
            response,
            re.DOTALL,
        )
        if thought_match:
            parsed.thought = thought_match.group(1).strip()

        # Check for Final Answer first (takes precedence)
        final_match = re.search(r"Final Answer:\s*(.+?)$", response, re.DOTALL)
        if final_match:
            parsed.final_answer = final_match.group(1).strip()
            return parsed

        # Extract Action and Action Input
        action_match = re.search(r"Action:\s*(\S+)", response)
        if action_match:
            parsed.action = action_match.group(1).strip()

            # Extract Action Input - try multiple patterns
            action_input_match = re.search(
                r"Action Input:\s*(\{.*\}|\[.*\])",
                response,
                re.DOTALL,
            )
            if action_input_match:
                try:
                    parsed.action_input = json.loads(action_input_match.group(1))
                except json.JSONDecodeError:
                    # Try to extract just the JSON part
                    json_str = action_input_match.group(1)
                    # Clean up common issues
                    json_str = json_str.replace("'", '"')
                    try:
                        parsed.action_input = json.loads(json_str)
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse Action Input: {json_str[:100]}")
                        parsed.action_input = {}
            else:
                # No JSON found, use empty dict
                parsed.action_input = {}

        return parsed

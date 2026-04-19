# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
LoopDetectionNode - Prevents Circular Reasoning (TASK-XX)

This node is called after each tool execution to detect if the agent
is stuck in a loop (repeatedly making similar calls without progress).

When a loop is detected:
1. First warning: Agent is prompted to try a different approach
2. Second warning: Agent is forced to conclude with available information

This prevents the agent from going in circles indefinitely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_graph import BaseNode, End, GraphRunContext

from meho_app.modules.agents.shared.graph.graph_deps import MEHOGraphDeps
from meho_app.modules.agents.shared.graph.graph_state import MEHOGraphState

if TYPE_CHECKING:
    from meho_app.modules.agents.shared.graph.nodes.reason_node import ReasonNode

from meho_app.core.otel import get_logger

logger = get_logger(__name__)

# Configuration constants
LOOP_CHECK_AFTER_STEPS = 5  # Start checking for loops after this many steps
LOOP_WINDOW_SIZE = 10  # Look at last N actions for pattern detection
LOOP_REPEAT_THRESHOLD = 3  # Same action repeated this many times = loop
MAX_LOOP_WARNINGS = 2  # After this many warnings, force conclusion


async def _generate_conclusion_with_available_data(
    state: MEHOGraphState,
    deps: MEHOGraphDeps,
) -> str:
    """
    Generate a conclusion using all available data when forced to stop.

    This is called when the agent has been warned multiple times but
    continues to loop. We force it to synthesize a response.
    """
    # Get scratchpad summary (last 8000 chars)
    scratchpad = state.get_scratchpad_text()
    if len(scratchpad) > 8000:
        scratchpad = scratchpad[-8000:]

    # Build explored approaches summary
    approaches_text = ""
    if state.explored_approaches:
        approaches_text = "\n\nApproaches already tried:\n" + "\n".join(
            f"- {approach}" for approach in state.explored_approaches
        )

    conclusion_prompt = f"""You are MEHO, an infrastructure agent. You have been investigating a user request but have not found a complete answer after extensive searching.

USER'S ORIGINAL REQUEST: {state.user_goal}

INVESTIGATION SUMMARY:
{scratchpad}
{approaches_text}

ACTION SUMMARY: {state.get_action_summary()}

You MUST now provide a Final Answer to the user. Do NOT suggest more actions to take.

Your response should:
1. Acknowledge what you found (specific data, facts discovered)
2. Clearly state what information you could NOT find or is not available
3. If applicable, explain WHY the information might not be available (e.g., "The API does not expose host-to-VM mapping")
4. Suggest alternatives if any exist (e.g., "You could check the host directly via SSH")

Be honest about limitations. Do NOT make up data or pretend you have information you don't.

Respond with ONLY the Final Answer content (no "Final Answer:" prefix needed)."""

    try:
        result = await deps.llm_agent.run(
            "Generate conclusion",
            instructions=conclusion_prompt,
        )
        return str(result.output)
    except Exception as e:
        logger.error(f"Failed to generate forced conclusion: {e}")
        return (
            f"I investigated your request but encountered limitations. "
            f"Based on my analysis:\n\n"
            f"**What I found:** {state.action_summary if hasattr(state, 'action_summary') else 'See above for details'}\n\n"
            f"**Limitation:** After extensive searching, the requested information "
            f"does not appear to be available through the current API connectors."
        )


def _get_loop_avoidance_hint(loop_description: str, state: MEHOGraphState) -> str:
    """
    Generate a hint to help the agent avoid the detected loop.
    """
    hints = []

    if "list_connectors" in loop_description:
        hints.append("You already know the connectors. Use the connector_id directly.")

    if "search_operations" in loop_description:
        hints.append(
            "You've searched operations multiple times. Either the operation doesn't exist, "
            "or you need to use a different search query. Consider concluding if no progress."
        )

    if "call_operation" in loop_description or "get_virtual_machine" in loop_description.lower():
        hints.append(
            "You've called this operation multiple times with similar parameters. "
            "The data returned is likely all that's available. Use it to answer or explain limitations."
        )

    if "Oscillating" in loop_description:
        hints.append(
            "You're alternating between two tools without progress. "
            "This suggests the information you're looking for may not be available. "
            "Conclude with what you have."
        )

    # Add explored approaches context
    if state.explored_approaches:
        approaches_list = ", ".join(state.explored_approaches[-5:])
        hints.append(f"Already tried: {approaches_list}")

    if not hints:
        hints.append(
            "Consider whether the information exists in the current system. "
            "If not, explain this limitation to the user."
        )

    return "\n".join(f"- {hint}" for hint in hints)


@dataclass
class LoopDetectionNode(BaseNode[MEHOGraphState, MEHOGraphDeps, None]):
    """
    Detects if the agent is stuck in a loop and intervenes.

    Called after each tool execution (before returning to ReasonNode).

    Transitions:
    - No loop detected → ReasonNode (continue normally)
    - Loop detected (first time) → ReasonNode (with warning in scratchpad)
    - Loop detected (second time) → Force conclusion → End
    """

    async def run(
        self,
        ctx: GraphRunContext[MEHOGraphState, MEHOGraphDeps],
    ) -> ReasonNode | End[None]:
        """Check for loops and decide next action."""
        from meho_app.modules.agents.shared.graph.nodes.reason_node import ReasonNode

        state = ctx.state
        deps = ctx.deps

        # Don't check for loops in early steps
        if state.step_count < LOOP_CHECK_AFTER_STEPS:
            return ReasonNode()

        # Check for loop pattern
        loop_description = state.detect_loop(
            window_size=LOOP_WINDOW_SIZE,
            repeat_threshold=LOOP_REPEAT_THRESHOLD,
        )

        if loop_description is None:
            # No loop detected, continue normally
            return ReasonNode()

        # Loop detected!
        logger.warning(f"Loop detected: {loop_description}")
        state.loop_warning_count += 1

        if state.loop_warning_count >= MAX_LOOP_WARNINGS:
            # Too many warnings, force conclusion
            logger.info(f"Forcing conclusion after {state.loop_warning_count} loop warnings")

            await deps.emit_progress(
                "thought",
                {
                    "content": (
                        f"⚠️ I've been going in circles ({loop_description}). "
                        f"Let me provide what I've learned so far."
                    )
                },
            )

            # Generate forced conclusion
            conclusion = await _generate_conclusion_with_available_data(state, deps)
            state.final_answer = conclusion

            await deps.emit_progress("final_answer", {"content": conclusion})
            return End(None)

        # First warning - add context to help agent break the loop
        hint = _get_loop_avoidance_hint(loop_description, state)

        warning_message = f"""
⚠️ LOOP DETECTED: {loop_description}

You are repeating similar actions without making progress.

SUGGESTIONS TO BREAK THE LOOP:
{hint}

IMPORTANT: If the information you're looking for is not available through the APIs,
acknowledge this to the user and provide what you DO have.

If you cannot find what the user needs, provide a Final Answer explaining:
1. What you DID find
2. What is NOT available and why
3. Any alternative approaches the user could try
"""

        state.add_to_scratchpad(warning_message)

        # Also emit as a thought so user can see the agent is aware
        await deps.emit_progress(
            "thought",
            {
                "content": f"🔄 Detected repetitive pattern: {loop_description}. Trying a different approach."
            },
        )

        # Continue to ReasonNode with the warning in context
        return ReasonNode()

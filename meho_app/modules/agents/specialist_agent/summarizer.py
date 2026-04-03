# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Step summarization for the specialist agent sliding window scratchpad.

Collapses older investigation steps to one-line LLM-generated summaries
(via Haiku 4.5) while keeping recent steps at full detail. Each step is
summarized exactly once when it ages out of the recent window.

Phase 34 (v1.69 Token Optimization): ~65% cumulative token reduction
when combined with Phase 33 observation compression.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from meho_app.modules.agents.base.inference import infer

if TYPE_CHECKING:
    from meho_app.modules.agents.specialist_agent.state import SpecialistReActState


# Map tool names to their most identifying parameter for summaries
_TOOL_KEY_PARAM_MAP: dict[str, str] = {
    "search_operations": "query",
    "call_operation": "operation_id",
    "reduce_data": "sql",
    "lookup_topology": "query",
    "search_knowledge": "query",
    "store_memory": "title",
    "forget_memory": "query",
    "invalidate_topology": "query",
}

# Maximum length for key parameter values in summaries
_MAX_KEY_PARAM_LENGTH = 60


@dataclass
class StepRecord:
    """A single completed step in the specialist investigation.

    Tracks step metadata and optionally a one-line summary generated
    by Haiku 4.5 when the step ages out of the recent window.

    Attributes:
        step_number: 1-based sequential index within completed_steps.
        tool: The tool name that was called.
        action_input_key: Primary identifying parameter value (e.g., query, operation_id).
        observation: Full compressed observation text.
        summary: One-line summary, set at collapse time. None until summarized.
    """

    step_number: int
    tool: str
    action_input_key: str
    observation: str
    summary: str | None = None

    @property
    def is_summarized(self) -> bool:
        """Whether this step has been collapsed to a one-line summary."""
        return self.summary is not None


async def summarize_step(step: StepRecord, current_goal: str) -> str:
    """Generate a one-line summary of a completed investigation step.

    Uses Haiku 4.5 for fast, cheap summarization. The specialist's current
    investigation goal provides context so summaries capture what matters.

    Args:
        step: The completed step to summarize.
        current_goal: The specialist's current investigation focus.

    Returns:
        One-line summary in format: "Step N: tool(key_param) -> outcome"
    """
    system_prompt = (
        "You are summarizing a single investigation step into ONE line. "
        "Format: 'Step {N}: {tool}({key_param}) -> {outcome}'. "
        "Preserve: table names, row counts, entity names, error messages. "
        "No preamble. One line only."
    )
    message = (
        f"Investigation goal: {current_goal}\n\n"
        f"Step {step.step_number}:\n"
        f"Tool: {step.tool}\n"
        f"Key parameter: {step.action_input_key}\n"
        f"Full observation:\n{step.observation}"
    )
    try:
        summary = await infer(
            system_prompt=system_prompt,
            message=message,
            model="anthropic:claude-haiku-4-5",
            temperature=0.0,
        )
        return summary.strip()
    except Exception:
        # Fallback: rule-based summary if Haiku fails
        obs_preview = step.observation[:80].replace("\n", " ")
        return f"Step {step.step_number}: {step.tool}({step.action_input_key}) -> {obs_preview}"


async def collapse_old_steps(
    state: SpecialistReActState,
    current_thought: str,
) -> None:
    """Summarize steps that have aged out of the recent window.

    Called after each add_observation() in the ReAct loop. Only summarizes
    steps that are outside the recent window and haven't been summarized yet.
    Each step is summarized exactly once (O(n) total calls across an investigation).

    Args:
        state: The specialist agent's ReAct state.
        current_thought: The specialist's current thought/goal for context-aware summaries.
    """
    if len(state.completed_steps) <= state.window_size:
        return  # Window not full yet

    # Steps that should be summarized: all except the last window_size
    boundary = max(0, len(state.completed_steps) - state.window_size)
    for step in state.completed_steps[:boundary]:
        if not step.is_summarized:
            step.summary = await summarize_step(step, current_thought)


def _extract_key_param(tool: str, action_input: dict | None) -> str:
    """Extract the most identifying parameter from a tool call.

    Maps each tool to its primary parameter name, then extracts and
    truncates the value for use in step summaries.

    Args:
        tool: The tool name.
        action_input: The tool arguments dict, or None.

    Returns:
        The key parameter value (truncated to 60 chars), or empty string.
    """
    if not action_input:
        return ""
    param_name = _TOOL_KEY_PARAM_MAP.get(tool, "")
    if not param_name:
        return ""
    value = action_input.get(param_name, "")
    value_str = str(value)
    if len(value_str) > _MAX_KEY_PARAM_LENGTH:
        return value_str[:57] + "..."
    return value_str

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Streaming and dispatch helpers for the Orchestrator Agent.

This module provides functions for building agent prompts and
managing parallel dispatch contexts.

Claude streaming compatibility:
- PydanticAI normalizes Anthropic's content_block_start/delta/stop events
  into a unified stream format, so this module doesn't need Anthropic-specific
  handling for streaming events.
- ThinkingPart objects from Claude's adaptive thinking are handled upstream
  in the SSE emitter (EventEmitter.thinking_part -> "thought" SSE event).
- The orchestrator dispatches to connector-specific agents via
  build_agent_prompt(), which feeds into the same ReAct loop that handles
  ThinkingPart mapping. No orchestrator-level streaming changes needed.
See: RESEARCH.md Pitfall 4.
"""

from __future__ import annotations

from typing import Any

from meho_app.modules.agents.orchestrator.context_passing import build_structured_prior_findings
from meho_app.modules.agents.orchestrator.state import (
    ConnectorSelection,
    OrchestratorState,
)


def build_agent_prompt(
    state: OrchestratorState,
    connector: ConnectorSelection,
    investigation_context: dict[str, Any] | None = None,
) -> str:
    """Build the prompt for a connector-specific agent.

    For Round 1 (no investigation_context): produces the standard prompt
    with user goal and prior findings context.

    For Round 2+ (investigation_context provided): produces a focused
    prompt with time window, entity identifiers, and investigation summary.
    Does NOT include prior findings text to avoid symptom propagation
    (locked decision: avoid tunnel vision).

    Args:
        state: Current orchestrator state.
        connector: The connector being queried.
        investigation_context: Optional context from previous round for
            focused follow-up investigations (Round 2+).

    Returns:
        Formatted prompt for the agent.
    """
    # Check if this connector was already queried (re-query scenario)
    connector_prior_findings = [
        f for f in state.all_findings if f.connector_id == connector.connector_id
    ]
    other_findings = [f for f in state.all_findings if f.connector_id != connector.connector_id]

    prior_context = ""

    # If re-querying same connector, emphasize what's already done and what's missing
    if connector_prior_findings:
        prior_context = "\n\n**IMPORTANT - You already queried this connector and got:**\n"
        for f in connector_prior_findings:
            if f.findings:
                prior_context += f"- {f.findings[:500]}...\n"
        prior_context += (
            "\n**Do NOT repeat what you already retrieved. "
            "Focus on the OTHER parts of the query that haven't been addressed yet.**\n"
        )

    # Round 2+: focused investigation with investigation_context
    if investigation_context is not None:
        focus_parts: list[str] = [state.user_goal, ""]

        # Investigation summary for orientation
        summary = investigation_context.get("investigation_summary", "")
        if summary:
            focus_parts.append(f"Context: {summary}")

        # Time window focus
        time_window = investigation_context.get("time_window", {})
        if time_window.get("start") and time_window.get("end"):
            focus_parts.append(
                f"Focus your investigation on the time window "
                f"{time_window['start']} to {time_window['end']}."
            )

        # Entity identifiers (Phase 77: topology_routing uses entity_identifiers key)
        entities = investigation_context.get("entity_identifiers") or investigation_context.get("entities", {})
        for entity_type, value in entities.items():
            focus_parts.append(f"Investigate {entity_type} '{value}' on this connector.")

        focus_parts.append(f"\nFocus on what {connector.connector_name} can tell us.")
        focus_parts.append(prior_context)
        return "\n".join(focus_parts)

    # Round 1: standard prompt with other connector context
    # Phase 77: Structured prior findings (D-12) -- replaces raw findings truncation
    if other_findings:
        structured_context = build_structured_prior_findings(other_findings)
        if structured_context:
            prior_context += f"\n{structured_context}"

    return f"""{state.user_goal}

Focus on what {connector.connector_name} can tell us about this question.
{prior_context}"""

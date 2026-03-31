# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Synthesis and response generation for the Orchestrator Agent.

This module provides functions for building synthesis prompts and
generating conversational responses.
"""

from __future__ import annotations

from pathlib import Path

from meho_app.modules.agents.orchestrator.history import format_history_for_prompt
from meho_app.modules.agents.orchestrator.state import OrchestratorState


def build_synthesis_prompt(
    state: OrchestratorState,
    memory_summary: str = "",
    skill_context: str = "",
    budget_context: str = "",  # Phase 77: budget exhaustion context
) -> str:
    """Build the synthesis prompt with conversation context.

    Args:
        state: Current orchestrator state with all findings.
        memory_summary: Cross-connector memory summaries for synthesis (Phase 11).
        skill_context: Full orchestrator skill content for investigation guidance (Phase 52).
        budget_context: Budget exhaustion/convergence context (Phase 77).

    Returns:
        Formatted synthesis prompt string.
    """
    template_path = Path(__file__).parent / "prompts" / "synthesis.md"
    template = template_path.read_text()

    # Include conversation history for context
    history_context = format_history_for_prompt(state.conversation_history)

    # Phase 77: Build budget context from state if not provided
    if not budget_context and state.budget_exhaustion_reason:
        systems_queried = len({f.connector_name for f in state.all_findings})
        if state.budget_exhaustion_reason == "budget_exhausted":
            budget_context = (
                f"Investigation budget exhausted ({state.dispatch_count} dispatches "
                f"across {systems_queried} systems). Synthesize all findings into "
                f"the best possible answer."
            )
        elif state.budget_exhaustion_reason == "converged":
            budget_context = (
                f"Investigation converged after {state.dispatch_count} dispatches "
                f"across {systems_queried} systems (last {state.convergence_window} "
                f"specialists produced no new findings). Synthesize accumulated findings."
            )

    # Use format_map with defaultdict for graceful handling of missing template variables
    from collections import defaultdict

    template_vars = defaultdict(str, {
        "query": state.user_goal,
        "findings": state.get_findings_summary(),
        "history": history_context,
        "memory_summary": memory_summary,
        "budget_context": budget_context,
    })

    prompt = template.format_map(template_vars)

    # Append orchestrator skill guidance if loaded during routing (Phase 52)
    if skill_context:
        prompt += (
            f"\n\n<orchestrator_skill_guidance>\n{skill_context}\n</orchestrator_skill_guidance>"
        )

    return prompt


async def build_memory_summaries_for_synthesis(state: OrchestratorState, tenant_id: str) -> str:
    """Fetch and format memory summaries for all connectors in findings.

    Builds a cross-connector memory context block for the synthesis prompt.
    Uses semantic search (Phase 89.1) with relevance-based filtering
    to include only relevant memories per connector.

    Args:
        state: Current orchestrator state with all_findings populated.
        tenant_id: Tenant ID for memory scoping. Required -- must always be
            provided by the caller from the authenticated user's tenant context.
            This is a defense-in-depth measure to prevent cross-tenant memory leaks.

    Returns:
        Formatted cross-connector memory summary string,
        or empty string if no memories found or on any failure.
    """
    try:
        from meho_app.modules.memory.context_builder import build_relevant_memory_summary

        # Collect unique connectors from findings
        seen: set[str] = set()
        connectors: list[tuple[str, str]] = []  # (connector_id, connector_name)
        for finding in state.all_findings:
            if finding.connector_id not in seen:
                seen.add(finding.connector_id)
                connectors.append((finding.connector_id, finding.connector_name))

        if not connectors:
            return ""

        sections: list[str] = []

        for connector_id, connector_name in connectors:
            # build_relevant_memory_summary handles its own DB session internally
            summary = await build_relevant_memory_summary(
                query=state.user_goal,
                connector_id=connector_id,
                tenant_id=tenant_id,
            )
            if summary:
                sections.append(f"### {connector_name}\n{summary}")

        if not sections:
            return ""

        return "## Cross-Connector Memory Context\n\n" + "\n\n".join(sections)

    except Exception:
        return ""


def build_conversational_prompt(user_goal: str) -> str:
    """Build a prompt for conversational/general knowledge responses.

    Used when no connectors were needed to answer the user's question.

    Args:
        user_goal: The user's original message/question.

    Returns:
        Formatted prompt for conversational response.
    """
    return f'''The user asked: "{user_goal}"

This appears to be a conversational message or general knowledge question that doesn't require querying any external systems.

Respond naturally and helpfully. If it's a greeting, respond warmly. If it's a general knowledge question, provide a helpful answer based on your knowledge.

Keep your response concise and friendly.'''


def build_multi_turn_synthesis_context(
    session_state: object | None,
    turn_count: int,
    session_context: str | None,
) -> str:
    """Build additional synthesis context for multi-turn conversations.

    Args:
        session_state: Session state object (may have turn_count attribute).
        turn_count: Current turn number.
        session_context: Pre-built session context string.

    Returns:
        Additional context to append to synthesis prompt, or empty string.
    """
    if not session_state or turn_count <= 0 or not session_context:
        return ""

    return f"""

## Conversation Context

This is turn {turn_count + 1} of an ongoing conversation.
The user may be following up on previous queries.

{session_context}

When synthesizing:
- Reference previous context if relevant
- Avoid repeating information already provided in earlier turns
- Connect this answer to prior findings where appropriate
"""

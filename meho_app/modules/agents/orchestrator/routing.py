# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Routing and decision logic for the Orchestrator Agent.

This module provides functions for connector routing decisions,
prompt building, and response parsing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from meho_app.core.otel import get_logger
from meho_app.modules.agents.orchestrator.history import format_history_for_prompt
from meho_app.modules.agents.orchestrator.state import (
    ConnectorSelection,
    OrchestratorState,
)

logger = get_logger(__name__)


def format_connectors(connectors: list[dict[str, Any]]) -> str:
    """Format connectors for LLM prompt with relationship hints.

    Args:
        connectors: List of connector dicts with name, id, type, etc.

    Returns:
        Formatted string for inclusion in prompts.

    Example:
        >>> connectors = [{"name": "K8s Prod", "id": "k8s-1", "connector_type": "k8s"}]
        >>> format_connectors(connectors)
        '- **K8s Prod** (ID: k8s-1, Type: k8s)\\n  Description: No description'
    """
    lines = []
    for c in connectors:
        # Include related connector hints if available
        related = c.get("related_connectors", [])
        related_hint = f" (related: {', '.join(related)})" if related else ""

        # Mark if already queried
        already_queried = c.get("already_queried", False)
        queried_hint = (
            " [ALREADY QUERIED - can re-query for different data]" if already_queried else ""
        )

        lines.append(
            f"- **{c['name']}** (ID: {c['id']}, Type: {c['connector_type']}){related_hint}{queried_hint}\n"
            f"  Description: {c['routing_description'] or c.get('description', 'No description')}"
        )
    return "\n".join(lines)


def find_connector(connectors: list[dict[str, Any]], connector_id: str) -> dict[str, Any] | None:
    """Find connector by ID.

    Args:
        connectors: List of connector dicts to search.
        connector_id: The connector ID to find.

    Returns:
        Connector dict if found, None otherwise.
    """
    for c in connectors:
        if c["id"] == connector_id:
            return c
    return None


def build_routing_prompt(
    state: OrchestratorState,
    connectors: list[dict[str, Any]],
    already_queried: set[str] | None = None,
    skill_summaries: str = "",
    investigation_skill_summaries: str = "",  # Phase 77: investigation skill summaries
) -> str:
    """Build the routing decision prompt with iteration and conversation context.

    Args:
        state: Current orchestrator state.
        connectors: Available connectors.
        already_queried: Set of already-queried connector IDs.
        skill_summaries: Formatted skill summaries for prompt injection.
        investigation_skill_summaries: Formatted investigation-only skill summaries (Phase 77).

    Returns:
        Formatted routing prompt string.
    """
    template_path = Path(__file__).parent / "prompts" / "routing.md"
    template = template_path.read_text()

    # Build iteration context
    iteration = state.current_iteration + 1
    max_iterations = state.max_iterations
    already_queried_list = list(already_queried) if already_queried else []

    # Build conversation history context
    history_context = format_history_for_prompt(state.conversation_history)

    # Phase 77: Budget and visited connector context
    visited_list = (
        ", ".join(f"{cid}(x{count})" for cid, count in state.visited_connectors.items())
        if state.visited_connectors
        else "None"
    )

    requery_warning = ""
    requery_set = state.get_requery_connectors()
    if requery_set:
        requery_warning = (
            "WARNING: The following connectors have been queried multiple times: "
            + ", ".join(requery_set)
            + ". You MUST justify re-querying them with a specific reason."
        )

    # Use format_map with defaultdict for graceful handling of missing template variables
    from collections import defaultdict

    template_vars = defaultdict(
        str,
        {
            "connectors": format_connectors(connectors),
            "query": state.user_goal,
            "findings": state.get_findings_summary() or "None yet - this is the first iteration.",
            "iteration": str(iteration),
            "max_iterations": str(max_iterations),
            "already_queried": (
                ", ".join(already_queried_list) if already_queried_list else "None"
            ),
            "history": history_context,
            # Phase 77: Budget, convergence, and investigation skill variables
            "remaining_budget": str(state.remaining_budget()),
            "dispatch_count": str(state.dispatch_count),
            "investigation_budget": str(state.investigation_budget),
            "visited_connectors": visited_list,
            "requery_warning": requery_warning,
            "investigation_skills": investigation_skill_summaries,
        },
    )

    prompt = template.format_map(template_vars)

    # Append orchestrator skill summaries if available (Phase 52)
    if skill_summaries:
        prompt += f"\n\n{skill_summaries}"

    return prompt


def parse_decision(response: str, connectors: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse LLM decision response.

    Args:
        response: Raw LLM response text.
        connectors: Available connectors for lookup.

    Returns:
        Parsed decision dict with "action" key and optional "connectors" list.
    """
    # Try to extract JSON from response
    try:
        # Look for JSON in the response
        start_idx = response.find("{")
        end_idx = response.rfind("}") + 1
        if start_idx >= 0 and end_idx > start_idx:
            json_str = response[start_idx:end_idx]
            decision = json.loads(json_str)

            if decision.get("action") == "query" and "connectors" in decision:
                # Phase 99: Extract classification and reasoning from routing decision
                decision["classification"] = decision.get("classification", "standard")
                decision["reasoning"] = decision.get("reasoning", "")

                # Convert to ConnectorSelection objects
                selections = []
                for c in decision["connectors"]:
                    conn_info = find_connector(connectors, c["connector_id"])
                    if conn_info:
                        selections.append(
                            ConnectorSelection(
                                connector_id=c["connector_id"],
                                connector_name=c.get("connector_name", conn_info["name"]),
                                connector_type=conn_info.get("connector_type", "unknown"),
                                routing_description=conn_info.get("routing_description", ""),
                                relevance_score=0.8,
                                reason=c.get("reason", "Selected by LLM"),
                                # Wire skill fields from connector info (Phase 7.1)
                                generated_skill=conn_info.get("generated_skill"),
                                custom_skill=conn_info.get("custom_skill"),
                                skill_name=conn_info.get("skill_name"),
                                # Connection details for error messaging (Phase 23)
                                base_url=conn_info.get("base_url"),
                                # Phase 99: Tiered dispatch fields
                                priority=c.get("priority", 1),
                                max_steps=c.get("max_steps"),
                                conditional=c.get("conditional", False),
                            )
                        )
                decision["connectors"] = selections
                result: dict[str, Any] = decision
                return result

            # Phase 99: Extract classification/reasoning for respond action too
            if decision.get("action") == "respond":
                decision["classification"] = decision.get("classification", "standard")
                decision["reasoning"] = decision.get("reasoning", "")

            return dict(decision)

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"Failed to parse decision JSON: {e}")

    # Default: respond (safe fallback)
    return {"action": "respond"}

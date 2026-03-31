# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Cross-system diagnostic entity routing for multi-round dispatch.

Phase 32: When a specialist discovers entities outside its connector domain,
it emits UNRESOLVED lines in its findings. This module parses those lines,
maps entities to the appropriate connectors, and builds investigation context
for follow-up specialists.

Flow:
  1. Specialist emits: UNRESOLVED: node worker-03 | High CPU pod here | kubernetes
  2. parse_unresolved_entities() extracts structured entity dicts
  3. route_unresolved_entities() maps entities to available connectors
  4. build_investigation_context() creates compact context for Round 2+ agents
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput

from meho_app.modules.agents.orchestrator.state import ConnectorSelection

# ---------------------------------------------------------------------------
# Unresolved entity parser
# ---------------------------------------------------------------------------

# Pattern: UNRESOLVED: <type> <value> | <context> | <domain>
# Forgiving regex: optional whitespace, multiline
_UNRESOLVED_PATTERN = re.compile(
    r"UNRESOLVED:\s*(\w+)\s+(.+?)\s*\|\s*(.+?)\s*\|\s*(\w+)",
    re.MULTILINE,
)

# Best-effort ISO timestamp extraction for time_window
_ISO_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")


def parse_unresolved_entities(findings: str) -> list[dict[str, Any]]:
    """Extract unresolved entities from specialist findings text.

    Parses UNRESOLVED: lines emitted by specialists when they discover
    entities outside their connector domain.

    Args:
        findings: Raw findings text from a specialist agent.

    Returns:
        List of entity dicts with keys: entity_type, value, context,
        suggested_domain. Empty list if no UNRESOLVED lines found
        (graceful degradation).
    """
    entities: list[dict[str, Any]] = []
    for match in _UNRESOLVED_PATTERN.finditer(findings):
        entities.append(
            {
                "entity_type": match.group(1),
                "value": match.group(2).strip(),
                "context": match.group(3).strip(),
                "suggested_domain": match.group(4).strip(),
            }
        )
    return entities


# ---------------------------------------------------------------------------
# Entity routing map
# ---------------------------------------------------------------------------

# Maps entity_type -> suggested_domain -> list of connector_types
ENTITY_ROUTING_MAP: dict[str, dict[str, list[str]]] = {
    "service_name": {
        "observability": ["prometheus", "loki", "tempo"],
        "kubernetes": ["kubernetes"],
    },
    "pod": {
        "kubernetes": ["kubernetes"],
        "observability": ["loki", "prometheus"],
    },
    "node": {
        "kubernetes": ["kubernetes"],
        "vmware": ["vmware"],
        "infrastructure": ["kubernetes", "vmware"],
    },
    "time_range": {
        "observability": ["prometheus", "loki", "tempo"],
    },
}


def route_unresolved_entities(
    entities: list[dict[str, Any]],
    available_connectors: list[dict[str, Any]],
    already_queried_ids: set[str],
) -> list[ConnectorSelection]:
    """Map unresolved entities to connectors for follow-up dispatch.

    For each entity, looks up target connector types from ENTITY_ROUTING_MAP
    based on entity_type and suggested_domain. Matches against available
    connectors by connector_type. Builds ConnectorSelection objects with
    combined goals per connector.

    Connectors already queried in Round 1 CAN be re-selected (different
    focus via investigation_context).

    Args:
        entities: Parsed unresolved entities from parse_unresolved_entities().
        available_connectors: List of connector dicts with at least 'id',
            'name', 'connector_type' keys.
        already_queried_ids: Set of connector IDs already queried. Currently
            informational -- connectors can be re-queried with different focus.

    Returns:
        List of ConnectorSelection objects for Round 2+ dispatch.
    """
    # Accumulate goals per connector_id
    connector_goals: dict[str, list[str]] = {}

    for entity in entities:
        entity_type = entity.get("entity_type", "")
        domain = entity.get("suggested_domain", "")
        value = entity.get("value", "")
        context = entity.get("context", "")

        # Find target connector types from routing map
        target_types = ENTITY_ROUTING_MAP.get(entity_type, {}).get(domain, [])

        for conn in available_connectors:
            if conn.get("connector_type") in target_types:
                conn_id = conn["id"]
                goal = f"Investigate {entity_type} '{value}': {context}"
                connector_goals.setdefault(conn_id, []).append(goal)

    # Build ConnectorSelection objects with combined goals
    selections: list[ConnectorSelection] = []
    for conn_id, goals in connector_goals.items():
        conn = next(c for c in available_connectors if c["id"] == conn_id)
        selections.append(
            ConnectorSelection(
                connector_id=conn_id,
                connector_name=conn.get("name", ""),
                connector_type=conn.get("connector_type", ""),
                routing_description=conn.get("routing_description", ""),
                relevance_score=0.9,  # High relevance -- entity-driven
                reason=f"Follow-up: {'; '.join(goals[:3])}",
                generated_skill=conn.get("generated_skill"),
                custom_skill=conn.get("custom_skill"),
                skill_name=conn.get("skill_name"),
                base_url=conn.get("base_url"),
            )
        )

    return selections


# ---------------------------------------------------------------------------
# Investigation context builder
# ---------------------------------------------------------------------------


def build_investigation_context(
    findings: list[SubgraphOutput],
    entities: list[dict[str, Any]],
    round_number: int,
) -> dict[str, Any]:
    """Build compact investigation context for Round 2+ specialists.

    Creates a focused context payload with time windows and entity
    identifiers. Deliberately does NOT include symptoms, error keywords,
    or detailed findings text (locked decision: avoid tunnel vision).

    Args:
        findings: SubgraphOutput list from the previous round.
        entities: Parsed unresolved entities driving this follow-up.
        round_number: Current dispatch round (2 or 3).

    Returns:
        Investigation context dict with keys: time_window, entities,
        round, triggering_connector, investigation_summary.
    """
    # Extract time_window from findings (best-effort ISO timestamp regex)
    time_window: dict[str, str] = {}
    all_timestamps: list[str] = []
    for f in findings:
        if f.findings:
            all_timestamps.extend(_ISO_TIMESTAMP_PATTERN.findall(f.findings))

    if all_timestamps:
        sorted_ts = sorted(all_timestamps)
        time_window = {"start": sorted_ts[0], "end": sorted_ts[-1]}

    # Collect entity identifiers from unresolved entities
    entity_identifiers: dict[str, str] = {}
    for entity in entities:
        etype = entity.get("entity_type", "")
        value = entity.get("value", "")
        if etype and value:  # noqa: SIM102 -- readability preferred over collapse
            # If multiple values for same type, keep first (most relevant)
            if etype not in entity_identifiers:
                entity_identifiers[etype] = value

    # Determine triggering connector (first successful finding)
    triggering_connector = ""
    for f in findings:
        if f.status == "success" and f.connector_name:
            triggering_connector = f.connector_name
            break

    # Build factual summary under 200 chars -- no symptoms, no error details
    entity_names = [
        f"{e.get('entity_type')} '{e.get('value')}'"
        for e in entities[:3]  # Cap at 3 for brevity
    ]
    summary_parts = []
    if triggering_connector:
        summary_parts.append(f"{triggering_connector} flagged")
    if entity_names:
        summary_parts.append(", ".join(entity_names))
    summary_parts.append(f"for follow-up (round {round_number})")
    investigation_summary = " ".join(summary_parts)
    # Enforce 200 char limit
    if len(investigation_summary) > 200:
        investigation_summary = investigation_summary[:197] + "..."

    return {
        "time_window": time_window,
        "entities": entity_identifiers,
        "round": round_number,
        "triggering_connector": triggering_connector,
        "investigation_summary": investigation_summary,
    }

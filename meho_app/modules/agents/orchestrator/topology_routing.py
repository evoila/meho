# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Topology-driven entity routing for cross-system investigation.

Phase 77: Replaces the hardcoded ENTITY_ROUTING_MAP (entity_routing.py) with
real topology lookups via TopologyService. Instead of guessing which connector
type handles an entity type, this module:

1. Uses TopologyService.lookup() to find SAME_AS entities across connectors
2. Queries connector-to-connector relationships (monitors, logs_for, etc.)
   to discover related systems worth investigating
3. Scores connectors by relationship priority (monitors > logs_for > manages > ...)

Flow:
  1. Specialist emits structured discovered_entities (via context_passing.py)
  2. parse_discovered_entities() extracts structured entity dicts
  3. route_via_topology() resolves entities via topology graph, finds related connectors
  4. build_topology_investigation_context() creates context for follow-up specialists

Decisions:
  D-06: Structured entity output replaces UNRESOLVED regex
  D-07: Topology-driven routing replaces ENTITY_ROUTING_MAP
  D-12: ConnectorRelationshipType for traversal priority
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.agents.orchestrator.context_passing import parse_discovered_entities
from meho_app.modules.agents.orchestrator.state import ConnectorSelection
from meho_app.modules.topology.models import TopologyEntityModel, TopologyRelationshipModel
from meho_app.modules.topology.schemas import (
    CONNECTOR_RELATIONSHIP_TYPES,
    ConnectorRelationshipType,
    LookupTopologyInput,
)
from meho_app.modules.topology.service import TopologyService

if TYPE_CHECKING:
    from meho_app.modules.agents.orchestrator.contracts import SubgraphOutput

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Relationship priority scoring
# ---------------------------------------------------------------------------

RELATIONSHIP_PRIORITY: dict[str, float] = {
    "monitors": 1.0,
    "logs_for": 0.9,
    "manages": 0.85,
    "traces_for": 0.8,
    "deploys_to": 0.7,
    "alerts_for": 0.6,
    "tracks_issues_for": 0.4,
}

# Best-effort ISO timestamp extraction for time_window
_ISO_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")


# ---------------------------------------------------------------------------
# Connector relationship discovery
# ---------------------------------------------------------------------------


async def get_connector_relationships(
    connector_id: str,
    tenant_id: str,
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Query topology for connector-to-connector relationships.

    Finds relationships where the given connector's topology entity is
    either the source (from_entity) or target (to_entity). Filters to
    only connector relationship types (monitors, logs_for, etc.).

    Args:
        connector_id: UUID string of the connector.
        tenant_id: Tenant ID for scoping.
        session: Database session.

    Returns:
        List of dicts with: related_connector_id, relationship_type, direction.
    """
    # Find the Connector-type topology entity for this connector
    stmt = select(TopologyEntityModel).where(
        and_(
            TopologyEntityModel.connector_id == connector_id,
            TopologyEntityModel.entity_type == "Connector",
            TopologyEntityModel.tenant_id == tenant_id,
        )
    )
    result = await session.execute(stmt)
    connector_entity = result.scalar_one_or_none()

    if not connector_entity:
        return []

    relationships: list[dict[str, Any]] = []

    # Relationships FROM this connector
    stmt_from = (
        select(TopologyRelationshipModel, TopologyEntityModel)
        .join(
            TopologyEntityModel,
            TopologyRelationshipModel.to_entity_id == TopologyEntityModel.id,
        )
        .where(
            and_(
                TopologyRelationshipModel.from_entity_id == connector_entity.id,
                TopologyRelationshipModel.relationship_type.in_(CONNECTOR_RELATIONSHIP_TYPES),
                TopologyEntityModel.entity_type == "Connector",
                TopologyEntityModel.tenant_id == tenant_id,
            )
        )
    )
    result_from = await session.execute(stmt_from)
    for rel, target_entity in result_from.all():
        if target_entity.connector_id:
            relationships.append(
                {
                    "related_connector_id": str(target_entity.connector_id),
                    "relationship_type": rel.relationship_type,
                    "direction": "from",
                }
            )

    # Relationships TO this connector
    stmt_to = (
        select(TopologyRelationshipModel, TopologyEntityModel)
        .join(
            TopologyEntityModel,
            TopologyRelationshipModel.from_entity_id == TopologyEntityModel.id,
        )
        .where(
            and_(
                TopologyRelationshipModel.to_entity_id == connector_entity.id,
                TopologyRelationshipModel.relationship_type.in_(CONNECTOR_RELATIONSHIP_TYPES),
                TopologyEntityModel.entity_type == "Connector",
                TopologyEntityModel.tenant_id == tenant_id,
            )
        )
    )
    result_to = await session.execute(stmt_to)
    for rel, source_entity in result_to.all():
        if source_entity.connector_id:
            relationships.append(
                {
                    "related_connector_id": str(source_entity.connector_id),
                    "relationship_type": rel.relationship_type,
                    "direction": "to",
                }
            )

    return relationships


# ---------------------------------------------------------------------------
# Topology-driven entity routing
# ---------------------------------------------------------------------------


async def route_via_topology(
    discovered_entities: list[dict[str, Any]],
    tenant_id: str,
    available_connectors: list[dict[str, Any]],
    visited_connectors: dict[str, int],
    session: AsyncSession,
) -> list[ConnectorSelection]:
    """Route discovered entities to connectors via topology graph.

    For each discovered entity:
    1. If entity has connector_id, add that connector directly
    2. Use TopologyService.lookup() to find SAME_AS entities across connectors
    3. Query connector relationships to find related connectors

    Connectors are scored by relationship priority and deduplicated
    when multiple entities point to the same connector.

    Args:
        discovered_entities: Structured entity dicts from parse_discovered_entities.
        tenant_id: Tenant ID for topology scoping.
        available_connectors: List of connector dicts with id, name, connector_type, etc.
        visited_connectors: Map of connector_id -> visit count for re-query detection.
        session: Database session for topology queries.

    Returns:
        Sorted list of ConnectorSelection objects, highest relevance first.
    """
    if not discovered_entities:
        return []

    # Build lookup of available connectors by ID
    available_by_id: dict[str, dict[str, Any]] = {
        c["id"]: c for c in available_connectors
    }

    # Accumulate targets: connector_id -> (reasons, max_score)
    targets: dict[str, dict[str, Any]] = {}

    topology_service = TopologyService(session)

    for entity in discovered_entities:
        entity_name = entity.get("name", "")
        entity_type = entity.get("type", "")
        entity_connector_id = entity.get("connector_id")
        entity_context = entity.get("context", "")

        if not entity_name:
            continue

        # 1. Direct connector_id from entity
        if entity_connector_id and entity_connector_id in available_by_id:
            _add_target(
                targets,
                entity_connector_id,
                reason=f"Direct entity match: {entity_type} '{entity_name}'",
                score=0.95,
            )

        # 2. Topology lookup for SAME_AS entities across connectors
        try:
            lookup_result = await topology_service.lookup(
                LookupTopologyInput(query=entity_name, cross_connectors=True),
                tenant_id=tenant_id,
            )

            if lookup_result.found and lookup_result.same_as_entities:
                for correlated in lookup_result.same_as_entities:
                    corr_entity = correlated.entity
                    if corr_entity.connector_id:
                        cid = str(corr_entity.connector_id)
                        if cid in available_by_id:
                            _add_target(
                                targets,
                                cid,
                                reason=(
                                    f"SAME_AS match for {entity_type} '{entity_name}' "
                                    f"-> {correlated.connector_type} '{corr_entity.name}'"
                                ),
                                score=0.9,
                            )
        except Exception as e:
            logger.warning(
                f"Topology lookup failed for entity '{entity_name}': {e}"
            )

        # 3. Query connector relationships for the entity's source connector
        source_connector_id = entity_connector_id
        if source_connector_id:
            try:
                rels = await get_connector_relationships(
                    source_connector_id, tenant_id, session
                )
                for rel in rels:
                    related_id = rel["related_connector_id"]
                    if related_id in available_by_id:
                        rel_type = rel["relationship_type"]
                        score = RELATIONSHIP_PRIORITY.get(rel_type, 0.5)
                        direction = rel["direction"]

                        related_name = available_by_id[related_id].get("name", related_id)
                        source_name = available_by_id.get(source_connector_id, {}).get(
                            "name", source_connector_id
                        )

                        if direction == "from":
                            reason = (
                                f"{source_name} {rel_type} {related_name} "
                                f"(related via topology)"
                            )
                        else:
                            reason = (
                                f"{related_name} {rel_type} {source_name} "
                                f"(related via topology)"
                            )

                        _add_target(targets, related_id, reason=reason, score=score)
            except Exception as e:
                logger.warning(
                    f"Connector relationship query failed for '{source_connector_id}': {e}"
                )

    # Build ConnectorSelection objects
    selections: list[ConnectorSelection] = []
    for conn_id, target in targets.items():
        conn = available_by_id.get(conn_id)
        if not conn:
            continue

        # Combine reasons (deduplicated)
        combined_reason = "; ".join(target["reasons"][:3])
        visit_count = visited_connectors.get(conn_id, 0)
        if visit_count > 0:
            combined_reason += f" (re-query #{visit_count + 1})"

        selections.append(
            ConnectorSelection(
                connector_id=conn_id,
                connector_name=conn.get("name", ""),
                connector_type=conn.get("connector_type", ""),
                routing_description=conn.get("routing_description", ""),
                relevance_score=target["max_score"],
                reason=combined_reason,
                generated_skill=conn.get("generated_skill"),
                custom_skill=conn.get("custom_skill"),
                skill_name=conn.get("skill_name"),
                base_url=conn.get("base_url"),
            )
        )

    # Sort by relevance_score descending
    selections.sort(key=lambda s: s.relevance_score, reverse=True)

    return selections


def _add_target(
    targets: dict[str, dict[str, Any]],
    connector_id: str,
    reason: str,
    score: float,
) -> None:
    """Add or update a routing target with deduplication.

    If the connector is already targeted, appends the reason and takes
    the maximum score. Avoids duplicate reasons.

    Args:
        targets: Mutable target accumulator.
        connector_id: Connector ID to add.
        reason: Why this connector was selected.
        score: Relevance score for this routing path.
    """
    if connector_id not in targets:
        targets[connector_id] = {"reasons": [reason], "max_score": score}
    else:
        existing = targets[connector_id]
        if reason not in existing["reasons"]:
            existing["reasons"].append(reason)
        existing["max_score"] = max(existing["max_score"], score)


# ---------------------------------------------------------------------------
# Investigation context builder (topology-aware)
# ---------------------------------------------------------------------------


def build_topology_investigation_context(
    findings: list[SubgraphOutput],
    entities: list[dict[str, Any]],
    round_number: int,
    visited_connectors: dict[str, int],
) -> dict[str, Any]:
    """Build investigation context for follow-up specialists.

    Creates a focused context payload using structured entities from
    parse_discovered_entities (instead of the old regex-based extraction).
    Includes visited_connectors so specialists know what was already queried.

    Args:
        findings: SubgraphOutput list from the previous round.
        entities: Structured entity dicts from parse_discovered_entities.
        round_number: Current dispatch round (2+).
        visited_connectors: Map of connector_id -> visit count.

    Returns:
        Investigation context dict with: time_window, entity_identifiers,
        round, triggering_connector, investigation_summary, visited_connectors.
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

    # Collect entity identifiers from structured entities
    entity_identifiers: dict[str, str] = {}
    for entity in entities:
        identifiers = entity.get("identifiers", {})
        name = entity.get("name", "")
        etype = entity.get("type", "")

        # Use structured identifiers from parse_discovered_entities
        for key in ("hostname", "ip", "provider_id", "namespace"):
            if identifiers.get(key) and key not in entity_identifiers:
                entity_identifiers[key] = identifiers[key]

        # Also store entity name by type for lookup
        if etype and name and etype not in entity_identifiers:
            entity_identifiers[etype] = name

    # Determine triggering connector (first successful finding)
    triggering_connector = ""
    for f in findings:
        if f.status == "success" and f.connector_name:
            triggering_connector = f.connector_name
            break

    # Build factual summary under 200 chars
    entity_names = [
        f"{e.get('type')} '{e.get('name')}'"
        for e in entities[:3]
    ]
    summary_parts = []
    if triggering_connector:
        summary_parts.append(f"{triggering_connector} flagged")
    if entity_names:
        summary_parts.append(", ".join(entity_names))
    summary_parts.append(f"for follow-up (round {round_number})")
    investigation_summary = " ".join(summary_parts)
    if len(investigation_summary) > 200:
        investigation_summary = investigation_summary[:197] + "..."

    return {
        "time_window": time_window,
        "entity_identifiers": entity_identifiers,
        "round": round_number,
        "triggering_connector": triggering_connector,
        "investigation_summary": investigation_summary,
        "visited_connectors": dict(visited_connectors),
    }

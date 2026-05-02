# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
TopologyContextNode - Injects topology knowledge at start of agent turn.

This node runs BEFORE the LLM starts reasoning. It:
1. Extracts entity references from the user's message
2. Looks up each entity in the topology database
3. Injects the known topology chain into the agent's context

This enables the agent to start with knowledge of:
- Known entities related to the user's query
- Relationships between entities (routes_to, runs_on, etc.)
- Cross-connector correlations (SAME_AS) with confidence labels
- Freshness timestamps for staleness assessment

Example:
    User: "My website shop.example.com is slow"

    TopologyContextNode extracts: ["shop.example.com"]
    Looks up and finds:
        ## Known Topology

        **shop.example.com** (Ingress) [Production K8s]
        Last seen: 2 hours ago (2026-03-22T14:30:00+00:00)

          Relationships:
            shop.example.com --routes_to--> shop-frontend (Last seen: 2 hours ago)
            shop-frontend --runs_on--> node-01 (Last seen: 1 hour ago)

          SAME_AS:
            == vm-web-01 (VM) [Production vCenter]
               CONFIRMED (providerID match: gce://project/zone/instance)
               Last seen: 4 hours ago (2026-03-22T12:00:00+00:00)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger

from .entity_extractor import extract_entity_references
from .schemas import LookupTopologyInput, TopologyChainItem, TopologyEntity
from .service import TopologyService

logger = get_logger(__name__)


@dataclass
class TopologyContext:
    """
    Topology context discovered for a user message.

    Injected into the agent's system prompt with full neighbor chain,
    freshness timestamps, and SAME_AS confidence markers.
    """

    query: str
    """The entity reference that was looked up"""

    found: bool
    """Whether the entity was found in topology"""

    entity: TopologyEntity | None = None
    """Full entity with timestamps for freshness display"""

    relationships: list[dict] = field(default_factory=list)
    """Direct relationships: [{from_entity, to_entity, relationship_type, last_verified_at}]"""

    same_as_entities: list[dict] = field(default_factory=list)
    """SAME_AS correlations: [{entity: TopologyEntity, connector_name, verified_via, similarity_score}]"""

    connectors: list[str] = field(default_factory=list)
    """Connector IDs traversed in the chain"""

    possibly_related: list[dict[str, Any]] = field(default_factory=list)
    """Entities that might be related (for SAME_AS discovery)"""

    # Legacy compatibility
    chain: list[TopologyChainItem] = field(default_factory=list)
    """The topology chain from this entity (legacy format)"""


def _format_freshness(timestamp: datetime | None) -> str:
    """Format timestamp as relative + absolute label per D-07.

    Returns "X minutes/hours/days ago (ISO timestamp)" or "Unknown".

    Uses:
    - <60s: "just now (ISO)"
    - <3600s: "N minutes ago (ISO)"
    - <86400s: "N hours ago (ISO)"
    - else: "N days ago (ISO)"
    """
    if not timestamp:
        return "Unknown"
    now = datetime.now(tz=UTC)
    delta = now - timestamp
    total_seconds = delta.total_seconds()
    if total_seconds < 60:
        relative = "just now"
    elif total_seconds < 3600:
        relative = f"{int(total_seconds / 60)} minutes ago"
    elif total_seconds < 86400:
        relative = f"{int(total_seconds / 3600)} hours ago"
    else:
        relative = f"{delta.days} days ago"
    return f"{relative} ({timestamp.isoformat()})"


def _format_confidence(verified_via: list[str]) -> str:
    """Format SAME_AS confidence with label + evidence per D-08.

    Maps verification evidence to confidence labels:
    - providerID match -> CONFIRMED
    - IP match -> HIGH
    - hostname match -> MEDIUM
    - everything else -> SUGGESTED

    Uses parse_verification_evidence from topology_utils for parsing.
    """
    from meho_app.modules.agents.shared.topology_utils import (
        parse_verification_evidence,
    )

    confidence_str, evidence_str = parse_verification_evidence(verified_via)

    # Determine the match type from verified_via directly
    match_type = None
    for item in verified_via:
        if item.startswith("match_type:"):
            match_type = item.split(":", 1)[1]
            break

    # Map to D-08 labels based on match_type
    if match_type == "provider_id":
        evidence_display = evidence_str if evidence_str else "providerID"
        return f"CONFIRMED (providerID match: {evidence_display})"
    elif match_type == "ip_address":
        evidence_display = evidence_str if evidence_str else "IP"
        return f"HIGH (IP match: {evidence_display})"
    elif match_type == "hostname":
        evidence_display = evidence_str if evidence_str else "hostname"
        return f"MEDIUM (hostname partial: {evidence_display})"
    else:
        # Fallback: use the parsed confidence label
        evidence_display = evidence_str if evidence_str else confidence_str
        return f"SUGGESTED ({evidence_display})"


def format_topology_context_for_prompt(  # NOSONAR (cognitive complexity)
    contexts: list[TopologyContext],
    token_budget: int = 2000,
) -> str:
    """Format topology contexts with freshness and confidence for agent prompt.

    Produces rich neighbor chain context per D-06, D-07, D-08:
    - Entity header with type, connector, and freshness
    - Direct relationships (capped at 10 per entity)
    - SAME_AS links with confidence labels (capped at 5 per entity)
    - Possibly related entities (capped at 3)
    - Soft token cap with priority-based truncation

    Args:
        contexts: List of TopologyContext from lookups.
        token_budget: Soft token cap (~4 chars/token). Default 2000 tokens.

    Returns:
        Formatted string for the system prompt, or "" if no found contexts.
    """
    if not contexts:
        return ""

    found_contexts = [c for c in contexts if c.found]
    if not found_contexts:
        return ""

    char_budget = token_budget * 4  # ~4 chars per token estimate
    lines: list[str] = ["## Known Topology", ""]

    for ctx in found_contexts:
        entity = ctx.entity
        if not entity:
            # Fallback for legacy contexts without entity
            lines.append(f"**{ctx.query}**")
            lines.append("")
            continue

        # Entity header with connector name and freshness
        connector_display = entity.connector_name or entity.connector_type or "unknown"
        freshness = _format_freshness(entity.last_verified_at or entity.discovered_at)
        lines.append(f"**{entity.name}** ({entity.entity_type}) [{connector_display}]")
        lines.append(f"Last seen: {freshness}")
        lines.append("")

        # Direct relationships (capped at 10)
        if ctx.relationships:
            lines.append("  Relationships:")
            for rel in ctx.relationships[:10]:
                rel_freshness = _format_freshness(rel.get("last_verified_at"))
                from_entity = rel.get("from_entity", "?")
                to_entity = rel.get("to_entity", "?")
                rel_type = rel.get("relationship_type", "?")
                lines.append(
                    f"    {from_entity} --{rel_type}--> {to_entity} (Last seen: {rel_freshness})"
                )
            if len(ctx.relationships) > 10:
                lines.append(f"    ... and {len(ctx.relationships) - 10} more")
            lines.append("")

        # SAME_AS with confidence labels (capped at 5)
        if ctx.same_as_entities:
            lines.append("  SAME_AS:")
            for correlated in ctx.same_as_entities[:5]:
                corr_entity = correlated.get("entity")
                if not corr_entity:
                    continue
                corr_connector = correlated.get("connector_name") or "unknown"
                confidence_label = _format_confidence(correlated.get("verified_via", []))
                corr_freshness = _format_freshness(getattr(corr_entity, "last_verified_at", None))
                lines.append(
                    f"    == {corr_entity.name} ({corr_entity.entity_type}) [{corr_connector}]"
                )
                lines.append(f"       {confidence_label}")
                lines.append(f"       Last seen: {corr_freshness}")
            lines.append("")

        # Possibly related (capped at 3)
        if ctx.possibly_related:
            lines.append("  Possibly related (unverified):")
            for related in ctx.possibly_related[:3]:
                entity_name = related.get("entity", "?")
                similarity = related.get("similarity", 0)
                lines.append(f"    - {entity_name} (similarity: {similarity:.2f})")
            lines.append("")

    lines.append(
        "Use this knowledge to guide your investigation. Start by checking these known paths."
    )
    lines.append("")

    # Token budget truncation (approximate)
    result = "\n".join(lines)
    if len(result) > char_budget:
        result = result[:char_budget] + "\n... (truncated for token budget)"

    return result


class TopologyContextService:
    """
    Service for building topology context for a user message.

    Called at the start of each agent turn to inject known topology.
    Produces enhanced context with full neighbor chain, freshness, and confidence.
    """

    def __init__(
        self,
        session: AsyncSession,
        topology_service: TopologyService | None = None,
    ) -> None:
        self.session = session
        self.topology_service = topology_service

    async def build_context(  # NOSONAR (cognitive complexity)
        self,
        user_message: str,
        tenant_id: str,
        max_entities: int = 5,
        traverse_depth: int = 10,
    ) -> list[TopologyContext]:
        """
        Build topology context for a user message.

        1. Extracts entity references from the message
        2. Looks up each entity in the topology database
        3. Fetches relationships and SAME_AS for found entities
        4. Returns context with full neighbor chain

        Args:
            user_message: The user's message
            tenant_id: Tenant for multi-tenancy
            max_entities: Maximum entities to look up
            traverse_depth: Maximum depth for graph traversal

        Returns:
            List of TopologyContext for found entities
        """
        if not self.topology_service:
            logger.debug("Topology service not available, skipping context")
            return []

        # Extract entity references
        entity_refs = extract_entity_references(user_message)

        if not entity_refs:
            logger.debug("No entity references found in message")
            return []

        logger.info(f"Found {len(entity_refs)} entity references: {entity_refs[:5]}")

        # Limit lookups
        entity_refs = entity_refs[:max_entities]

        # Look up each entity
        contexts: list[TopologyContext] = []

        for ref in entity_refs:
            try:
                result = await self.topology_service.lookup(
                    input=LookupTopologyInput(
                        query=ref,
                        traverse_depth=traverse_depth,
                        cross_connectors=True,
                    ),
                    tenant_id=tenant_id,
                )

                # Build enhanced context with relationships and SAME_AS
                relationships: list[dict] = []
                same_as_entities: list[dict] = []

                if result.found and result.entity:
                    # Fetch relationships for the found entity
                    try:
                        from .repository import TopologyRepository

                        repo = TopologyRepository(self.session)

                        # Get relationships FROM this entity
                        rels_from = await repo.get_relationships_from(result.entity.id)
                        for rel in rels_from:
                            to_ent = rel.to_entity
                            relationships.append(
                                {
                                    "from_entity": result.entity.name,
                                    "to_entity": (to_ent.name if to_ent else str(rel.to_entity_id)),
                                    "relationship_type": rel.relationship_type,
                                    "last_verified_at": rel.last_verified_at,
                                }
                            )

                        # Get relationships TO this entity
                        rels_to = await repo.get_relationships_to(result.entity.id)
                        for rel in rels_to:
                            from_ent = rel.from_entity
                            relationships.append(
                                {
                                    "from_entity": (
                                        from_ent.name if from_ent else str(rel.from_entity_id)
                                    ),
                                    "to_entity": result.entity.name,
                                    "relationship_type": rel.relationship_type,
                                    "last_verified_at": rel.last_verified_at,
                                }
                            )

                    except Exception as e:
                        logger.debug(f"Failed to fetch relationships for {ref}: {e}")

                    # Build SAME_AS from lookup result
                    for correlated in result.same_as_entities:
                        same_as_entities.append(
                            {
                                "entity": correlated.entity,
                                "connector_name": correlated.connector_name
                                or correlated.connector_type,
                                "verified_via": correlated.verified_via,
                                "similarity_score": 0.0,  # Not available from CorrelatedEntity
                            }
                        )

                contexts.append(
                    TopologyContext(
                        query=ref,
                        found=result.found,
                        entity=result.entity,
                        relationships=relationships,
                        same_as_entities=same_as_entities,
                        chain=result.topology_chain,
                        connectors=result.connectors_traversed,
                        possibly_related=[
                            {
                                "entity": p.entity,
                                "similarity": p.similarity,
                            }
                            for p in result.possibly_related
                        ],
                    )
                )

                if result.found:
                    logger.info(
                        f"Found topology for {ref}: "
                        f"{len(relationships)} relationships, "
                        f"{len(same_as_entities)} SAME_AS"
                    )

            except Exception as e:
                logger.warning(f"Failed to lookup topology for {ref}: {e}")
                continue

        return contexts

    def format_for_prompt(self, contexts: list[TopologyContext]) -> str:
        """Format contexts for injection into system prompt."""
        return format_topology_context_for_prompt(contexts)


def get_topology_context_service(
    session: AsyncSession,
) -> TopologyContextService:
    """Get a TopologyContextService for dependency injection."""
    from .service import TopologyService

    topology_service = TopologyService(session)
    return TopologyContextService(session, topology_service)

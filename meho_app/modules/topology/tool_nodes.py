# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Topology agent tool nodes.

These nodes enable the agent to:
1. Look up known topology (lookup_topology)
2. Store discovered topology (store_discovery)
3. Invalidate stale topology (invalidate_topology)

Following the pydantic-graph pattern from existing tool nodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic_graph import BaseNode, GraphRunContext

from meho_app.core.otel import get_logger

if TYPE_CHECKING:
    from meho_app.modules.agents.shared.graph.nodes.loop_detection_node import LoopDetectionNode
    from meho_app.modules.agents.shared.graph.nodes.reason_node import ReasonNode

from meho_app.modules.agents.shared.graph.graph_deps import MEHOGraphDeps
from meho_app.modules.agents.shared.graph.graph_state import MEHOGraphState

from .schemas import (
    InvalidateTopologyInput,
    LookupTopologyInput,
    StoreDiscoveryInput,
    TopologyEntityCreate,
    TopologyRelationshipCreate,
    TopologySameAsCreate,
)

logger = get_logger(__name__)


@dataclass
class LookupTopologyNode(BaseNode[MEHOGraphState, MEHOGraphDeps, None]):
    """
    Look up known topology for an entity.

    Returns the full topology chain from the entity,
    traversing relationships and SAME_AS links.

    Tool signature:
        lookup_topology(query: str, traverse_depth: int = 10, cross_connectors: bool = True)

    Example:
        {"query": "shop.example.com", "traverse_depth": 10, "cross_connectors": true}
    """

    query: str
    traverse_depth: int = 10
    cross_connectors: bool = True

    async def run(
        self, ctx: GraphRunContext[MEHOGraphState, MEHOGraphDeps]
    ) -> ReasonNode | LoopDetectionNode:
        """Execute lookup_topology and return result."""
        from meho_app.modules.agents.shared.graph.nodes.loop_detection_node import LoopDetectionNode

        state = ctx.state
        deps = ctx.deps

        logger.info(f"LookupTopologyNode: query='{self.query}'")

        # Record action for loop detection
        state.record_action("lookup_topology", {"query": self.query})

        # Get topology service from dependencies
        result = await self._execute_lookup(deps)

        # Update state
        state.add_to_scratchpad(f"Observation: {result}")
        state.last_observation = result
        state.step_count += 1
        state.pending_tool = None
        state.pending_args = None

        # Go through loop detection before reasoning again
        return LoopDetectionNode()

    async def _execute_lookup(self, deps: MEHOGraphDeps) -> str:
        """Execute the topology lookup."""
        try:
            # Get session from meho_deps
            if not deps.meho_deps:
                return "Error: No MEHO dependencies available"

            # Check if topology service is available
            session = getattr(deps.meho_deps, "db_session", None)
            if not session:
                return "Topology lookup not available (no database session)"

            tenant_id = getattr(deps.meho_deps, "tenant_id", "default")

            from .service import TopologyService

            topology_service = TopologyService(session)

            result = await topology_service.lookup(
                input=LookupTopologyInput(
                    query=self.query,
                    traverse_depth=self.traverse_depth,
                    cross_connectors=self.cross_connectors,
                ),
                tenant_id=tenant_id,
            )

            if not result.found:
                suggestions = "\n".join(f"- {s}" for s in result.suggestions)
                return (
                    f"Entity '{self.query}' not found in topology.\n\nSuggestions:\n{suggestions}"
                )

            # Format the result
            lines = [f"Found topology for '{self.query}':"]
            lines.append("")

            for item in result.topology_chain:
                indent = "  " * item.depth
                rel_str = f" ({item.relationship})" if item.relationship else ""
                lines.append(f"{indent}→ {item.entity}{rel_str}")

            if result.connectors_traversed:
                lines.append("")
                lines.append(f"Connectors: {', '.join(result.connectors_traversed)}")

            # Show confirmed SAME_AS entities (cross-connector correlations)
            if result.same_as_entities:
                lines.append("")
                lines.append("CONFIRMED SAME_AS (this entity IS the same physical resource as):")
                for correlated in result.same_as_entities[:5]:
                    entity = correlated.entity
                    connector = correlated.connector_name or correlated.connector_type
                    lines.append(f"  - {entity.name} ({entity.entity_type}) via {connector}")
                    if correlated.verified_via:
                        lines.append(f"    Verified by: {', '.join(correlated.verified_via[:3])}")
                lines.append("  → Query BOTH connectors for comprehensive diagnostics")

            if result.possibly_related:
                lines.append("")
                lines.append("Possibly related (verify via API before storing SAME_AS):")
                for related in result.possibly_related[:5]:
                    lines.append(f"  - {related.entity}: similarity {related.similarity:.2f}")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"Topology lookup failed: {e}", exc_info=True)
            return f"Topology lookup failed: {e!s}"


@dataclass
class StoreDiscoveryNode(BaseNode[MEHOGraphState, MEHOGraphDeps, None]):
    """
    Store discovered topology (entities, relationships, SAME_AS).

    The agent calls this after investigating systems to remember
    what it learned for future requests.

    Tool signature:
        store_discovery(entities: list, relationships: list, same_as: list)

    Example:
        {
            "entities": [
                {"name": "shop-ingress", "type": "Ingress", "connector_id": "...",
                 "description": "K8s Ingress in namespace ecommerce"}
            ],
            "relationships": [
                {"from": "shop.example.com", "to": "shop-ingress", "type": "resolves_to"}
            ],
            "same_as": [
                {"entity_a": "node-01", "entity_b": "k8s-worker-01",
                 "similarity_score": 0.87, "verified_via": ["IP: 192.168.1.10"]}
            ]
        }

    IMPORTANT: same_as requires verified_via - the agent must verify via API first!
    """

    entities: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    same_as: list[dict[str, Any]] = field(default_factory=list)

    async def run(
        self, ctx: GraphRunContext[MEHOGraphState, MEHOGraphDeps]
    ) -> ReasonNode | LoopDetectionNode:
        """Execute store_discovery and return result."""
        from meho_app.modules.agents.shared.graph.nodes.loop_detection_node import LoopDetectionNode

        state = ctx.state
        deps = ctx.deps

        logger.info(
            f"StoreDiscoveryNode: {len(self.entities)} entities, {len(self.relationships)} relationships"
        )

        # Record action for loop detection
        state.record_action(
            "store_discovery",
            {
                "entity_count": len(self.entities),
                "relationship_count": len(self.relationships),
                "same_as_count": len(self.same_as),
            },
        )

        # Execute store
        result = await self._execute_store(deps)

        # Update state
        state.add_to_scratchpad(f"Observation: {result}")
        state.last_observation = result
        state.step_count += 1
        state.pending_tool = None
        state.pending_args = None

        # Go through loop detection before reasoning again
        return LoopDetectionNode()

    async def _execute_store(self, deps: MEHOGraphDeps) -> str:
        """Execute the topology store."""
        try:
            if not deps.meho_deps:
                return "Error: No MEHO dependencies available"

            session = getattr(deps.meho_deps, "db_session", None)
            if not session:
                return "Topology store not available (no database session)"

            tenant_id = getattr(deps.meho_deps, "tenant_id", "default")

            from .service import TopologyService

            topology_service = TopologyService(session)

            # Convert raw dicts to proper schemas
            entity_creates = []
            for e in self.entities:
                entity_creates.append(
                    TopologyEntityCreate(
                        name=e.get("name", ""),
                        connector_id=e.get("connector_id"),
                        description=e.get("description", e.get("name", "")),
                        raw_attributes=e.get("raw_attributes", e.get("attributes", {})),
                    )
                )

            relationship_creates = []
            for r in self.relationships:
                relationship_creates.append(
                    TopologyRelationshipCreate(
                        from_entity_name=r.get("from", r.get("from_entity_name", "")),
                        to_entity_name=r.get("to", r.get("to_entity_name", "")),
                        relationship_type=r.get("type", r.get("relationship_type", "relates_to")),
                    )
                )

            same_as_creates = []
            for s in self.same_as:
                if not s.get("verified_via"):
                    logger.warning(f"Skipping SAME_AS without verified_via: {s}")
                    continue
                same_as_creates.append(
                    TopologySameAsCreate(
                        entity_a_name=s.get("entity_a", s.get("entity_a_name", "")),
                        entity_b_name=s.get("entity_b", s.get("entity_b_name", "")),
                        similarity_score=s.get("similarity_score", 0.8),
                        verified_via=s.get("verified_via", []),
                    )
                )

            result = await topology_service.store_discovery(
                input=StoreDiscoveryInput(
                    entities=entity_creates,
                    relationships=relationship_creates,
                    same_as=same_as_creates,
                ),
                tenant_id=tenant_id,
            )

            return result.message

        except Exception as e:
            logger.error(f"Topology store failed: {e}", exc_info=True)
            return f"Topology store failed: {e!s}"


@dataclass
class InvalidateTopologyNode(BaseNode[MEHOGraphState, MEHOGraphDeps, None]):
    """
    Invalidate a stale topology entity.

    Called when the agent detects that stored topology no longer
    matches reality (e.g., 404 from API, entity deleted).

    Tool signature:
        invalidate_topology(entity_name: str, reason: str)

    Example:
        {"entity_name": "shop-ingress", "reason": "Not found in K8s API (404)"}
    """

    entity_name: str
    reason: str

    async def run(
        self, ctx: GraphRunContext[MEHOGraphState, MEHOGraphDeps]
    ) -> ReasonNode | LoopDetectionNode:
        """Execute invalidate_topology and return result."""
        from meho_app.modules.agents.shared.graph.nodes.loop_detection_node import LoopDetectionNode

        state = ctx.state
        deps = ctx.deps

        logger.info(f"InvalidateTopologyNode: entity='{self.entity_name}', reason='{self.reason}'")

        # Record action for loop detection
        state.record_action("invalidate_topology", {"entity_name": self.entity_name})

        # Execute invalidate
        result = await self._execute_invalidate(deps)

        # Update state
        state.add_to_scratchpad(f"Observation: {result}")
        state.last_observation = result
        state.step_count += 1
        state.pending_tool = None
        state.pending_args = None

        # Go through loop detection before reasoning again
        return LoopDetectionNode()

    async def _execute_invalidate(self, deps: MEHOGraphDeps) -> str:
        """Execute the topology invalidation."""
        try:
            if not deps.meho_deps:
                return "Error: No MEHO dependencies available"

            session = getattr(deps.meho_deps, "db_session", None)
            if not session:
                return "Topology invalidation not available (no database session)"

            tenant_id = getattr(deps.meho_deps, "tenant_id", "default")

            from .service import TopologyService

            topology_service = TopologyService(session)

            result = await topology_service.invalidate(
                input=InvalidateTopologyInput(
                    entity_name=self.entity_name,
                    reason=self.reason,
                ),
                tenant_id=tenant_id,
            )

            return result.message

        except Exception as e:
            logger.error(f"Topology invalidation failed: {e}", exc_info=True)
            return f"Topology invalidation failed: {e!s}"

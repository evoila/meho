# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
TopologyLookupNode - LLM-powered topology lookup at start of turn.

This node runs BEFORE ReasonNode to check what the agent already knows
about entities mentioned in the user's message. It uses an LLM to:
1. Understand what the user is asking about
2. Extract entity references (names, types, systems)
3. Query the topology database for known entities
4. Inject relevant context into the state for ReasonNode

This enables the agent to recall previously learned topology without
needing to query APIs again.

NOTE: This node is part of the old ReAct graph (pydantic-graph architecture) and is
actively used when the OrchestratorAgent runs topology lookups via the graph path.
It is NOT dead code. The SpecialistAgent uses a separate topology pre-population
mechanism (specialist_agent/agent.py:_format_topology_context). Both paths coexist.

TRACING: Enhanced with comprehensive OTEL tracing for:
- Entity extraction from user message (LLM call)
- Database lookups with entity details
- Context injection decisions
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_graph import BaseNode, GraphRunContext

from meho_app.core.otel import get_logger, span
from meho_app.modules.agents.persistence.event_context import get_transcript_collector

# Import tracing utilities
from meho_app.modules.agents.shared.handlers.tracing import trace_topology_lookup

if TYPE_CHECKING:
    from meho_app.modules.agents.shared.graph.nodes.reason_node import ReasonNode

from meho_app.modules.agents.shared.graph.graph_deps import MEHOGraphDeps
from meho_app.modules.agents.shared.graph.graph_state import MEHOGraphState
from meho_app.modules.agents.shared.topology_utils import (
    extract_key_attributes,
    parse_verification_evidence,
)

logger = get_logger(__name__)


# =============================================================================
# Pydantic models for structured LLM output
# =============================================================================


class EntityReference(BaseModel):
    """An entity reference extracted from the user's message."""

    name: str = Field(..., description="Entity name or identifier mentioned")
    entity_type: str | None = Field(
        None, description="Type if mentioned: VM, Node, Pod, Service, Ingress, etc."
    )
    system: str | None = Field(
        None, description="System/connector if mentioned: Proxmox, Kubernetes, vSphere, etc."
    )


class ExtractedReferences(BaseModel):
    """Entities the user is asking about."""

    entities: list[EntityReference] = Field(
        default_factory=list, description="Entity references found in the message"
    )
    is_topology_relevant: bool = Field(
        True, description="False if this is a general question not about specific infrastructure"
    )


# =============================================================================
# Extraction prompt
# =============================================================================

EXTRACTION_PROMPT = """Analyze this user message and extract any infrastructure entity references.

User message: {message}

Extract:
1. **Entity names**: Specific names of VMs, servers, pods, services, nodes, etc.
2. **Entity types**: VM, Node, Pod, Service, Ingress, Datastore, Network, etc.
3. **Systems**: Proxmox, Kubernetes, vSphere, AWS, Azure, etc.

Examples:
- "What's the status of DEV-gameflow-db?" → entities: [{{name: "DEV-gameflow-db", type: "VM"}}]
- "Show me pods in the shop namespace" → entities: [{{name: "shop", type: "Namespace"}}]
- "Which node runs the API gateway?" → entities: [{{name: "API gateway", type: null}}]
- "How do I create a VM?" → is_topology_relevant: false (general question)

Only extract specific infrastructure entities, not general concepts."""


# =============================================================================
# TopologyLookupNode
# =============================================================================


@dataclass
class TopologyLookupNode(BaseNode[MEHOGraphState, MEHOGraphDeps, None]):
    """
    LLM-powered topology lookup at start of turn.

    Runs before ReasonNode to check what the agent already knows.
    Uses LLM to understand the query and extract entity references,
    then looks them up in the topology database.
    """

    async def run(self, ctx: GraphRunContext[MEHOGraphState, MEHOGraphDeps]) -> ReasonNode:
        """Extract entity references and lookup known topology."""
        from meho_app.modules.agents.shared.graph.nodes.reason_node import ReasonNode

        state = ctx.state
        deps = ctx.deps

        # Track timing
        start_time = time.perf_counter()

        # Track results for logging
        result_info = {
            "user_message": state.user_goal[:100] if state.user_goal else None,
            "entities_extracted": 0,
            "entities_found": 0,
            "context_injected": False,
            "skipped_reason": None,
        }

        # Track extracted entity names for final trace
        extracted_entity_names: list[str] = []
        found_entities: list[dict] = []

        try:
            # Get db_session from dependencies
            db_session = deps.meho_deps.db_session if deps.meho_deps else None

            if not db_session:
                result_info["skipped_reason"] = "no_db_session"
                logger.debug("No db_session available, skipping topology lookup")
                self._log_result(result_info)
                return ReasonNode()

            # Use LLM to extract entity references
            with span(
                "meho.topology.extract_refs",
                user_id=str(deps.user_id) if deps.user_id else None,
                tenant_id=str(deps.tenant_id) if deps.tenant_id else None,
                session_id=str(deps.session_id) if deps.session_id else None,
                user_message=state.user_goal[:100] if state.user_goal else None,
            ):
                references = await self._extract_entity_references(state.user_goal)
                if references and references.entities:
                    result_info["entities_extracted"] = len(references.entities)
                    logger.info(
                        f"Extracted {len(references.entities)} entity references",
                        entities=[e.name for e in references.entities],
                        is_topology_relevant=references.is_topology_relevant,
                    )

            if not references or not references.is_topology_relevant or not references.entities:
                result_info["skipped_reason"] = "no_relevant_entities"
                logger.debug("No topology-relevant entities in user message")
                self._log_result(result_info, start_time, extracted_entity_names, found_entities)
                return ReasonNode()

            # Track extracted entity names
            extracted_entity_names = [e.name for e in references.entities]

            logger.info(f"TopologyLookupNode: Found {len(references.entities)} entity references")

            # Get tenant_id from deps (correct source)
            tenant_id = deps.tenant_id

            # Lookup each entity in topology
            context_parts = []
            with span(
                "meho.topology.lookup",
                tenant_id=str(tenant_id) if tenant_id else None,
                entity_count=len(references.entities),
            ):
                for ref in references.entities:
                    result = await self._lookup_entity(ref, tenant_id, db_session)
                    if result:
                        context_parts.append(result)
                result_info["entities_found"] = len(context_parts)
                logger.info(
                    f"Found {len(context_parts)}/{len(references.entities)} entities in topology DB",
                    found_entities=[p["entity"].name for p in context_parts]
                    if context_parts
                    else [],
                )

            if context_parts:
                # Track found entities
                found_entities = [
                    {
                        "name": part["entity"].name,
                        "type": part["entity"].entity_type,
                        "connector_id": str(part["entity"].connector_id)
                        if part["entity"].connector_id
                        else None,
                    }
                    for part in context_parts
                ]

                # Inject into state
                state.topology_context = self._format_context(context_parts)
                result_info["context_injected"] = True
                logger.info(
                    f"Injected topology context for {len(context_parts)} entities",
                    context_preview=state.topology_context[:500]
                    if state.topology_context
                    else None,
                    found_entities=found_entities,
                )
            else:
                result_info["skipped_reason"] = "no_entities_in_db"

        except Exception as e:
            # Non-blocking - log and continue to ReasonNode
            result_info["skipped_reason"] = f"error: {e!s}"
            logger.warning(f"Topology lookup failed (non-blocking): {e}")
            logger.error(f"Topology lookup failed: {e}", exception=str(e))
            # Rollback to prevent "transaction aborted" errors in subsequent queries
            try:
                if db_session:
                    await db_session.rollback()
            except Exception:  # noqa: S110 -- intentional silent exception handling
                pass  # Ignore rollback errors

        await self._log_result(result_info, start_time, extracted_entity_names, found_entities)
        return ReasonNode()

    async def _log_result(
        self,
        result_info: dict,
        start_time: float,
        extracted_entities: list[str],
        found_entities: list[dict],
    ) -> None:
        """Log the final result summary with timing and entity details."""
        duration_ms = (time.perf_counter() - start_time) * 1000

        logger.info(
            f"TopologyLookup complete: {len(extracted_entities)} extracted, {len(found_entities)} found in {duration_ms:.1f}ms",
            extracted=len(extracted_entities),
            found=len(found_entities),
            duration_ms=duration_ms,
            **result_info,
        )

        # Also use the centralized tracing utility
        trace_topology_lookup(
            query=result_info.get("user_message", ""),
            entities_extracted=extracted_entities,
            entities_found=found_entities,
            context_injected=result_info.get("context_injected"),
            duration_ms=duration_ms,
        )

        # Emit transcript event for deep observability
        try:
            collector = get_transcript_collector()
            if collector:
                event = collector.create_topology_lookup_event(
                    summary=f"Topology lookup: {len(extracted_entities)} extracted, {len(found_entities)} found",
                    query=result_info.get("user_message", ""),
                    found=len(found_entities) > 0,
                    duration_ms=duration_ms,
                    node_name="topology_lookup",
                )
                await collector.add(event)
        except Exception:  # noqa: S110 -- intentional silent exception handling
            pass  # Non-blocking: don't let event emission break the node

    async def _extract_entity_references(self, message: str) -> ExtractedReferences | None:
        """Use LLM to extract entity references from user message."""
        try:
            from pydantic_ai import InstrumentationSettings

            from meho_app.core.config import get_config

            config = get_config()
            agent = Agent(
                config.classifier_model,
                output_type=ExtractedReferences,
                instructions="You extract infrastructure entity references from user messages. Be concise.",
                instrument=InstrumentationSettings(),
            )

            prompt = EXTRACTION_PROMPT.format(message=message)
            result = await agent.run(prompt)

            return result.output

        except Exception as e:
            logger.warning(f"Entity extraction failed: {e}")
            return None

    async def _lookup_entity(self, ref: EntityReference, tenant_id: str, db_session) -> dict | None:
        """Lookup a single entity in the topology database."""
        try:
            from meho_app.modules.topology import TopologyService
            from meho_app.modules.topology.schemas import LookupTopologyInput

            service = TopologyService(db_session)

            # Create lookup input
            lookup_input = LookupTopologyInput(
                query=ref.name,
                traverse_depth=5,  # Don't go too deep for initial lookup
                cross_connectors=True,
            )

            result = await service.lookup(lookup_input, tenant_id)

            if result.found and result.entity:
                return {
                    "entity": result.entity,
                    "chain": result.topology_chain,
                    "same_as": result.same_as_entities,
                    "related": result.possibly_related,
                }

            return None

        except Exception as e:
            logger.debug(f"Lookup failed for {ref.name}: {e}")
            # Rollback to prevent "transaction aborted" errors in subsequent queries
            try:  # noqa: SIM105 -- explicit error handling preferred
                await db_session.rollback()
            except Exception:  # noqa: S110 -- intentional silent exception handling
                pass  # Ignore rollback errors
            return None

    def _format_context(self, context_parts: list[dict]) -> str:
        """Format topology context for injection into system prompt."""
        lines = ["## Known Topology (from previous investigations)"]
        lines.append(
            "Use this information to skip discovery steps - go directly to relevant operations."
        )
        lines.append("")

        for part in context_parts:
            entity = part["entity"]
            lines.append(f"### {entity.name} ({entity.entity_type})")

            # Show connector info - try to get name from chain or show ID
            connector_info = self._get_connector_info(entity, part.get("chain", []))
            lines.append(f"- **Managed by connector**: {connector_info}")
            lines.append(f"- **Description**: {entity.description}")

            # Show key attributes that help with API calls (vmid, node, etc.)
            if entity.raw_attributes:
                key_attrs = extract_key_attributes(entity.raw_attributes)
                if key_attrs:
                    lines.append(f"- **Key identifiers**: {key_attrs}")

            if part["chain"]:
                chain_str = " → ".join(
                    [
                        f"{item.entity} ({item.entity_type})"
                        for item in part["chain"][:5]  # Limit chain length
                    ]
                )
                lines.append(f"- **Topology chain**: {chain_str}")
                # Show cross-connector chain items (SAME_AS entities in the chain)
                chain_same_as_lines = self._format_chain_same_as_context(part["chain"], entity)
                lines.extend(chain_same_as_lines)

            # Add schema navigation hints (TASK-158)
            self._add_schema_hints(lines, entity)

            # Show confirmed SAME_AS entities (cross-connector correlations)
            same_as = part.get("same_as", [])
            if same_as:
                lines.append("")
                lines.append("#### Cross-System Identity (SAME_AS)")
                lines.append("This entity IS the same physical resource as:")
                for correlated in same_as[:5]:  # Limit to 5 correlations
                    corr_entity = correlated.entity
                    connector_display = correlated.connector_name or correlated.connector_type
                    connector_id_str = (
                        str(corr_entity.connector_id) if corr_entity.connector_id else "unknown"
                    )
                    lines.append(
                        f"  - **{corr_entity.name}** ({corr_entity.entity_type}) via {connector_display}"
                    )
                    lines.append(f"    connector_id: {connector_id_str}")
                    # Parse confidence and evidence from verified_via
                    confidence, evidence_summary = parse_verification_evidence(
                        correlated.verified_via
                    )
                    lines.append(f"    Match confidence: {confidence}")
                    if evidence_summary:
                        lines.append(f"    Evidence: {evidence_summary}")
                    # Extract key identifiers from the correlated entity
                    if corr_entity.raw_attributes:
                        corr_key_attrs = extract_key_attributes(corr_entity.raw_attributes)
                        if corr_key_attrs:
                            lines.append(f"    Key identifiers: {corr_key_attrs}")
                lines.append(
                    "  → Investigate linked entities using their connector_id with search_operations and call_operation"
                )

            # Show possibly related (not yet confirmed)
            if part["related"]:
                related_str = ", ".join(
                    [
                        f"{r.entity} ({r.entity_type}, {r.similarity:.0%} match)"
                        for r in part["related"][:3]  # Limit related count
                    ]
                )
                lines.append(f"- **Possibly related** (not yet confirmed): {related_str}")

            lines.append("")

        return "\n".join(lines)

    def _format_chain_same_as_context(self, chain: list, entity) -> list[str]:
        """Render SAME_AS info for entities discovered through the topology chain.

        TopologyChainItem does not carry SAME_AS metadata directly. This method
        adds a note when chain items come from different connectors than the
        primary entity, indicating potential cross-system availability.

        Args:
            chain: List of TopologyChainItem from the traversal.
            entity: The primary looked-up TopologyEntity.

        Returns:
            List of context lines to append. Empty if no cross-connector
            chain items are found.
        """
        if not chain:
            return []

        primary_connector_id = str(entity.connector_id) if entity.connector_id else None
        cross_connector_items = []
        for item in chain:
            item_connector_id = str(item.connector_id) if item.connector_id else None
            if item_connector_id and item_connector_id != primary_connector_id:
                cross_connector_items.append(item)

        if not cross_connector_items:
            return []

        lines = []
        lines.append("")
        lines.append("**Cross-connector chain items [via SAME_AS hop]:**")
        for item in cross_connector_items[:5]:
            connector_label = item.connector or f"connector:{item.connector_id}"
            lines.append(
                f"  - {item.entity} ({item.entity_type}) via {connector_label} [connector_id: {item.connector_id}]"
            )
        lines.append("  Items reached via SAME_AS hops are available on other connectors.")
        return lines

    def _get_connector_info(self, entity, chain: list) -> str:
        """Extract connector info from entity or chain."""
        # First, try the entity's connector_name field
        if hasattr(entity, "connector_name") and entity.connector_name:
            return entity.connector_name

        # Try to find connector name in chain items
        for item in chain:
            if item.connector:
                return item.connector

        # Fall back to connector_id or External
        if entity.connector_id:
            return f"connector:{entity.connector_id}"
        return "External/Unknown"

    def _add_schema_hints(self, lines: list[str], entity) -> None:
        """
        Add schema navigation hints and common queries to the context.

        Looks up the entity's connector_type in the topology schema registry
        and appends navigation hints and common queries if available.

        Args:
            lines: List of output lines to append to
            entity: The topology entity with connector_type and entity_type
        """
        from meho_app.modules.topology.schema import get_topology_schema

        connector_type = entity.connector_type  # Required field on TopologyEntity
        if not connector_type:
            return

        schema = get_topology_schema(connector_type)
        if not schema:
            # REST/SOAP connectors have no schema - gracefully skip
            return

        entity_def = schema.get_entity_definition(entity.entity_type)
        if not entity_def:
            # Unknown entity type in this schema - gracefully skip
            return

        # Add navigation hints
        if entity_def.navigation_hints:
            lines.append("")
            lines.append("**How to navigate:**")
            for hint in entity_def.navigation_hints:
                lines.append(f"  - {hint}")

        # Add common queries
        if entity_def.common_queries:
            lines.append("")
            lines.append("**Common questions you can answer:**")
            for query in entity_def.common_queries:
                lines.append(f"  - {query}")

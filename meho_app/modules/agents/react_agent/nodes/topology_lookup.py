# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""TopologyLookupNode - Injects known topology context at start of turn.

This node runs at the beginning of each agent turn to check if we already
know about entities mentioned in the user's message, providing relevant
context to the reasoning step.

Delegates all formatting to the centralized TopologyContextService and
format_topology_context_for_prompt in context_node.py, which produces
rich neighbor chain context with freshness and confidence markers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from meho_app.core.otel import get_logger
from meho_app.modules.agents.base.node import BaseNode, NodeResult
from meho_app.modules.agents.shared.topology_utils import (
    extract_entity_mentions,
)

if TYPE_CHECKING:
    from meho_app.modules.agents.react_agent.state import ReactAgentState
    from meho_app.modules.agents.sse.emitter import EventEmitter

logger = get_logger(__name__)


@dataclass
class TopologyLookupNode(BaseNode["ReactAgentState"]):
    """Topology lookup node - provides context from learned topology.

    This node:
    1. Extracts entity mentions from user message
    2. Delegates to TopologyContextService for lookup + formatting
    3. Injects rich context (neighbor chain, freshness, confidence) into state
    4. Proceeds to reasoning

    Attributes:
        NODE_NAME: Unique identifier for this node type.
    """

    NODE_NAME: ClassVar[str] = "topology_lookup"

    async def run(
        self,
        state: ReactAgentState,
        deps: Any,
        emitter: EventEmitter,
    ) -> NodeResult:
        """Look up topology context and proceed to reasoning.

        Args:
            state: Current agent state with user goal.
            deps: Agent dependencies (services, config, etc.).
            emitter: SSE event emitter for streaming updates.

        Returns:
            NodeResult pointing to reason node.
        """
        await emitter.node_enter(self.NODE_NAME)

        try:
            # Extract potential entity names from user message
            entity_mentions = extract_entity_mentions(state.user_goal)

            if entity_mentions:
                logger.debug(f"Found potential entities: {entity_mentions}")

                # Look up topology for these entities
                topology_context = await self._lookup_topology(entity_mentions, deps)

                if topology_context:
                    # Inject context into deps for ReasonNode to use
                    if hasattr(deps, "topology_context"):
                        deps.topology_context = topology_context
                    else:
                        # Store in a way the ReasonNode can access
                        deps.topology_context = topology_context

                    logger.info(f"Injected topology context for {len(entity_mentions)} entities")

        except Exception as e:
            # Non-fatal - log and continue to reasoning
            logger.warning(f"Topology lookup error (non-fatal): {e}")

        await emitter.node_exit(self.NODE_NAME, next_node="reason")
        return NodeResult(next_node="reason")

    async def _lookup_topology(self, entity_mentions: list[str], deps: Any) -> str:
        """Look up known topology for entities using centralized context service.

        Delegates to TopologyContextService.build_context() for lookup and
        format_topology_context_for_prompt() for formatting, producing rich
        neighbor chain context with freshness and confidence markers.

        Args:
            entity_mentions: List of entity names to look up.
            deps: Agent dependencies with topology service.

        Returns:
            Formatted topology context string or empty string.
        """
        if not hasattr(deps, "external_deps") or not deps.external_deps:
            return ""

        try:
            from meho_app.api.database import create_openapi_session_maker
            from meho_app.modules.topology.context_node import (
                TopologyContextService,
                format_topology_context_for_prompt,
            )
            from meho_app.modules.topology.service import TopologyService

            tenant_id = (
                deps.external_deps.user_context.tenant_id
                if hasattr(deps.external_deps, "user_context")
                else ""
            )
            if not tenant_id:
                return ""

            session_maker = create_openapi_session_maker()
            async with session_maker() as db:
                context_service = TopologyContextService(db, TopologyService(db))
                contexts = await context_service.build_context(
                    user_message=" ".join(entity_mentions),
                    tenant_id=tenant_id,
                    max_entities=5,
                    traverse_depth=3,
                )
                return format_topology_context_for_prompt(contexts)

        except Exception as e:
            logger.debug(f"Topology lookup failed: {e}")

        return ""

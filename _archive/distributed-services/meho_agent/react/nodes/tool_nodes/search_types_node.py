"""SearchTypesNode - Generic tool node for type definition search (TASK-97)."""

from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING, Union
import logging

from pydantic_graph import BaseNode, GraphRunContext

if TYPE_CHECKING:
    from meho_agent.react.nodes.reason_node import ReasonNode
    from meho_agent.react.nodes.loop_detection_node import LoopDetectionNode

from meho_agent.react.graph_state import MEHOGraphState
from meho_agent.react.graph_deps import MEHOGraphDeps

logger = logging.getLogger(__name__)


@dataclass
class SearchTypesNode(BaseNode[MEHOGraphState, MEHOGraphDeps, None]):
    """
    Search for entity type definitions in ANY connector.
    
    TASK-97: Generic tool that routes to appropriate type definitions
    based on connector_type.
    
    TASK-98: REST connectors now also have types extracted from OpenAPI
    components/schemas during spec ingestion.
    """
    
    connector_id: str
    query: str = ""
    limit: int = 10
    
    async def run(
        self,
        ctx: GraphRunContext[MEHOGraphState, MEHOGraphDeps]
    ) -> Union['ReasonNode', 'LoopDetectionNode']:
        """Execute search_types and check for loops."""
        from meho_agent.react.nodes.loop_detection_node import LoopDetectionNode
        from meho_agent.react.tool_handlers import search_types_handler
        
        state = ctx.state
        deps = ctx.deps
        
        logger.info(f"SearchTypesNode: query='{self.query}', connector={self.connector_id}")
        
        # Record action for loop detection
        state.record_action("search_types", {
            "connector_id": self.connector_id,
            "query": self.query,
        })
        
        # Call the generic handler (routes internally based on connector_type)
        result = await search_types_handler(deps, {
            "connector_id": self.connector_id,
            "query": self.query,
            "limit": self.limit,
        })
        
        # Truncate if needed
        if len(result) > 100000:
            result = result[:4000] + "\n... (truncated)"
        
        # Update state
        state.add_to_scratchpad(f"Observation: {result}")
        state.last_observation = result
        state.step_count += 1
        state.pending_tool = None
        state.pending_args = None
        
        # Go through loop detection before reasoning again
        return LoopDetectionNode()


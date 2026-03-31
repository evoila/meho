"""SearchOperationsNode - Generic tool node for operation search (TASK-97)."""

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
class SearchOperationsNode(BaseNode[MEHOGraphState, MEHOGraphDeps, None]):
    """
    Search for operations on ANY connector type.
    
    TASK-97: Generic tool that routes to REST endpoints, SOAP operations,
    or VMware operations based on connector_type.
    """
    
    connector_id: str
    query: str  # Required - no default!
    limit: int = 10
    
    async def run(
        self,
        ctx: GraphRunContext[MEHOGraphState, MEHOGraphDeps]
    ) -> Union['ReasonNode', 'LoopDetectionNode']:
        """Execute search_operations and check for loops."""
        from meho_agent.react.nodes.loop_detection_node import LoopDetectionNode
        from meho_agent.react.tool_handlers import search_operations_handler
        
        state = ctx.state
        deps = ctx.deps
        
        logger.info(f"SearchOperationsNode: query='{self.query}', connector={self.connector_id}")
        
        # Record action for loop detection
        state.record_action("search_operations", {
            "connector_id": self.connector_id,
            "query": self.query,
        })
        
        # Call the generic handler (routes internally based on connector_type)
        result = await search_operations_handler(deps, {
            "connector_id": self.connector_id,
            "query": self.query,
            "limit": self.limit,
        })
        
        # Truncate if needed (100k limit for consistency)
        if len(result) > 100000:
            result = result[:100000] + "\n... (truncated)"
        
        # Update state
        state.add_to_scratchpad(f"Observation: {result}")
        state.last_observation = result
        state.step_count += 1
        state.pending_tool = None
        state.pending_args = None
        
        # Go through loop detection before reasoning again
        return LoopDetectionNode()


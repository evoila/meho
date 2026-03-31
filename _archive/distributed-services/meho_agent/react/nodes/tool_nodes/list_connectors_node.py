"""ListConnectorsNode - Typed tool node for listing connectors."""

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
class ListConnectorsNode(BaseNode[MEHOGraphState, MEHOGraphDeps, None]):
    """
    List all available system connectors.
    
    No required inputs - this node just lists what's available.
    """
    
    async def run(
        self,
        ctx: GraphRunContext[MEHOGraphState, MEHOGraphDeps]
    ) -> Union['ReasonNode', 'LoopDetectionNode']:
        """Execute list_connectors and check for loops."""
        from meho_agent.react.nodes.loop_detection_node import LoopDetectionNode
        from meho_agent.react.tool_handlers import list_connectors_handler
        
        state = ctx.state
        deps = ctx.deps
        
        logger.info("ListConnectorsNode: listing connectors")
        
        # Record action for loop detection
        state.record_action("list_connectors", {})
        
        # Call the existing handler
        result = await list_connectors_handler(deps, {})
        
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


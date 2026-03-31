"""ReduceDataNode - Query cached data using SQL."""

from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Any, Union
import logging

from pydantic_graph import BaseNode, GraphRunContext

if TYPE_CHECKING:
    from meho_agent.react.nodes.reason_node import ReasonNode
    from meho_agent.react.nodes.loop_detection_node import LoopDetectionNode

from meho_agent.react.graph_state import MEHOGraphState
from meho_agent.react.graph_deps import MEHOGraphDeps

logger = logging.getLogger(__name__)


@dataclass
class ReduceDataNode(BaseNode[MEHOGraphState, MEHOGraphDeps, None]):
    """
    Query cached data using SQL.
    
    Example:
        sql="SELECT * FROM virtual_machines WHERE num_cpu > 8 ORDER BY memory_mb DESC"
    """
    
    sql: str
    
    async def run(
        self,
        ctx: GraphRunContext[MEHOGraphState, MEHOGraphDeps]
    ) -> Union['ReasonNode', 'LoopDetectionNode']:
        """Execute SQL query and check for loops."""
        from meho_agent.react.nodes.loop_detection_node import LoopDetectionNode
        from meho_agent.react.tool_handlers import reduce_data_handler
        
        state = ctx.state
        deps = ctx.deps
        
        logger.info(f"ReduceDataNode SQL: {self.sql[:80]}...")
        
        # Record action for loop detection
        state.record_action("reduce_data", {"sql": self.sql})
        
        # Call handler with SQL
        result = await reduce_data_handler(deps, {"sql": self.sql})
        
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


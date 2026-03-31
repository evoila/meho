"""SearchKnowledgeNode - Typed tool node for knowledge search."""

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
class SearchKnowledgeNode(BaseNode[MEHOGraphState, MEHOGraphDeps, None]):
    """
    Search the knowledge base for documentation.
    
    Fields are validated by Pydantic when node is instantiated.
    """
    
    query: str
    limit: int = 5
    
    async def run(
        self,
        ctx: GraphRunContext[MEHOGraphState, MEHOGraphDeps]
    ) -> Union['ReasonNode', 'LoopDetectionNode']:
        """Execute search_knowledge and check for loops."""
        from meho_agent.react.nodes.loop_detection_node import LoopDetectionNode
        from meho_agent.react.tool_handlers import search_knowledge_handler
        
        state = ctx.state
        deps = ctx.deps
        
        logger.info(f"SearchKnowledgeNode: query='{self.query}'")
        
        # Record action for loop detection
        state.record_action("search_knowledge", {"query": self.query})
        
        # Call the existing handler
        result = await search_knowledge_handler(deps, {
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


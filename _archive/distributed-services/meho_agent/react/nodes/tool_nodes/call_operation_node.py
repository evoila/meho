"""CallOperationNode - Generic tool node for operation execution (TASK-97)."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Any, List, Union
import logging

from pydantic_graph import BaseNode, GraphRunContext

if TYPE_CHECKING:
    from meho_agent.react.nodes.reason_node import ReasonNode
    from meho_agent.react.nodes.approval_check_node import ApprovalCheckNode
    from meho_agent.react.nodes.loop_detection_node import LoopDetectionNode

from meho_agent.react.graph_state import MEHOGraphState
from meho_agent.react.graph_deps import MEHOGraphDeps

logger = logging.getLogger(__name__)


@dataclass
class CallOperationNode(BaseNode[MEHOGraphState, MEHOGraphDeps, None]):
    """
    Execute an operation on ANY connector type.
    
    TASK-97: Generic tool that routes to REST, SOAP, or VMware
    based on connector_type.
    
    Uses parameter_sets for ALL calls - always a list.
    Single calls have one item, batch calls have multiple items.
    """
    
    connector_id: str
    operation_id: str
    parameter_sets: List[Dict[str, Any]] = field(default_factory=lambda: [{}])
    
    async def run(
        self,
        ctx: GraphRunContext[MEHOGraphState, MEHOGraphDeps]
    ) -> Union['ReasonNode', 'ApprovalCheckNode', 'LoopDetectionNode']:
        """Execute call_operation, potentially routing through approval or loop detection."""
        from meho_agent.react.nodes.reason_node import ReasonNode
        from meho_agent.react.nodes.approval_check_node import ApprovalCheckNode
        from meho_agent.react.nodes.loop_detection_node import LoopDetectionNode
        from meho_agent.react.tool_handlers import call_operation_handler
        
        state = ctx.state
        deps = ctx.deps
        
        logger.info(
            f"CallOperationNode: operation={self.operation_id}, "
            f"connector={self.connector_id}, "
            f"parameter_sets={len(self.parameter_sets)} sets"
        )
        
        # Record action for loop detection
        state.record_action("call_operation", {
            "connector_id": self.connector_id,
            "operation_id": self.operation_id,
            "parameter_sets": self.parameter_sets,
        })
        
        # Build args dict with parameter_sets
        args = {
            "connector_id": self.connector_id,
            "operation_id": self.operation_id,
            "parameter_sets": self.parameter_sets,
        }
        
        # Call the generic handler (routes internally based on connector_type)
        result = await call_operation_handler(deps, args, state=state)
        
        # Truncate if needed (100k limit for detailed responses like performance metrics)
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


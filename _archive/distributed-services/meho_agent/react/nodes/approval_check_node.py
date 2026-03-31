from __future__ import annotations
"""
ApprovalCheckNode - Approval Gate (TASK-89 + TASK-76 + TASK-92)

This node checks if the pending tool call requires user approval.
For dangerous operations (POST, PUT, DELETE), it:
1. Checks if approval already exists
2. If not, creates a pending approval and halts execution
3. If approved, proceeds to CallOperationNode (TASK-92: typed node)
4. If rejected, returns to ReasonNode with rejection info
"""

import json
import hashlib
import logging
from typing import Union, Tuple, TYPE_CHECKING
from dataclasses import dataclass
from uuid import UUID

from pydantic_graph import BaseNode, End, GraphRunContext

if TYPE_CHECKING:
    from meho_agent.react.nodes.reason_node import ReasonNode

from meho_agent.react.graph_state import MEHOGraphState
from meho_agent.react.graph_deps import MEHOGraphDeps

# Import typed tool node (TASK-92, TASK-97)
from meho_agent.react.nodes.tool_nodes.call_operation_node import CallOperationNode

# Danger level assignment functions
def assign_danger_level(method: str, path: str) -> Tuple[str, bool]:
    """Assign danger level based on HTTP method."""
    try:
        from meho_agent.approval.danger_level import assign_danger_level as _impl
        return _impl(method, path)
    except ImportError:
        # Fallback implementation
        method = method.upper()
        if method in ("GET", "HEAD", "OPTIONS"):
            return ("safe", False)
        if method == "DELETE":
            return ("critical", True)
        return ("dangerous", True)


def get_impact_message(level: str, method: str, path: str) -> str:
    """Get impact message for danger level."""
    try:
        from meho_agent.approval.danger_level import get_impact_message as _impl
        return _impl(method, path)
    except ImportError:
        if level == "critical":
            return f"This {method} operation is irreversible."
        return f"This {method} operation may modify data."

logger = logging.getLogger(__name__)


@dataclass
class ApprovalCheckNode(BaseNode[MEHOGraphState, MEHOGraphDeps, None]):
    """
    Approval check gate for dangerous operations.
    
    This node integrates with TASK-76 approval flow:
    1. Fetch operation details from pending args
    2. Determine danger level from HTTP method (for REST) or operation type
    3. Check if approval already granted
    4. If not, create pending approval and halt
    5. Resume when user approves/rejects
    
    TASK-92/97: Now returns typed CallOperationNode.
    
    Transitions:
    - If safe operation → CallOperationNode
    - If approved → CallOperationNode  
    - If pending approval → End (with approval_required state)
    - If rejected → ReasonNode (with rejection info)
    """
    
    async def run(
        self,
        ctx: GraphRunContext[MEHOGraphState, MEHOGraphDeps]
    ) -> Union[CallOperationNode, 'ReasonNode', End[None]]:
        """Check if pending tool call needs approval."""
        state = ctx.state
        deps = ctx.deps
        
        logger.info(f"ApprovalCheckNode: Checking {state.pending_tool}")
        
        # For non-call_operation tools, this node shouldn't be reached
        # (TASK-92: typed nodes handle routing directly)
        if state.pending_tool != "call_operation":
            logger.warning(f"ApprovalCheckNode called for non-call_operation: {state.pending_tool}")
            # Return to reason node
            from meho_agent.react.nodes.reason_node import ReasonNode
            state.add_to_scratchpad("Observation: Internal routing error, retrying.")
            return ReasonNode()
        
        # Extract operation details from pending args
        operation_id = state.pending_args.get("operation_id") if state.pending_args else None
        connector_id = state.pending_args.get("connector_id") if state.pending_args else None
        
        if not operation_id or not connector_id:
            # No operation specified, can't check danger level
            # Build CallOperationNode from pending args (if any)
            return CallOperationNode(
                connector_id=state.pending_args.get("connector_id", "") if state.pending_args else "",
                operation_id=state.pending_args.get("operation_id", "") if state.pending_args else "",
                parameter_sets=state.pending_args.get("parameter_sets", [{}]) if state.pending_args else [{}],
            )
        
        # For REST connectors, fetch endpoint to get HTTP method
        # For other connector types, we may need different logic
        method = "GET"
        path = ""
        try:
            if deps.meho_deps and hasattr(deps.meho_deps, 'endpoint_repo'):
                endpoint = await deps.meho_deps.endpoint_repo.get_endpoint(operation_id)
                if endpoint:
                    method = getattr(endpoint, 'method', 'GET').upper()
                    path = getattr(endpoint, 'path', '')
        except Exception as e:
            logger.warning(f"Could not fetch endpoint details: {e}")
        
        # Determine danger level
        danger_level, requires_approval = assign_danger_level(method, path)
        
        logger.info(f"Operation {method} {path}: danger_level={danger_level}, requires_approval={requires_approval}")
        
        # Safe operations don't need approval
        if not requires_approval:
            return CallOperationNode(
                connector_id=connector_id,
                operation_id=operation_id,
                parameter_sets=state.pending_args.get("parameter_sets", [{}]) if state.pending_args else [{}],
            )
        
        # Check if we already have approval for this call
        if deps.approval_store:
            # Convert session_id to UUID if it's a string
            session_uuid: UUID
            if isinstance(state.session_id, str):
                session_uuid = UUID(state.session_id)
            else:
                session_uuid = state.session_id  # type: ignore[assignment]
            
            # check_approval returns ONLY approved records (filtered in WHERE clause)
            # So if it returns something, it means the action was approved
            existing_approval = await deps.approval_store.check_approval(
                session_id=session_uuid,
                tool_name=state.pending_tool or "call_operation",
                tool_args=state.pending_args or {},
            )
            
            if existing_approval:
                # The check_approval method only returns APPROVED records
                # so if we get a result, it's approved
                logger.info("Found existing approval, proceeding")
                state.approval_granted = True
                return CallOperationNode(
                    connector_id=connector_id,
                    operation_id=operation_id,
                    parameter_sets=state.pending_args.get("parameter_sets", [{}]) if state.pending_args else [{}],
                )
            
            # No existing approval - create pending approval
            logger.info("Creating pending approval request")
            
            impact_message = get_impact_message(danger_level, method, path)
            
            approval_request = await deps.approval_store.create_pending(
                session_id=session_uuid,
                tenant_id=deps.tenant_id or "",
                user_id=deps.user_id or "",
                tool_name=state.pending_tool or "call_operation",
                tool_args=state.pending_args or {},
                danger_level=danger_level,
                user_message=state.user_goal,
                conversation_history=[],  # Could include scratchpad
                http_method=method,
                endpoint_path=path,
                description=f"{method} {path}" if path else f"Operation {operation_id}",
                impact_message=impact_message,
            )
            
            state.pending_approval_id = str(approval_request.id)
            
            # Emit approval_required event
            await deps.emit_progress("approval_required", {
                "approval_id": str(approval_request.id),
                "tool": state.pending_tool,
                "danger_level": danger_level,
                "method": method,
                "path": path,
                "description": f"{method} {path}" if path else f"Operation {operation_id}",
                "impact": impact_message,
            })
            
            # End execution - will resume when user approves
            # The state is persisted so we can resume later
            return End(None)
        
        # No approval store configured - log warning and proceed
        logger.warning("No approval store configured, proceeding without approval")
        return CallOperationNode(
            connector_id=connector_id,
            operation_id=operation_id,
            parameter_sets=state.pending_args.get("parameter_sets", [{}]) if state.pending_args else [{}],
        )

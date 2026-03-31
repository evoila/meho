"""
Step Classifier - Classifies plan steps into discovery, action, and interpretation.

This enables progressive approval UX where users only approve API calls,
not internal discovery steps like searching APIs or listing connectors.
"""
from typing import Dict, List
from meho_agent.schemas import Plan, PlanStep
import logging

logger = logging.getLogger(__name__)


# Tool categories (Updated for generic tools TASK-97)
DISCOVERY_TOOLS = {
    "search_knowledge",      # Search knowledge base
    "list_connectors",       # List available systems
    "search_operations",     # Search operations (generic - works for all connector types)
    "search_types",          # Search type definitions
}
ACTION_TOOLS = {"call_operation"}  # Requires user approval (handles single and batch via parameter_sets)
INTERPRET_TOOLS = {"interpret_results", "reduce_data"}  # Auto-execute after action


def classify_steps(plan: Plan) -> Dict[str, List[PlanStep]]:
    """
    Classify plan steps into discovery, action, and interpretation categories.
    
    This enables progressive approval where:
    - Discovery steps run automatically (with status updates)
    - Action steps require user approval
    - Interpretation runs automatically after action
    
    Args:
        plan: Execution plan to classify
        
    Returns:
        Dictionary with keys: 'discovery', 'action', 'interpret'
        Each containing a list of PlanStep objects
    
    Example:
        >>> plan = Plan(goal="get hosts", steps=[...])
        >>> categories = classify_steps(plan)
        >>> categories['discovery']  # [search_apis, list_connectors, get_endpoint_details]
        >>> categories['action']     # [call_endpoint]
        >>> categories['interpret']  # [interpret_results]
    """
    discovery_steps: List[PlanStep] = []
    action_steps: List[PlanStep] = []
    interpret_steps: List[PlanStep] = []
    
    for step in plan.steps:
        if step.tool_name in DISCOVERY_TOOLS:
            discovery_steps.append(step)
        elif step.tool_name in ACTION_TOOLS:
            action_steps.append(step)
        elif step.tool_name in INTERPRET_TOOLS:
            interpret_steps.append(step)
        else:
            # Unknown tool - treat as discovery (safe default)
            logger.warning(f"Unknown tool '{step.tool_name}' - treating as discovery step")
            discovery_steps.append(step)
    
    logger.info(f"Step classification: {len(discovery_steps)} discovery, {len(action_steps)} action, {len(interpret_steps)} interpret")
    
    return {
        "discovery": discovery_steps,
        "action": action_steps,
        "interpret": interpret_steps
    }


def is_discovery_tool(tool_name: str) -> bool:
    """Check if a tool is a discovery tool (auto-execute)"""
    return tool_name in DISCOVERY_TOOLS


def is_action_tool(tool_name: str) -> bool:
    """Check if a tool is an action tool (requires approval)"""
    return tool_name in ACTION_TOOLS


def is_interpret_tool(tool_name: str) -> bool:
    """Check if a tool is an interpretation tool (auto-execute after action)"""
    return tool_name in INTERPRET_TOOLS


"""
Risk classification for agent plans.

Determines which plans require user approval before execution.
Safe operations (read-only) can auto-execute.
Risky operations (API calls, modifications) require approval.
"""
from meho_agent.schemas import Plan, PlanStep
from typing import List


# Safe tools (read-only, no side effects)
SAFE_TOOLS = {
    "search_knowledge",      # Knowledge base search
    "list_connectors",       # Connector discovery
    "get_endpoint_details",  # Endpoint metadata
    "interpret_results",     # LLM result interpretation
}

# Risky tools (can modify state, make external calls)
RISKY_TOOLS = {
    "call_endpoint",        # API calls (can be read or write)
    # Add more risky tools as they're added
}


def classify_plan_risk(plan: Plan) -> bool:
    """
    Classify if a plan requires user approval.
    
    Returns:
        True if plan requires approval (has risky operations)
        False if plan is safe (read-only operations only)
    """
    if not plan.steps:
        return False  # Empty plan is safe
    
    # Check if any step uses a risky tool
    for step in plan.steps:
        if step.tool_name in RISKY_TOOLS:
            return True  # Requires approval
    
    # All tools are safe
    return False


def get_risky_steps(plan: Plan) -> List[PlanStep]:
    """Get list of steps that use risky tools"""
    return [step for step in plan.steps if step.tool_name in RISKY_TOOLS]


def get_safe_steps(plan: Plan) -> List[PlanStep]:
    """Get list of steps that use safe tools"""
    return [step for step in plan.steps if step.tool_name in SAFE_TOOLS]


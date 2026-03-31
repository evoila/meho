"""
MEHO Agent Service - PydanticAI-based workflow orchestration.

Exports:
    - Models: AgentPlanModel, AgentPlanStepModel (ephemeral chat execution plans)
    - Schemas: Plan, PlanStep, AgentPlan, AgentPlanCreate
    - Repository: AgentPlanRepository
    - Dependencies: MEHODependencies
    - Agents: MEHOReActGraph (recommended)

Note: HTTP routes still use "/workflows/*" for backwards compatibility,
but internally we call them AgentPlans to distinguish from WorkflowDefinitions.

TASK-89: MEHOReActGraph is the recommended agent architecture.
"""
from meho_agent.models import Base, AgentPlanModel, AgentPlanStepModel, PlanStatus, StepStatus
from meho_agent.schemas import Plan, PlanStep, AgentPlan, AgentPlanCreate, WorkflowStep
from meho_agent.repository import AgentPlanRepository
from meho_agent.dependencies import MEHODependencies

# ReAct Graph Architecture (TASK-89)
from meho_agent.react import MEHOReActGraph, MEHOGraphState, MEHOGraphDeps, GraphEvent

# Backward compatibility aliases (deprecated - use AgentPlan* names instead)
WorkflowModel = AgentPlanModel
WorkflowStepModel = AgentPlanStepModel
WorkflowStatus = PlanStatus
Workflow = AgentPlan
WorkflowCreate = AgentPlanCreate
WorkflowRepository = AgentPlanRepository

__all__ = [
    "Base",
    # New clear names
    "AgentPlanModel",
    "AgentPlanStepModel",
    "PlanStatus",
    "StepStatus",
    "Plan",
    "PlanStep",
    "AgentPlan",
    "AgentPlanCreate",
    "WorkflowStep",
    "AgentPlanRepository",
    "MEHODependencies",
    # ReAct Graph (TASK-89) - Recommended
    "MEHOReActGraph",
    "MEHOGraphState",
    "MEHOGraphDeps",
    "GraphEvent",
    # Backward compatibility (deprecated)
    "WorkflowModel",
    "WorkflowStepModel",
    "WorkflowStatus",
    "Workflow",
    "WorkflowCreate",
    "WorkflowRepository",
]


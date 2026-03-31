"""
Repository for agent database operations.

Note: WorkflowDefinitionRepository removed in Session 80.
Replaced by Recipe system (meho_agent/recipes/).
"""
# mypy: disable-error-code="arg-type,assignment"
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from meho_agent.models import (
    AgentPlanModel,
    AgentPlanStepModel,
    PlanStatus,
    StepStatus,
)
from meho_agent.schemas import (
    AgentPlanCreate,
    AgentPlan,
    Plan,
)
from typing import Optional, Dict
from datetime import datetime
import uuid
import asyncio
from collections import defaultdict


class AgentPlanRepository:
    """Repository for agent plan operations"""
    
    # Class-level lock dictionary for concurrent execution protection
    _execution_locks: defaultdict = defaultdict(asyncio.Lock)
    # Track lock ownership to prevent incorrect releases
    _lock_owners: Dict[str, int] = {}
    
    def __init__(self, session: AsyncSession):
        self.session = session
        self._instance_id = id(self)  # Unique ID for this repository instance
    
    async def create_workflow(self, plan_create: AgentPlanCreate) -> AgentPlan:
        """Create a new agent plan (ephemeral chat execution)"""
        db_plan = AgentPlanModel(
            id=uuid.uuid4(),
            tenant_id=plan_create.tenant_id,
            user_id=plan_create.user_id,
            goal=plan_create.goal,
            status=PlanStatus.PLANNING,
            plan_json=plan_create.plan.model_dump() if plan_create.plan else None,
            current_step_index=0
        )
        
        self.session.add(db_plan)
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
        await self.session.refresh(db_plan)
        
        return AgentPlan(
            id=str(db_plan.id),
            tenant_id=db_plan.tenant_id,
            user_id=db_plan.user_id,
            status=db_plan.status.value,
            goal=db_plan.goal,
            plan_json=db_plan.plan_json,
            current_step_index=db_plan.current_step_index,
            created_at=db_plan.created_at,
            updated_at=db_plan.updated_at
        )
    
    async def get_workflow(self, workflow_id: str) -> Optional[AgentPlan]:
        """Get agent plan by ID"""
        try:
            query = select(AgentPlanModel).where(AgentPlanModel.id == uuid.UUID(workflow_id))
            result = await self.session.execute(query)
            db_plan = result.scalar_one_or_none()
            
            if not db_plan:
                return None
            
            return AgentPlan(
                id=str(db_plan.id),
                tenant_id=db_plan.tenant_id,
                user_id=db_plan.user_id,
                status=db_plan.status.value,
                goal=db_plan.goal,
                plan_json=db_plan.plan_json,
                current_step_index=db_plan.current_step_index,
                session_id=str(db_plan.session_id) if db_plan.session_id else None,
                created_at=db_plan.created_at,
                updated_at=db_plan.updated_at
            )
        except ValueError:
            return None
    
    async def update_status(self, workflow_id: str, status: PlanStatus) -> bool:
        """Update workflow status"""
        try:
            query = select(AgentPlanModel).where(AgentPlanModel.id == uuid.UUID(workflow_id))
            result = await self.session.execute(query)
            db_plan = result.scalar_one_or_none()
            
            if not db_plan:
                return False
            
            db_plan.status = status
            await self.session.flush()  # Flush changes, don't commit (session managed externally)
            return True
        except ValueError:
            return False
    
    async def acquire_execution_lock(self, workflow_id: str) -> bool:
        """
        Acquire exclusive lock for workflow execution (non-blocking).
        
        Prevents concurrent execution of the same workflow.
        Tracks ownership to prevent incorrect releases.
        
        Args:
            workflow_id: Workflow identifier
        
        Returns:
            True if lock acquired, False if already locked
        """
        lock = self._execution_locks[workflow_id]
        
        # Non-blocking acquisition using very short timeout
        # asyncio.Lock doesn't have try_acquire(), so we use timeout
        try:
            await asyncio.wait_for(lock.acquire(), timeout=0.001)
            # Record ownership
            self._lock_owners[workflow_id] = self._instance_id
            return True
        except asyncio.TimeoutError:
            # Lock is already held (couldn't acquire within 1ms)
            return False
    
    def release_execution_lock(self, workflow_id: str) -> None:
        """
        Release execution lock for workflow.
        
        Only releases if this instance acquired the lock.
        Prevents incorrect release from workers that failed to acquire.
        
        Args:
            workflow_id: Workflow identifier
        """
        # SECURITY: Only release if we own the lock
        if self._lock_owners.get(workflow_id) != self._instance_id:
            # We don't own this lock - don't release it!
            return
        
        lock = self._execution_locks[workflow_id]
        if lock.locked():
            lock.release()
            # Clear ownership
            self._lock_owners.pop(workflow_id, None)


# =============================================================================
# DEPRECATED: WorkflowDefinitionRepository removed in Session 80
# Replaced by Recipe system (meho_agent/recipes/)
# =============================================================================


# Backward compatibility alias
WorkflowRepository = AgentPlanRepository

"""
FastAPI routes for Agent HTTP Service.

Full HTTP implementation for BFF refactoring (Task 36).
"""
# mypy: disable-error-code="no-untyped-def,arg-type,assignment,union-attr,misc"
from fastapi import APIRouter, HTTPException, Depends, Query
from meho_agent.api_schemas import (
    CreateWorkflowRequest,
    AgentPlanResponse,
    ExecutionResponse,
    HealthResponse,
    CreateChatSessionRequest,
    UpdateChatSessionRequest,
    ChatSessionResponse,
    ChatMessageResponse,
    ChatSessionWithMessagesResponse,
    AddMessageRequest,
    CreateTemplateRequest,
    UpdateTemplateRequest,
    TemplateResponse,
    ExecuteTemplateRequest
)
from meho_agent.repository import AgentPlanRepository
# Note: Use MEHOReActGraph for new implementations (see meho_agent.react)
from meho_agent.schemas import Plan, AgentPlanCreate
from meho_agent.models import PlanStatus, ChatSessionModel, ChatMessageModel
from meho_agent.risk_classification import classify_plan_risk
from meho_agent.dependencies import MEHODependencies
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from typing import List, Optional
from datetime import datetime
from uuid import UUID, uuid4
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])


# ============================================================================
# Dependency Injection
# ============================================================================

async def get_db_session() -> AsyncSession:
    """Get database session for agent service"""
    from meho_agent.database import get_session
    async for session in get_session():
        yield session


async def get_agent_plan_repository(session: AsyncSession = Depends(get_db_session)) -> AgentPlanRepository:
    """Get workflow repository"""
    return AgentPlanRepository(session)


# ============================================================================
# Routes
# ============================================================================

@router.post("/workflows", response_model=AgentPlanResponse, status_code=201)
async def create_workflow(
    request: CreateWorkflowRequest,
    repo: AgentPlanRepository = Depends(get_agent_plan_repository)
):
    """
    Create a workflow and generate execution plan.
    
    Note: This endpoint creates the workflow record but does NOT generate the plan.
    Plan generation is handled automatically by the chat flow using MEHOReActGraph.
    
    This endpoint is primarily used for:
    1. Creating workflow records for tracking
    2. Storing execution metadata
    3. Associating chat sessions with execution history
    """
    try:
        plan_create = AgentPlanCreate(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            goal=request.goal,
            plan=None  # Plan will be added later
        )
        
        agent_plan = await repo.create_workflow(plan_create)
        
        return AgentPlanResponse(
            id=str(agent_plan.id),
            tenant_id=agent_plan.tenant_id,
            user_id=agent_plan.user_id,
            goal=agent_plan.goal,
            status=agent_plan.status if agent_plan.status else "pending",
            plan=agent_plan.plan_json,
            session_id=str(agent_plan.session_id) if agent_plan.session_id else None,
            created_at=agent_plan.created_at,
            updated_at=agent_plan.updated_at
        )
    except Exception as e:
        logger.error(f"Failed to create workflow: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create workflow: {str(e)}")


@router.get("/workflows/{workflow_id}", response_model=AgentPlanResponse)
async def get_workflow(
    workflow_id: str,
    repo: AgentPlanRepository = Depends(get_agent_plan_repository)
):
    """Get agent plan by ID"""
    try:
        agent_plan = await repo.get_workflow(workflow_id)
        
        if not agent_plan:
            raise HTTPException(status_code=404, detail="Workflow not found")
        
        return AgentPlanResponse(
            id=str(agent_plan.id),
            tenant_id=agent_plan.tenant_id,
            user_id=agent_plan.user_id,
            goal=agent_plan.goal,
            status=agent_plan.status if agent_plan.status else "pending",
            plan=agent_plan.plan_json,
            session_id=str(agent_plan.session_id) if agent_plan.session_id else None,
            created_at=agent_plan.created_at,
            updated_at=agent_plan.updated_at
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get agent plan: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get agent plan: {str(e)}")


@router.patch("/workflows/{workflow_id}", response_model=AgentPlanResponse)
async def update_workflow(
    workflow_id: str,
    updates: dict,
    repo: AgentPlanRepository = Depends(get_agent_plan_repository)
):
    """
    Update workflow (e.g., add plan, update status).
    
    Accepts any workflow fields to update.
    """
    try:
        workflow = await repo.get_workflow(workflow_id)
        if not workflow:
            raise HTTPException(status_code=404, detail="Workflow not found")
        
        # Update workflow
        from sqlalchemy import update
        from meho_agent.models import AgentPlanModel
        import uuid
        
        stmt = update(AgentPlanModel).where(
            AgentPlanModel.id == uuid.UUID(workflow_id)
        ).values(**updates)
        
        session = repo.session
        await session.execute(stmt)
        await session.commit()
        
        # Refresh and return
        updated_workflow = await repo.get_workflow(workflow_id)
        
        return AgentPlanResponse(
            id=str(updated_workflow.id),
            tenant_id=updated_workflow.tenant_id,
            user_id=updated_workflow.user_id,
            goal=updated_workflow.goal,
            status=updated_workflow.status if updated_workflow.status else "pending",
            plan=updated_workflow.plan_json,
            session_id=str(updated_workflow.session_id) if updated_workflow.session_id else None,
            created_at=updated_workflow.created_at,
            updated_at=updated_workflow.updated_at
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update workflow: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update workflow: {str(e)}")


@router.post("/workflows/{workflow_id}/execute", response_model=ExecutionResponse)
async def execute_workflow(workflow_id: str):
    """
    Execute a workflow (run the plan).
    
    Note: This endpoint requires MEHODependencies which should be provided by the caller.
    This is a placeholder - actual execution happens in the BFF with full context.
    
    For now, returns 501 - execution should be done in BFF with proper dependencies.
    """
    raise HTTPException(
        status_code=501,
        detail="Execution requires MEHODependencies. Execute in BFF with full user context."
    )


@router.get("/workflows", response_model=List[AgentPlanResponse])
async def list_workflows(
    tenant_id: str = Query(...),
    user_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(default=50, le=200),
    repo: AgentPlanRepository = Depends(get_agent_plan_repository)
):
    """List workflows for a tenant/user with optional filters"""
    try:
        from sqlalchemy import select, desc
        from meho_agent.models import AgentPlanModel
        
        # Build query
        query = select(AgentPlanModel).where(
            AgentPlanModel.tenant_id == tenant_id
        )
        
        # Filter by user if provided
        if user_id:
            query = query.where(AgentPlanModel.user_id == user_id)
        
        # Filter by status if provided
        if status:
            try:
                status_enum = PlanStatus(status)
                query = query.where(AgentPlanModel.status == status_enum)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
        
        # Order by created_at descending (newest first)
        query = query.order_by(desc(AgentPlanModel.created_at))
        
        # Limit results
        query = query.limit(limit)
        
        # Execute query
        session = repo.session
        result = await session.execute(query)
        workflows = result.scalars().all()
        
        # Convert to response format
        return [
            AgentPlanResponse(
                id=str(wf.id),
                tenant_id=wf.tenant_id,
                user_id=wf.user_id,
                goal=wf.goal,
                status=wf.status if wf.status else "pending",
                plan=wf.plan_json,
                created_at=wf.created_at,
                updated_at=wf.updated_at
            )
            for wf in workflows
        ]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list workflows: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list workflows: {str(e)}")


@router.patch("/workflows/{workflow_id}/status")
async def update_workflow_status(
    workflow_id: str,
    status: str,
    repo: AgentPlanRepository = Depends(get_agent_plan_repository)
):
    """Update workflow status"""
    try:
        workflow = await repo.get_workflow(workflow_id)
        if not workflow:
            raise HTTPException(status_code=404, detail="Workflow not found")
        
        # Convert string to PlanStatus enum
        try:
            status_enum = PlanStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
        
        await repo.update_status(workflow_id, status_enum)
        
        return {"message": "Status updated", "workflow_id": workflow_id, "status": status}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update status: {str(e)}")


@router.post("/workflows/{workflow_id}/cancel")
async def cancel_workflow(
    workflow_id: str,
    repo: AgentPlanRepository = Depends(get_agent_plan_repository)
):
    """Cancel a workflow"""
    try:
        workflow = await repo.get_workflow(workflow_id)
        if not workflow:
            raise HTTPException(status_code=404, detail="Workflow not found")
        
        await repo.update_status(workflow_id, PlanStatus.CANCELLED)
        
        return {
            "id": workflow_id,
            "status": "CANCELLED",
            "message": "Workflow cancelled"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cancel workflow: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to cancel workflow: {str(e)}")


@router.put("/workflows/{workflow_id}/plan")
async def update_workflow_plan(
    workflow_id: str,
    plan: dict,
    repo: AgentPlanRepository = Depends(get_agent_plan_repository)
):
    """Update workflow plan"""
    try:
        workflow = await repo.get_workflow(workflow_id)
        if not workflow:
            raise HTTPException(status_code=404, detail="Workflow not found")
        
        # Update workflow with new plan
        from sqlalchemy import update
        from meho_agent.models import AgentPlanModel
        import uuid
        
        stmt = update(AgentPlanModel).where(
            AgentPlanModel.id == uuid.UUID(workflow_id)
        ).values(plan_json=plan)
        
        session = repo.session
        await session.execute(stmt)
        await session.commit()
        
        # Refresh and return
        updated_workflow = await repo.get_workflow(workflow_id)
        
        return AgentPlanResponse(
            id=str(updated_workflow.id),
            tenant_id=updated_workflow.tenant_id,
            user_id=updated_workflow.user_id,
            goal=updated_workflow.goal,
            status=updated_workflow.status if updated_workflow.status else "pending",
            plan=updated_workflow.plan_json,
            session_id=str(updated_workflow.session_id) if updated_workflow.session_id else None,
            created_at=updated_workflow.created_at,
            updated_at=updated_workflow.updated_at
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update plan: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update plan: {str(e)}")


@router.post("/workflows/{workflow_id}/clone", response_model=AgentPlanResponse)
async def clone_workflow(
    workflow_id: str,
    modified_plan: Optional[dict] = None,
    repo: AgentPlanRepository = Depends(get_agent_plan_repository)
):
    """Clone an existing workflow"""
    try:
        # Get original workflow
        original_workflow = await repo.get_workflow(workflow_id)
        if not original_workflow:
            raise HTTPException(status_code=404, detail="Workflow not found")
        
        # Create new agent plan
        plan_create = AgentPlanCreate(
            tenant_id=original_workflow.tenant_id,
            user_id=original_workflow.user_id,
            goal=original_workflow.goal,
            plan=None
        )
        new_plan = await repo.create_workflow(plan_create)
        
        # Use modified plan if provided, otherwise use original
        plan_to_use = modified_plan if modified_plan else original_workflow.plan_json
        
        if plan_to_use:
            # Update new plan with plan JSON
            from sqlalchemy import update
            from meho_agent.models import AgentPlanModel
            import uuid
            
            stmt = update(AgentPlanModel).where(
                AgentPlanModel.id == uuid.UUID(new_plan.id)
            ).values(plan_json=plan_to_use)
            
            session = repo.session
            await session.execute(stmt)
            await session.commit()
        
        # Refresh and return
        cloned_plan = await repo.get_workflow(new_plan.id)
        
        return AgentPlanResponse(
            id=str(cloned_plan.id),
            tenant_id=cloned_plan.tenant_id,
            user_id=cloned_plan.user_id,
            goal=cloned_plan.goal,
            status=cloned_plan.status if cloned_plan.status else "pending",
            plan=cloned_plan.plan_json,
            session_id=str(cloned_plan.session_id) if cloned_plan.session_id else None,
            created_at=cloned_plan.created_at,
            updated_at=cloned_plan.updated_at
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to clone workflow: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to clone workflow: {str(e)}")


# ============================================================================
# Chat Session Routes
# ============================================================================

@router.post("/chat/sessions", response_model=ChatSessionResponse, status_code=201)
async def create_chat_session(
    request: CreateChatSessionRequest,
    session: AsyncSession = Depends(get_db_session)
):
    """Create a new chat session"""
    try:
        # Use client-provided ID if available, otherwise generate new one
        session_id = UUID(request.id) if request.id else uuid4()
        
        chat_session = ChatSessionModel(
            id=session_id,
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            title=request.title,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        
        session.add(chat_session)
        await session.commit()
        await session.refresh(chat_session)
        
        logger.info(f"Created chat session {chat_session.id}")
        
        return ChatSessionResponse(
            id=str(chat_session.id),
            tenant_id=chat_session.tenant_id,
            user_id=chat_session.user_id,
            title=chat_session.title,
            created_at=chat_session.created_at,
            updated_at=chat_session.updated_at,
            message_count=0
        )
    except Exception as e:
        logger.error(f"Failed to create chat session: {e}", exc_info=True)
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create chat session: {str(e)}")


@router.get("/chat/sessions", response_model=List[ChatSessionResponse])
async def list_chat_sessions(
    tenant_id: str = Query(...),
    user_id: str = Query(...),
    limit: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db_session)
):
    """List all chat sessions for a user"""
    try:
        # Get sessions with message counts
        stmt = (
            select(ChatSessionModel)
            .where(
                ChatSessionModel.tenant_id == tenant_id,
                ChatSessionModel.user_id == user_id
            )
            .order_by(desc(ChatSessionModel.updated_at))
            .limit(limit)
        )
        
        result = await db.execute(stmt)
        sessions = result.scalars().all()
        
        # Get message counts for each session
        session_responses = []
        for chat_session in sessions:
            # Count messages
            msg_stmt = select(func.count()).where(
                ChatMessageModel.session_id == chat_session.id
            )
            msg_result = await db.execute(msg_stmt)
            message_count = msg_result.scalar()
            
            session_responses.append(ChatSessionResponse(
                id=str(chat_session.id),
                tenant_id=chat_session.tenant_id,
                user_id=chat_session.user_id,
                title=chat_session.title,
                created_at=chat_session.created_at,
                updated_at=chat_session.updated_at,
                message_count=message_count
            ))
        
        logger.info(f"Listed {len(session_responses)} sessions for user {user_id}")
        return session_responses
    except Exception as e:
        logger.error(f"Failed to list chat sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list chat sessions: {str(e)}")


@router.get("/chat/sessions/{session_id}", response_model=ChatSessionWithMessagesResponse)
async def get_chat_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_db_session)
):
    """Get a specific chat session with all messages"""
    try:
        # Get session
        stmt = select(ChatSessionModel).where(ChatSessionModel.id == session_id)
        result = await db.execute(stmt)
        chat_session = result.scalar_one_or_none()
        
        if not chat_session:
            raise HTTPException(status_code=404, detail="Session not found")
        
        # Get messages
        msg_stmt = (
            select(ChatMessageModel)
            .where(ChatMessageModel.session_id == session_id)
            .order_by(ChatMessageModel.created_at)
        )
        msg_result = await db.execute(msg_stmt)
        messages = msg_result.scalars().all()
        
        logger.info(f"Retrieved session {session_id} with {len(messages)} messages")
        
        return ChatSessionWithMessagesResponse(
            id=str(chat_session.id),
            tenant_id=chat_session.tenant_id,
            user_id=chat_session.user_id,
            title=chat_session.title,
            created_at=chat_session.created_at,
            updated_at=chat_session.updated_at,
            messages=[
                ChatMessageResponse(
                    id=str(msg.id),
                    role=msg.role,
                    content=msg.content,
                    workflow_id=str(msg.agent_plan_id) if msg.agent_plan_id else (str(msg.workflow_id) if msg.workflow_id else None),
                    message_data=msg.message_data,  # Include full PydanticAI message (Session 69)
                    created_at=msg.created_at
                )
                for msg in messages
            ]
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get chat session: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get chat session: {str(e)}")


@router.patch("/chat/sessions/{session_id}", response_model=ChatSessionResponse)
async def update_chat_session(
    session_id: UUID,
    request: UpdateChatSessionRequest,
    db: AsyncSession = Depends(get_db_session)
):
    """Update session metadata (e.g., change title)"""
    try:
        # Get session
        stmt = select(ChatSessionModel).where(ChatSessionModel.id == session_id)
        result = await db.execute(stmt)
        chat_session = result.scalar_one_or_none()
        
        if not chat_session:
            raise HTTPException(status_code=404, detail="Session not found")
        
        # Update
        chat_session.title = request.title
        chat_session.updated_at = datetime.utcnow()
        
        await db.commit()
        await db.refresh(chat_session)
        
        logger.info(f"Updated session {session_id} title to '{request.title}'")
        
        return ChatSessionResponse(
            id=str(chat_session.id),
            tenant_id=chat_session.tenant_id,
            user_id=chat_session.user_id,
            title=chat_session.title,
            created_at=chat_session.created_at,
            updated_at=chat_session.updated_at
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update chat session: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update chat session: {str(e)}")


@router.delete("/chat/sessions/{session_id}", status_code=204)
async def delete_chat_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_db_session)
):
    """Delete a chat session and all its messages"""
    try:
        # Get session
        stmt = select(ChatSessionModel).where(ChatSessionModel.id == session_id)
        result = await db.execute(stmt)
        chat_session = result.scalar_one_or_none()
        
        if not chat_session:
            raise HTTPException(status_code=404, detail="Session not found")
        
        # Delete (cascade will delete messages)
        await db.delete(chat_session)
        await db.commit()
        
        logger.info(f"Deleted session {session_id}")
        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete chat session: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete chat session: {str(e)}")


@router.post("/chat/sessions/{session_id}/messages", response_model=ChatMessageResponse, status_code=201)
async def add_message(
    session_id: UUID,
    request: AddMessageRequest,
    db: AsyncSession = Depends(get_db_session)
):
    """Add a message to a chat session"""
    try:
        # Verify session exists
        stmt = select(ChatSessionModel).where(ChatSessionModel.id == session_id)
        result = await db.execute(stmt)
        chat_session = result.scalar_one_or_none()
        
        if not chat_session:
            raise HTTPException(status_code=404, detail="Session not found")
        
        # Create message
        message = ChatMessageModel(
            id=uuid4(),
            session_id=session_id,
            role=request.role,
            content=request.content,
            message_data=request.message_data,  # Store full PydanticAI message (Session 69)
            agent_plan_id=UUID(request.workflow_id) if request.workflow_id else None,
            created_at=datetime.utcnow()
        )
        
        db.add(message)
        
        # Update session updated_at
        chat_session.updated_at = datetime.utcnow()
        
        # Auto-generate title from first user message if not set
        if not chat_session.title and request.role == "user":
            title = request.content[:50] + "..." if len(request.content) > 50 else request.content
            chat_session.title = title
        
        await db.commit()
        await db.refresh(message)
        
        logger.info(f"Added {request.role} message to session {session_id}")
        
        return ChatMessageResponse(
            id=str(message.id),
            role=message.role,
            content=message.content,
            workflow_id=str(message.agent_plan_id) if message.agent_plan_id else (str(message.workflow_id) if message.workflow_id else None),
            message_data=message.message_data,  # Include full PydanticAI message (Session 69)
            created_at=message.created_at
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to add message to session: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to add message: {str(e)}")


# ============================================================================
# Health Check
# ============================================================================

@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint"""
    return HealthResponse(
        status="healthy",
        service="meho-agent",
        version="0.1.0"
    )


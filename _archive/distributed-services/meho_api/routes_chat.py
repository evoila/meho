"""
Chat routes with streaming support (Server-Sent Events).

Provides Cursor-like streaming UX for MEHO chat interface.
"""
# mypy: disable-error-code="no-untyped-def,arg-type"
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import json
from meho_core.auth_context import UserContext
from meho_api.auth import get_current_user
from meho_api.http_clients import get_agent_client as get_agent_http_client
from meho_api.config import get_api_config
from meho_api.dependencies import create_state_store
from meho_agent.session_state import AgentSessionState
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


async def get_conversation_history(session_id: str, limit: int = 10) -> List[Dict[str, str]]:
    """
    Fetch conversation history for context-aware chat.
    
    Enables LLM to understand references like "that endpoint" or "the system we discussed".
    
    Args:
        session_id: Chat session ID
        limit: Maximum number of messages to fetch (default: 10, optimized for performance)
        
    Returns:
        List of messages in format: [{"role": "user", "content": "..."}, ...]
        Ordered oldest to newest (for LLM context)
    """
    if not session_id:
        return []
    
    try:
        # Use HTTP client to fetch session data
        http_client = get_agent_http_client()
        session_data = await http_client.get_chat_session(session_id)
        
        if not session_data or "messages" not in session_data:
            logger.warning(f"No session data found for {session_id}")
            return []
        
        messages = session_data["messages"]
        
        # Take last N messages and format for LLM
        recent_messages = messages[-limit:] if len(messages) > limit else messages
        
        # Return full message data if available (Session 69 - includes tool calls)
        # Otherwise fallback to simple role/content format
        history = []
        for msg in recent_messages:
            if msg.get("message_data"):
                # Full PydanticAI message available - use it!
                message_data = msg["message_data"]
                
                # message_data could be:
                # - A single message dict: {"role": "...", "parts": [...]}
                # - A list of message dicts: [{...}, {...}, ...]
                if isinstance(message_data, list):
                    # Multiple messages (e.g., all new_messages from a turn)
                    # Extend history with all of them
                    history.extend(message_data)
                elif isinstance(message_data, dict):
                    # Single message - append it
                    history.append(message_data)
                else:
                    # Unexpected format - log and skip
                    logger.warning(f"Unexpected message_data type: {type(message_data)}")
            else:
                # Fallback to simple format (backward compatibility)
                history.append({"role": msg["role"], "content": msg["content"]})
        
        logger.info(f"Fetched {len(history)} messages from session {session_id} for context")
        tool_call_count = sum(1 for msg in history if isinstance(msg, dict) and "parts" in msg)
        if tool_call_count > 0:
            logger.info(f"   Including {tool_call_count} messages with tool calls/results")
        return history
        
    except Exception as e:
        logger.error(f"Error fetching conversation history: {e}", exc_info=True)
        return []



class ChatRequest(BaseModel):
    """Chat request from user"""
    message: str
    system_context: Optional[str] = None  # Optional: specific system to query
    session_id: Optional[str] = None  # Optional: link to chat session


class ChatResponse(BaseModel):
    """Chat response (non-streaming)"""
    response: str
    workflow_id: Optional[str] = None


# =============================================================================
# TASK-76: Approval Flow Endpoints
# =============================================================================


class ApprovalDecision(BaseModel):
    """Request body for approval decision."""
    approved: bool
    reason: Optional[str] = None


class ApprovalResponse(BaseModel):
    """Response from approval decision."""
    status: str  # "approved", "rejected", "error"
    message: str
    approval_id: Optional[str] = None


class PendingApproval(BaseModel):
    """Pending approval request info."""
    approval_id: str
    tool_name: str
    danger_level: str
    method: Optional[str] = None
    path: Optional[str] = None
    description: Optional[str] = None
    created_at: str


@router.post("/{session_id}/approve/{approval_id}", response_model=ApprovalResponse)
async def approve_action(
    session_id: str,
    approval_id: str,
    decision: ApprovalDecision,
    user: UserContext = Depends(get_current_user),
):
    """
    Approve or reject a pending action.
    
    TASK-76: Approval Flow Architecture
    
    When agent wants to execute a dangerous operation (POST, PUT, DELETE),
    it yields an approval_required event. The frontend displays an approval
    dialog, and the user calls this endpoint to approve or reject.
    
    Args:
        session_id: Chat session ID
        approval_id: Approval request UUID
        decision: ApprovalDecision with approved=True/False
        
    Returns:
        ApprovalResponse with status and message
    """
    from uuid import UUID
    from meho_agent.database import create_session_maker
    from meho_agent.approval import ApprovalStore
    from meho_agent.approval.exceptions import (
        ApprovalNotFound,
        ApprovalExpired,
        ApprovalAlreadyDecided,
    )
    
    logger.info(
        f"📋 Approval decision: session={session_id[:8]}..., "
        f"approval={approval_id[:8]}..., approved={decision.approved}"
    )
    
    try:
        session_maker = create_session_maker()
        async with session_maker() as db_session:
            store = ApprovalStore(db_session)
            
            if decision.approved:
                approval = await store.approve(
                    approval_id=UUID(approval_id),
                    decided_by=user.user_id,
                    reason=decision.reason,
                )
                await db_session.commit()
                
                logger.info(f"✅ Approval {approval_id[:8]}... approved by {user.user_id}")
                
                return ApprovalResponse(
                    status="approved",
                    message="Action approved. You may need to re-send your message to execute it.",
                    approval_id=approval_id,
                )
            else:
                approval = await store.reject(
                    approval_id=UUID(approval_id),
                    decided_by=user.user_id,
                    reason=decision.reason,
                )
                await db_session.commit()
                
                logger.info(f"❌ Approval {approval_id[:8]}... rejected by {user.user_id}")
                
                return ApprovalResponse(
                    status="rejected",
                    message="Action rejected.",
                    approval_id=approval_id,
                )
    
    except ApprovalNotFound:
        logger.warning(f"⚠️ Approval not found: {approval_id}")
        raise HTTPException(status_code=404, detail="Approval request not found")
    
    except ApprovalExpired as e:
        logger.warning(f"⚠️ Approval expired: {approval_id}")
        raise HTTPException(status_code=400, detail=f"Approval request expired at {e.expired_at}")
    
    except ApprovalAlreadyDecided as e:
        logger.warning(f"⚠️ Approval already decided: {approval_id} ({e.current_status})")
        raise HTTPException(status_code=400, detail=f"Approval already {e.current_status}")
    
    except Exception as e:
        logger.error(f"❌ Error processing approval: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{session_id}/pending-approvals", response_model=List[PendingApproval])
async def get_pending_approvals(
    session_id: str,
    user: UserContext = Depends(get_current_user),
):
    """
    Get all pending approval requests for a session.
    
    TASK-76: Approval Flow Architecture
    
    Used by frontend to show pending approval dialogs if user refreshes
    or reconnects while an approval is pending.
    
    Args:
        session_id: Chat session ID
        
    Returns:
        List of PendingApproval objects
    """
    from uuid import UUID
    from meho_agent.database import create_session_maker
    from meho_agent.approval import ApprovalStore
    
    logger.info(f"📋 Getting pending approvals for session {session_id[:8]}...")
    
    try:
        session_maker = create_session_maker()
        async with session_maker() as db_session:
            store = ApprovalStore(db_session)
            
            pending = await store.get_pending_for_session(
                session_id=UUID(session_id),
                tenant_id=user.tenant_id,
            )
            
            return [
                PendingApproval(
                    approval_id=str(p.id),
                    tool_name=p.tool_name,
                    danger_level=p.danger_level.value,
                    method=p.http_method,
                    path=p.endpoint_path,
                    description=p.description,
                    created_at=p.created_at.isoformat(),
                )
                for p in pending
            ]
    
    except Exception as e:
        logger.error(f"❌ Error getting pending approvals: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


class ResumeRequest(BaseModel):
    """Request body for resume after approval."""
    approval_id: str


class ResumeResponse(BaseModel):
    """Response from resume after approval."""
    status: str  # "success", "error"
    message: str


@router.post("/{session_id}/resume", response_model=ResumeResponse)
async def resume_after_approval(
    session_id: str,
    request: ResumeRequest,
    user: UserContext = Depends(get_current_user),
):
    """
    Resume execution after approval.
    
    TASK-76: After user approves a dangerous action, this endpoint
    triggers re-execution of the original user message. Since the
    approval now exists in the database, the tool will find it and
    proceed without asking again.
    
    This returns immediately with a status - the actual execution
    happens when the frontend re-sends the chat message via /stream.
    
    Note: The recommended flow is:
    1. User sees approval_required event
    2. User clicks "Allow" → POST /approve/{approval_id} 
    3. Frontend re-submits original message to /stream
    4. Agent finds existing approval, executes tool
    
    This endpoint is provided for cases where frontend needs to
    explicitly trigger resume rather than re-sending the message.
    
    Args:
        session_id: Chat session ID
        request: ResumeRequest with approval_id
        
    Returns:
        ResumeResponse with status and message
    """
    from uuid import UUID
    from meho_agent.database import create_session_maker
    from meho_agent.approval import ApprovalStore
    from meho_agent.models import ApprovalStatus
    
    logger.info(f"🔄 Resume request: session={session_id[:8]}..., approval={request.approval_id[:8]}...")
    
    try:
        session_maker = create_session_maker()
        async with session_maker() as db_session:
            store = ApprovalStore(db_session)
            
            # Get the approval request
            approval = await store.get_by_id(UUID(request.approval_id))
            
            if not approval:
                raise HTTPException(status_code=404, detail="Approval not found")
            
            # Check if it belongs to this session
            if str(approval.session_id) != session_id:
                raise HTTPException(status_code=400, detail="Approval does not belong to this session")
            
            # Check if it's approved
            if approval.status != ApprovalStatus.APPROVED:
                return ResumeResponse(
                    status="error",
                    message=f"Approval is {approval.status.value}, not approved"
                )
            
            # The frontend should re-send the original message to /stream
            # The agent will find the approval and execute the tool
            return ResumeResponse(
                status="success",
                message="Approval is valid. Re-send the original message to /chat/stream to execute."
            )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error resuming: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# TASK-89: ReAct Graph Architecture (v2 Streaming)
# =============================================================================


@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Stream chat responses using the ReAct graph architecture (TASK-89).
    
    ReAct-based implementation that provides:
    - Explicit Thought → Action → Observation loops
    - Visible reasoning (thoughts shown to user)
    - Built-in depth limiting (no infinite loops)
    - Cleaner approval flow integration
    - Generic design - works with REST, SOAP, VMware, any system
    
    SSE Message Types:
    - 'thought': Agent reasoning step
    - 'action': Tool being called
    - 'observation': Tool result
    - 'approval_required': Needs user approval
    - 'final_answer': Complete response
    - 'done': Stream complete
    - 'error': Something failed
    """
    from meho_agent.react import MEHOReActGraph
    from meho_api.database import create_bff_session_maker
    from meho_api.dependencies import create_agent_dependencies
    from meho_agent.unified_executor import get_unified_executor
    from meho_agent.state_store import get_redis_client
    from meho_api.config import get_api_config
    
    async def generate():
        """Generate SSE stream with ReAct graph"""
        session_maker = create_bff_session_maker()
        state_store = await create_state_store()
        
        # TASK-93: Initialize UnifiedExecutor with Redis for persistent response cache
        config = get_api_config()
        redis_client = await get_redis_client(config.redis_url)
        get_unified_executor(redis_client=redis_client)  # Upgrade singleton with Redis
        graph_state = None  # Track for saving after
        
        async with session_maker() as session:
            try:
                # Load agent state from Redis (same as v1!)
                from meho_agent.session_state import AgentSessionState
                session_state = None
                if request.session_id:
                    session_state = await state_store.load_state(request.session_id)
                    if session_state:
                        logger.info(f"📬 v2: Loaded state for session {request.session_id[:8]}...")
                        logger.info(f"   Entities: {len(session_state.entities)}")
                    else:
                        logger.info(f"📭 v2: No state for {request.session_id[:8]}... (creating new)")
                        session_state = AgentSessionState()
                else:
                    session_state = AgentSessionState()
                
                # Create dependencies with loaded state
                dependencies = await create_agent_dependencies(user, session)
                dependencies.session_state = session_state  # Inject loaded state!
                
                # Setup approval store
                if request.session_id:
                    from meho_agent.approval import ApprovalStore
                    approval_store = ApprovalStore(session)
                else:
                    approval_store = None
                
                # Load agent config (model, prompts, etc.) from config file + env vars
                from meho_agent.agent_config import get_agent_config
                agent_config = await get_agent_config(
                    tenant_id=user.tenant_id,
                )
                
                # Create ReAct graph with MEHODependencies
                # All business logic (credentials, BM25 search, etc.) comes from dependencies
                graph = MEHOReActGraph(
                    meho_dependencies=dependencies,  # Pass the full MEHODependencies!
                    approval_store=approval_store,
                    llm_model=agent_config.model.name,  # From config/env, not hardcoded!
                    max_steps=10,
                )
                
                # Load conversation history for multi-turn context
                conversation_history = await get_conversation_history(request.session_id) if request.session_id else []
                logger.info(f"📜 Loaded {len(conversation_history)} messages from conversation history")
                
                # Save user message to session BEFORE streaming (for next turn context)
                # This is critical for multi-turn conversations!
                if request.session_id:
                    try:
                        agent_http_client = get_agent_http_client()
                        
                        # Auto-create session if it doesn't exist
                        if len(conversation_history) == 0:
                            try:
                                existing = await agent_http_client.get_chat_session(request.session_id)
                                if not existing:
                                    raise Exception("Session not found")
                            except Exception:
                                logger.info(f"📝 v2: Auto-creating session {request.session_id[:8]}...")
                                await agent_http_client.create_chat_session(
                                    tenant_id=user.tenant_id or "default",
                                    user_id=user.user_id or "anonymous",
                                    title=f"Chat {request.session_id[:8]}",
                                    session_id=request.session_id
                                )
                        
                        # Save user message
                        await agent_http_client.add_message(
                            session_id=request.session_id,
                            role="user",
                            content=request.message,
                            workflow_id=None,
                            message_data=None
                        )
                        logger.info(f"💾 v2: Saved user message to session")
                    except Exception as e:
                        logger.error(f"v2: Failed to save user message: {e}")
                
                # Track final answer for saving
                final_answer_content = None
                
                # Stream from graph (pass conversation history for context!)
                async for event in graph.run_streaming(
                    user_message=request.message,
                    session_id=request.session_id,
                    user_id=user.user_id or "anonymous",
                    existing_state=None,  # Graph manages its own state
                    conversation_history=conversation_history,  # Previous messages for multi-turn context
                ):
                    # Convert GraphEvent to SSE format
                    sse_data = {
                        "type": event.type,
                        **event.data
                    }
                    yield f"data: {json.dumps(sse_data)}\n\n"
                    
                    # Log progress
                    if event.type == "thought":
                        logger.info(f"💭 Thought: {event.data.get('content', '')[:50]}...")
                    elif event.type == "action":
                        logger.info(f"🔧 Action: {event.data.get('tool', '')}")
                    elif event.type == "observation":
                        logger.info(f"👁️ Observation: {event.data.get('result', '')[:50]}...")
                    elif event.type == "final_answer":
                        logger.info(f"✅ Final Answer ready")
                        # Capture final answer for saving
                        final_answer_content = event.data.get('content', '')
                    elif event.type == "approval_required":
                        logger.info(f"🚨 Approval required: {event.data.get('description', '')}")
                
                # Save assistant message after streaming completes
                if request.session_id and final_answer_content:
                    try:
                        agent_http_client = get_agent_http_client()
                        await agent_http_client.add_message(
                            session_id=request.session_id,
                            role="assistant",
                            content=final_answer_content,
                            workflow_id=None,
                            message_data=None  # v2 doesn't track PydanticAI messages yet
                        )
                        logger.info(f"💾 v2: Saved assistant message to session")
                    except Exception as e:
                        logger.error(f"v2: Failed to save assistant message: {e}")
                
                # Send completion
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                
                # Save state to Redis (persist entities for next request!)
                if request.session_id and dependencies.session_state:
                    await state_store.save_state(
                        request.session_id, 
                        dependencies.session_state
                    )
                    logger.info(f"💾 v2: Saved state for session {request.session_id[:8]}...")
                    logger.info(f"   Entities: {len(dependencies.session_state.entities)}")
                
            except Exception as e:
                logger.error(f"ReAct graph error: {e}", exc_info=True)
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
            finally:
                await session.close()
    
    return StreamingResponse(generate(), media_type="text/event-stream")

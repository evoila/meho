"""
Chat session management routes for conversation persistence.

HTTP-based BFF implementation (Task 36) - all operations via Agent Service HTTP API.

Enables users to:
- Create and manage chat sessions
- View conversation history
- Continue previous conversations
- Search and organize chats
"""
# mypy: disable-error-code="no-untyped-def,arg-type"
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from uuid import UUID
from meho_core.auth_context import UserContext
from meho_api.auth import get_current_user
from meho_api.http_clients import get_agent_client
from meho_core.structured_logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/chat/sessions", tags=["chat-sessions"])


# ============================================================================
# Request/Response Models
# ============================================================================

class CreateSessionRequest(BaseModel):
    """Create a new chat session"""
    title: Optional[str] = None  # Auto-generated if not provided


class UpdateSessionRequest(BaseModel):
    """Update session metadata"""
    title: str


class SessionResponse(BaseModel):
    """Chat session response"""
    id: str
    title: Optional[str]
    created_at: datetime
    updated_at: datetime
    message_count: Optional[int] = None  # Included in list view
    
    class Config:
        from_attributes = True


class MessageResponse(BaseModel):
    """Chat message response"""
    id: str
    role: str  # 'user' or 'assistant'
    content: str
    workflow_id: Optional[str]
    created_at: datetime
    
    class Config:
        from_attributes = True


class SessionWithMessagesResponse(BaseModel):
    """Session with full message history"""
    id: str
    title: Optional[str]
    created_at: datetime
    updated_at: datetime
    messages: List[MessageResponse]
    
    class Config:
        from_attributes = True


class AddMessageRequest(BaseModel):
    """Add a message to a session"""
    role: str  # 'user' or 'assistant'
    content: str
    workflow_id: Optional[str] = None


# ============================================================================
# Routes (HTTP-based via Agent Service)
# ============================================================================

@router.post("", response_model=SessionResponse)
async def create_session(
    request: CreateSessionRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Create a new chat session via Agent Service HTTP API.
    
    Sessions are used to persist conversations across page refreshes
    and allow users to return to previous conversations.
    """
    agent_client = get_agent_client()
    
    try:
        session_data = await agent_client.create_chat_session(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            title=request.title
        )
        
        logger.info(f"Created chat session {session_data['id']} for user {user.user_id}")
        
        return SessionResponse(**session_data)
        
    except Exception as e:
        logger.error(f"Error creating chat session: {e}")
        raise HTTPException(status_code=500, detail="Failed to create chat session")


@router.get("", response_model=List[SessionResponse])
async def list_sessions(
    user: UserContext = Depends(get_current_user),
    limit: int = 50
):
    """
    List all chat sessions for the current user via Agent Service HTTP API.
    
    Returns sessions ordered by most recent first.
    Includes message count for each session.
    """
    agent_client = get_agent_client()
    
    try:
        sessions = await agent_client.list_chat_sessions(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            limit=limit
        )
        
        logger.info(f"Listed {len(sessions)} sessions for user {user.user_id}")
        
        return [SessionResponse(**session) for session in sessions]
        
    except Exception as e:
        logger.error(f"Error listing chat sessions: {e}")
        raise HTTPException(status_code=500, detail="Failed to list chat sessions")


@router.get("/{session_id}", response_model=SessionWithMessagesResponse)
async def get_session(
    session_id: UUID,
    user: UserContext = Depends(get_current_user)
):
    """
    Get a specific chat session with all messages via Agent Service HTTP API.
    
    This is used when loading a previous conversation.
    """
    agent_client = get_agent_client()
    
    try:
        session_data = await agent_client.get_chat_session(str(session_id))
        
        if not session_data:
            raise HTTPException(status_code=404, detail="Session not found")
        
        # Verify session belongs to user's tenant and user
        if session_data["tenant_id"] != user.tenant_id or session_data["user_id"] != user.user_id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        logger.info(f"Retrieved session {session_id} with {len(session_data['messages'])} messages")
        
        return SessionWithMessagesResponse(**session_data)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting chat session: {e}")
        raise HTTPException(status_code=500, detail="Failed to get chat session")


@router.patch("/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: UUID,
    request: UpdateSessionRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Update session metadata (e.g., change title) via Agent Service HTTP API.
    """
    agent_client = get_agent_client()
    
    try:
        # Verify session exists and belongs to user
        session_data = await agent_client.get_chat_session(str(session_id))
        
        if not session_data:
            raise HTTPException(status_code=404, detail="Session not found")
        
        if session_data["tenant_id"] != user.tenant_id or session_data["user_id"] != user.user_id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Update session
        updated_session = await agent_client.update_chat_session(
            session_id=str(session_id),
            title=request.title
        )
        
        logger.info(f"Updated session {session_id} title to '{request.title}'")
        
        return SessionResponse(**updated_session)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating chat session: {e}")
        raise HTTPException(status_code=500, detail="Failed to update chat session")


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: UUID,
    user: UserContext = Depends(get_current_user)
):
    """
    Delete a chat session and all its messages via Agent Service HTTP API.
    """
    agent_client = get_agent_client()
    
    try:
        # Verify session exists and belongs to user
        session_data = await agent_client.get_chat_session(str(session_id))
        
        if not session_data:
            raise HTTPException(status_code=404, detail="Session not found")
        
        if session_data["tenant_id"] != user.tenant_id or session_data["user_id"] != user.user_id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Delete session
        await agent_client.delete_chat_session(str(session_id))
        
        logger.info(f"Deleted session {session_id}")
        return None
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting chat session: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete chat session")


@router.post("/{session_id}/messages", response_model=MessageResponse)
async def add_message(
    session_id: UUID,
    request: AddMessageRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Add a message to a chat session via Agent Service HTTP API.
    
    This is used to persist both user messages and assistant responses.
    """
    agent_client = get_agent_client()
    
    try:
        # Verify session exists and belongs to user
        session_data = await agent_client.get_chat_session(str(session_id))
        
        if not session_data:
            raise HTTPException(status_code=404, detail="Session not found")
        
        if session_data["tenant_id"] != user.tenant_id or session_data["user_id"] != user.user_id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Add message
        message_data = await agent_client.add_message(
            session_id=str(session_id),
            role=request.role,
            content=request.content,
            workflow_id=request.workflow_id
        )
        
        logger.info(f"Added {request.role} message to session {session_id}")
        
        return MessageResponse(**message_data)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding message to session: {e}")
        raise HTTPException(status_code=500, detail="Failed to add message")

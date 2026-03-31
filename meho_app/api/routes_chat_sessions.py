# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Chat session management routes for conversation persistence.

Direct service implementation - all operations via AgentService (modular monolith).

Enables users to:
- Create and manage chat sessions
- View conversation history
- Continue previous conversations
- Search and organize chats
- Upgrade session visibility (private -> group -> tenant)
"""

# mypy: disable-error-code="no-untyped-def,arg-type"
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from meho_app.api.dependencies import AgentServiceDep, CurrentUser
from meho_app.core.otel import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/chat/sessions", tags=["chat-sessions"])


# ============================================================================
# Request/Response Models
# ============================================================================


class CreateSessionRequest(BaseModel):
    """Create a new chat session"""

    title: str | None = None  # Auto-generated if not provided


class UpdateSessionRequest(BaseModel):
    """Update session metadata"""

    title: str


class SessionResponse(BaseModel):
    """Chat session response"""

    id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int | None = None  # Included in list view
    visibility: str | None = None  # Phase 38: session visibility level
    is_active: bool = False  # Phase 59: whether agent is currently processing
    session_mode: str = "agent"  # Phase 65: "ask" or "agent" mode

    class Config:
        from_attributes = True


class MessageResponse(BaseModel):
    """Chat message response"""

    id: str
    role: str  # 'user' or 'assistant'
    content: str
    created_at: datetime
    # Phase 39: War room sender attribution
    sender_id: str | None = None
    sender_name: str | None = None

    class Config:
        from_attributes = True


class SessionWithMessagesResponse(BaseModel):
    """Session with full message history"""

    id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageResponse]
    visibility: str | None = None  # Phase 39: session visibility for group detection
    is_active: bool = False  # Phase 39: whether agent is currently processing
    session_mode: str = "agent"  # Phase 65: "ask" or "agent" mode
    trigger_source: str | None = None  # Phase 75: null=human session, else automation trigger name
    created_by_name: str | None = None  # Phase 75: display name of session creator

    class Config:
        from_attributes = True


class AddMessageRequest(BaseModel):
    """Add a message to a session"""

    role: str  # 'user' or 'assistant'
    content: str


# Phase 38: Team sessions and visibility models


class TeamSessionResponse(BaseModel):
    """Team session response with status derivation and approval counts."""

    id: str
    title: str | None
    visibility: str
    created_by_name: str | None
    trigger_source: str | None
    status: str  # "awaiting_approval" or "idle"
    pending_approval_count: int
    created_at: datetime
    updated_at: datetime


class UpdateVisibilityRequest(BaseModel):
    """Request to upgrade session visibility."""

    visibility: str  # "group" or "tenant"


# ============================================================================
# Routes (HTTP-based via Agent Service)
# ============================================================================


@router.post("", response_model=SessionResponse)
async def create_session(
    request: CreateSessionRequest,
    user: CurrentUser,
    agent_service: AgentServiceDep,
):
    """
    Create a new chat session via AgentService.

    Sessions are used to persist conversations across page refreshes
    and allow users to return to previous conversations.
    """
    try:
        session_obj = await agent_service.create_chat_session(
            tenant_id=user.tenant_id, user_id=user.user_id, title=request.title
        )

        logger.info(f"Created chat session {session_obj.id} for user {user.user_id}")

        return SessionResponse(
            id=str(session_obj.id),
            title=session_obj.title,
            created_at=session_obj.created_at,
            updated_at=session_obj.updated_at,
            message_count=0,
            visibility=session_obj.visibility,
            session_mode=getattr(session_obj, "session_mode", "agent"),
        )

    except Exception as e:
        logger.error(f"Error creating chat session: {e}")
        raise HTTPException(status_code=500, detail="Failed to create chat session") from e


@router.get("", response_model=list[SessionResponse])
async def list_sessions(user: CurrentUser, agent_service: AgentServiceDep, limit: int = 50):
    """
    List all chat sessions for the current user via AgentService.

    Returns sessions ordered by most recent first.
    Includes message count for each session.
    """
    try:
        sessions = await agent_service.list_chat_sessions(
            tenant_id=user.tenant_id, user_id=user.user_id, limit=limit
        )

        logger.info(f"Listed {len(sessions)} sessions for user {user.user_id}")

        # Phase 59: Batch-check is_active for each session via Redis
        active_session_ids: set = set()
        try:
            from meho_app.core.config import get_config
            from meho_app.core.redis import get_redis_client
            from meho_app.modules.agents.sse.broadcaster import RedisSSEBroadcaster

            config = get_config()
            redis_client = await get_redis_client(config.redis_url)
            broadcaster = RedisSSEBroadcaster(redis_client)
            for s in sessions:
                try:
                    if await broadcaster.is_active(str(s.id)):
                        active_session_ids.add(str(s.id))
                except Exception:  # noqa: S110 -- intentional silent exception handling
                    pass  # Gracefully degrade per session
        except Exception:
            logger.debug("Redis unavailable for is_active batch check, defaulting all to False")

        # Convert to response format - don't access messages relationship (lazy load issue)
        return [
            SessionResponse(
                id=str(s.id),
                title=s.title,
                created_at=s.created_at,
                updated_at=s.updated_at,
                message_count=None,  # Don't access messages to avoid lazy load
                visibility=getattr(s, "visibility", "private"),
                is_active=str(s.id) in active_session_ids,
                session_mode=getattr(s, "session_mode", "agent"),
            )
            for s in sessions
        ]

    except Exception as e:
        logger.error(f"Error listing chat sessions: {e}")
        raise HTTPException(status_code=500, detail="Failed to list chat sessions") from e


# Phase 38: Team Sessions Endpoint -- extracted to routes_enterprise_sessions.py (Phase 80)


@router.get("/{session_id}", response_model=SessionWithMessagesResponse)
async def get_session(
    session_id: UUID,
    user: CurrentUser,
    agent_service: AgentServiceDep,
):
    """
    Get a specific chat session with all messages via AgentService.

    This is used when loading a previous conversation.
    Group/tenant sessions are accessible to any user in the same tenant.
    Private sessions are accessible only to the session owner.
    """
    try:
        session_obj = await agent_service.get_chat_session(str(session_id), include_messages=True)

        if not session_obj:
            raise HTTPException(status_code=404, detail="Session not found")

        # Phase 38: Group-aware access check
        if session_obj.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")
        if session_obj.visibility == "private" and session_obj.user_id != user.user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        logger.info(f"Retrieved session {session_id} with {len(session_obj.messages)} messages")

        # Phase 39: Check if agent is currently processing this session via Redis
        is_active = False
        try:
            from meho_app.core.config import get_config
            from meho_app.core.redis import get_redis_client
            from meho_app.modules.agents.sse.broadcaster import RedisSSEBroadcaster

            config = get_config()
            redis_client = await get_redis_client(config.redis_url)
            broadcaster = RedisSSEBroadcaster(redis_client)
            is_active = await broadcaster.is_active(str(session_id))
        except Exception:
            logger.debug(
                f"Redis unavailable for is_active check on session {session_id}, defaulting to False"
            )

        return SessionWithMessagesResponse(
            id=str(session_obj.id),
            title=session_obj.title,
            created_at=session_obj.created_at,
            updated_at=session_obj.updated_at,
            visibility=getattr(session_obj, "visibility", None),
            is_active=is_active,
            session_mode=getattr(session_obj, "session_mode", "agent"),
            trigger_source=getattr(session_obj, "trigger_source", None),  # Phase 75
            created_by_name=getattr(session_obj, "created_by_name", None),  # Phase 75
            messages=[
                MessageResponse(
                    id=str(m.id),
                    role=m.role,
                    content=m.content,
                    created_at=m.created_at,
                    sender_id=getattr(m, "sender_id", None),
                    sender_name=getattr(m, "sender_name", None),
                )
                for m in session_obj.messages
            ],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting chat session: {e}")
        raise HTTPException(status_code=500, detail="Failed to get chat session") from e


@router.patch("/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: UUID,
    request: UpdateSessionRequest,
    user: CurrentUser,
    agent_service: AgentServiceDep,
):
    """
    Update session metadata (e.g., change title) via AgentService.

    Only the session owner can edit a session (regardless of visibility).
    """
    try:
        # Verify session exists and belongs to user (owner-only for edits)
        session_obj = await agent_service.get_chat_session(str(session_id))

        if not session_obj:
            raise HTTPException(status_code=404, detail="Session not found")

        if session_obj.tenant_id != user.tenant_id or session_obj.user_id != user.user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        # Update session
        updated_session = await agent_service.update_chat_session(
            session_id=str(session_id), title=request.title
        )

        logger.info(f"Updated session {session_id} title to '{request.title}'")

        return SessionResponse(
            id=str(updated_session.id),
            title=updated_session.title,
            created_at=updated_session.created_at,
            updated_at=updated_session.updated_at,
            visibility=updated_session.visibility,
            session_mode=getattr(updated_session, "session_mode", "agent"),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating chat session: {e}")
        raise HTTPException(status_code=500, detail="Failed to update chat session") from e


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: UUID,
    user: CurrentUser,
    agent_service: AgentServiceDep,
):
    """
    Delete a chat session and all its messages via AgentService.

    Only the session owner can delete a session (regardless of visibility).
    """
    try:
        # Verify session exists and belongs to user (owner-only for deletes)
        session_obj = await agent_service.get_chat_session(str(session_id))

        if not session_obj:
            raise HTTPException(status_code=404, detail="Session not found")

        if session_obj.tenant_id != user.tenant_id or session_obj.user_id != user.user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        # Delete session
        await agent_service.delete_chat_session(str(session_id))

        logger.info(f"Deleted session {session_id}")
        return None

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting chat session: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete chat session") from e


@router.post("/{session_id}/messages", response_model=MessageResponse)
async def add_message(
    session_id: UUID,
    request: AddMessageRequest,
    user: CurrentUser,
    agent_service: AgentServiceDep,
):
    """
    Add a message to a chat session via AgentService.

    This is used to persist both user messages and assistant responses.
    """
    try:
        # Verify session exists and belongs to user
        session_obj = await agent_service.get_chat_session(str(session_id))

        if not session_obj:
            raise HTTPException(status_code=404, detail="Session not found")

        if session_obj.tenant_id != user.tenant_id or session_obj.user_id != user.user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        # Add message
        message_obj = await agent_service.add_chat_message(
            session_id=str(session_id),
            role=request.role,
            content=request.content,
        )

        logger.info(f"Added {request.role} message to session {session_id}")

        return MessageResponse(
            id=str(message_obj.id),
            role=message_obj.role,
            content=message_obj.content,
            created_at=message_obj.created_at,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding message to session: {e}")
        raise HTTPException(status_code=500, detail="Failed to add message") from e


# ============================================================================
# Phase 38: Visibility Upgrade Endpoint
# ============================================================================


@router.patch("/{session_id}/visibility", response_model=SessionResponse)
async def update_session_visibility(
    session_id: UUID,
    request: UpdateVisibilityRequest,
    user: CurrentUser,
    agent_service: AgentServiceDep,
):
    """
    Upgrade a session's visibility level.

    Only the session owner can upgrade visibility.
    Visibility can only go up: private -> group -> tenant. Downgrades are rejected.
    """
    try:
        # Verify session exists
        session_obj = await agent_service.get_chat_session(str(session_id))

        if not session_obj:
            raise HTTPException(status_code=404, detail="Session not found")

        # Only the session owner can upgrade visibility
        if session_obj.tenant_id != user.tenant_id or session_obj.user_id != user.user_id:
            raise HTTPException(
                status_code=403, detail="Only the session owner can change visibility"
            )

        # Attempt upgrade (raises ValueError for invalid transitions)
        updated_session = await agent_service.update_session_visibility(
            session_id=str(session_id),
            visibility=request.visibility,
            user_id=user.user_id,
        )

        logger.info(f"Updated session {session_id} visibility to '{request.visibility}'")

        return SessionResponse(
            id=str(updated_session.id),
            title=updated_session.title,
            created_at=updated_session.created_at,
            updated_at=updated_session.updated_at,
            visibility=updated_session.visibility,
            session_mode=getattr(updated_session, "session_mode", "agent"),
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating session visibility: {e}")
        raise HTTPException(status_code=500, detail="Failed to update session visibility") from e


# ============================================================================
# Phase 65: Session Mode Switching Endpoint
# ============================================================================


class UpdateSessionModeRequest(BaseModel):
    """Request to change session mode (ask/agent)."""

    session_mode: str  # "ask" or "agent"


@router.patch("/{session_id}/mode", response_model=SessionResponse)
async def update_session_mode(
    session_id: UUID,
    request: UpdateSessionModeRequest,
    user: CurrentUser,
    agent_service: AgentServiceDep,
):
    """
    Switch a session's mode between ask and agent.

    Phase 65: Mid-session mode switching. Any user with access to the session
    can switch modes. Mode applies to all subsequent messages until changed again.
    """
    if request.session_mode not in ("ask", "agent"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid session_mode: '{request.session_mode}'. Must be 'ask' or 'agent'.",
        )

    try:
        session_obj = await agent_service.get_chat_session(str(session_id))

        if not session_obj:
            raise HTTPException(status_code=404, detail="Session not found")

        # Access check: same tenant required
        if session_obj.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")
        # Private sessions: owner only
        if session_obj.visibility == "private" and session_obj.user_id != user.user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        # Update session_mode directly on the model
        session_obj.session_mode = request.session_mode

        # Use the agent_service's internal session to commit
        await agent_service.session.commit()
        await agent_service.session.refresh(session_obj)

        logger.info(
            f"Phase 65: Session {session_id} mode switched to '{request.session_mode}' "
            f"by user {user.user_id}"
        )

        return SessionResponse(
            id=str(session_obj.id),
            title=session_obj.title,
            created_at=session_obj.created_at,
            updated_at=session_obj.updated_at,
            visibility=getattr(session_obj, "visibility", "private"),
            session_mode=session_obj.session_mode,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating session mode: {e}")
        raise HTTPException(status_code=500, detail="Failed to update session mode") from e

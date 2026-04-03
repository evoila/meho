# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
FastAPI routes for Agent HTTP Service.

Provides chat session management and health check endpoints.
Note: Workflow routes removed - ReAct agent operates without persistent plan storage.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,assignment,union-attr,misc"
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.api.auth import get_current_user
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.modules.agents.api_schemas import (
    AddMessageRequest,
    ChatMessageResponse,
    ChatSessionResponse,
    ChatSessionWithMessagesResponse,
    CreateChatSessionRequest,
    HealthResponse,
    UpdateChatSessionRequest,
)
from meho_app.modules.agents.models import ChatMessageModel, ChatSessionModel

logger = get_logger(__name__)

MSG_SESSION_NOT_FOUND = "Session not found"

router = APIRouter(prefix="/agent", tags=["agent"])


# ============================================================================
# Dependency Injection
# ============================================================================


async def get_db_session() -> AsyncSession:
    """Get database session for agent service"""
    from meho_app.database import get_db_session as _get_db_session

    async for session in _get_db_session():
        yield session


# ============================================================================
# Chat Session Routes
# ============================================================================


@router.post(
    "/chat/sessions",
    response_model=ChatSessionResponse,
    status_code=201,
    responses={500: {"description": "Failed to create chat session: ..."}},
)
async def create_chat_session(
    request: CreateChatSessionRequest,
    user_context: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    """Create a new chat session"""
    try:
        # Use client-provided ID if available, otherwise generate new one
        session_id = UUID(request.id) if request.id else uuid4()

        # Use tenant_id and user_id from JWT — never trust the request body
        chat_session = ChatSessionModel(
            id=session_id,
            tenant_id=user_context.tenant_id,
            user_id=user_context.user_id,
            title=request.title,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
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
            message_count=0,
        )
    except Exception as e:
        logger.error(f"Failed to create chat session: {e}", exc_info=True)
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create chat session: {e!s}") from e


@router.get(
    "/chat/sessions",
    response_model=list[ChatSessionResponse],
    responses={500: {"description": "Failed to list chat sessions: ..."}},
)
async def list_chat_sessions(
    user_context: Annotated[UserContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(le=200)] = 50,
):
    """List all chat sessions for the authenticated user"""
    try:
        # Get sessions with message counts — tenant_id and user_id from JWT
        stmt = (
            select(ChatSessionModel)
            .where(
                ChatSessionModel.tenant_id == user_context.tenant_id,
                ChatSessionModel.user_id == user_context.user_id,
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
            msg_stmt = select(func.count()).where(ChatMessageModel.session_id == chat_session.id)
            msg_result = await db.execute(msg_stmt)
            message_count = msg_result.scalar()

            session_responses.append(
                ChatSessionResponse(
                    id=str(chat_session.id),
                    tenant_id=chat_session.tenant_id,
                    user_id=chat_session.user_id,
                    title=chat_session.title,
                    created_at=chat_session.created_at,
                    updated_at=chat_session.updated_at,
                    message_count=message_count,
                )
            )

        logger.info(f"Listed {len(session_responses)} sessions for user {user_context.user_id}")
        return session_responses
    except Exception as e:
        logger.error(f"Failed to list chat sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list chat sessions: {e!s}") from e


@router.get(
    "/chat/sessions/{session_id}",
    response_model=ChatSessionWithMessagesResponse,
    responses={
        404: {"description": "Session not found"},
        500: {"description": "Failed to get chat session: ..."},
    },
)
async def get_chat_session(
    session_id: UUID,
    user_context: Annotated[UserContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
):
    """Get a specific chat session with all messages"""
    try:
        # Get session
        stmt = select(ChatSessionModel).where(ChatSessionModel.id == session_id)
        result = await db.execute(stmt)
        chat_session = result.scalar_one_or_none()

        # Verify tenant ownership — return 404 on mismatch (not 403, to avoid info disclosure)
        if not chat_session or chat_session.tenant_id != user_context.tenant_id:
            raise HTTPException(status_code=404, detail=MSG_SESSION_NOT_FOUND)

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
                    message_data=msg.message_data,
                    created_at=msg.created_at,
                )
                for msg in messages
            ],
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get chat session: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get chat session: {e!s}") from e


@router.patch(
    "/chat/sessions/{session_id}",
    response_model=ChatSessionResponse,
    responses={
        404: {"description": "Session not found"},
        500: {"description": "Failed to update chat session: ..."},
    },
)
async def update_chat_session(
    session_id: UUID,
    request: UpdateChatSessionRequest,
    user_context: Annotated[UserContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
):
    """Update session metadata (e.g., change title)"""
    try:
        # Get session
        stmt = select(ChatSessionModel).where(ChatSessionModel.id == session_id)
        result = await db.execute(stmt)
        chat_session = result.scalar_one_or_none()

        # Verify tenant ownership
        if not chat_session or chat_session.tenant_id != user_context.tenant_id:
            raise HTTPException(status_code=404, detail=MSG_SESSION_NOT_FOUND)

        # Update
        chat_session.title = request.title
        chat_session.updated_at = datetime.now(tz=UTC)

        await db.commit()
        await db.refresh(chat_session)

        logger.info(f"Updated session {session_id} title to '{request.title}'")

        return ChatSessionResponse(
            id=str(chat_session.id),
            tenant_id=chat_session.tenant_id,
            user_id=chat_session.user_id,
            title=chat_session.title,
            created_at=chat_session.created_at,
            updated_at=chat_session.updated_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update chat session: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update chat session: {e!s}") from e


@router.delete(
    "/chat/sessions/{session_id}",
    status_code=204,
    responses={
        404: {"description": "Session not found"},
        500: {"description": "Failed to delete chat session: ..."},
    },
)
async def delete_chat_session(
    session_id: UUID,
    user_context: Annotated[UserContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
):
    """Delete a chat session and all its messages"""
    try:
        # Get session
        stmt = select(ChatSessionModel).where(ChatSessionModel.id == session_id)
        result = await db.execute(stmt)
        chat_session = result.scalar_one_or_none()

        # Verify tenant ownership
        if not chat_session or chat_session.tenant_id != user_context.tenant_id:
            raise HTTPException(status_code=404, detail=MSG_SESSION_NOT_FOUND)

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
        raise HTTPException(status_code=500, detail=f"Failed to delete chat session: {e!s}") from e


@router.post(
    "/chat/sessions/{session_id}/messages", response_model=ChatMessageResponse, status_code=201
)
async def add_message(
    session_id: UUID,
    request: AddMessageRequest,
    user_context: Annotated[UserContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
):
    """Add a message to a chat session"""
    try:
        # Verify session exists and tenant ownership
        stmt = select(ChatSessionModel).where(ChatSessionModel.id == session_id)
        result = await db.execute(stmt)
        chat_session = result.scalar_one_or_none()

        if not chat_session or chat_session.tenant_id != user_context.tenant_id:
            raise HTTPException(status_code=404, detail=MSG_SESSION_NOT_FOUND)

        # Create message
        message = ChatMessageModel(
            id=uuid4(),
            session_id=session_id,
            role=request.role,
            content=request.content,
            message_data=request.message_data,
            created_at=datetime.now(tz=UTC),
        )

        db.add(message)

        # Update session updated_at
        chat_session.updated_at = datetime.now(tz=UTC)

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
            message_data=message.message_data,
            created_at=message.created_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to add message to session: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to add message: {e!s}") from e


# ============================================================================
# Health Check
# ============================================================================


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint"""
    return HealthResponse(status="healthy", service="meho-agent", version="0.1.0")

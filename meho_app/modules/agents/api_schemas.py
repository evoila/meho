# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
API schemas for Agent HTTP Service.

Provides schemas for chat session management.
Note: Workflow schemas removed - ReAct agent operates without persistent plan storage.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

# ============================================================================
# Health Check
# ============================================================================


class HealthResponse(BaseModel):
    """Health check response"""

    status: str
    service: str = "meho-agent"
    version: str = "0.1.0"


# ============================================================================
# Chat Session Schemas
# ============================================================================


class CreateChatSessionRequest(BaseModel):
    """Request to create a chat session"""

    tenant_id: str
    user_id: str
    title: str | None = None
    id: str | None = None  # Optional client-provided ID for session continuity


class UpdateChatSessionRequest(BaseModel):
    """Request to update a chat session"""

    title: str


class ChatSessionResponse(BaseModel):
    """Chat session response"""

    id: str
    tenant_id: str
    user_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int | None = None  # Included in list view
    session_mode: str = "agent"  # Phase 65: "ask" or "agent" mode


class ChatMessageResponse(BaseModel):
    """Chat message response"""

    id: str
    role: str  # 'user' or 'assistant'
    content: str
    message_data: dict[str, Any] | list[dict[str, Any]] | None = None  # PydanticAI message(s)
    created_at: datetime


class ChatSessionWithMessagesResponse(BaseModel):
    """Chat session with full message history"""

    id: str
    tenant_id: str
    user_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessageResponse]


class AddMessageRequest(BaseModel):
    """Request to add a message to a session"""

    role: str  # 'user' or 'assistant'
    content: str
    message_data: dict[str, Any] | list[dict[str, Any]] | None = None  # PydanticAI message(s)

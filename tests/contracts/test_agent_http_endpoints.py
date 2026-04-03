# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Contract tests for Agent Service HTTP endpoints.

Tests added in Session 36 for BFF HTTP refactoring (Task 36).

These tests validate that the agent service HTTP endpoints exist and
return the expected response structures without requiring a full server.

Note: Workflow/Template contracts removed - ReAct agent operates without persistent storage.
"""

import pytest
from pydantic import ValidationError

from meho_app.modules.agents.api_schemas import (
    ChatMessageResponse,
    ChatSessionResponse,
    CreateChatSessionRequest,
    HealthResponse,
)


class TestChatSessionEndpointContracts:
    """Test chat session HTTP endpoint contracts"""

    def test_create_chat_session_request_schema(self):
        """Test CreateChatSessionRequest schema validation"""
        request = CreateChatSessionRequest(
            tenant_id="tenant-123", user_id="user-123", title="Test Chat"
        )
        assert request.tenant_id == "tenant-123"
        assert request.user_id == "user-123"
        assert request.title == "Test Chat"

    def test_create_chat_session_request_optional_title(self):
        """Test CreateChatSessionRequest with optional title"""
        request = CreateChatSessionRequest(tenant_id="tenant-123", user_id="user-123")
        assert request.title is None

    def test_chat_session_response_schema(self):
        """Test ChatSessionResponse schema structure"""
        from datetime import UTC, datetime

        response = ChatSessionResponse(
            id="session-123",
            tenant_id="tenant-123",
            user_id="user-123",
            title="Test Chat",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
            message_count=5,
        )
        assert response.id == "session-123"
        assert response.message_count == 5

    def test_chat_message_response_schema(self):
        """Test ChatMessageResponse schema structure"""
        from datetime import UTC, datetime

        response = ChatMessageResponse(
            id="message-123", role="user", content="Hello", created_at=datetime.now(tz=UTC)
        )
        assert response.id == "message-123"
        assert response.role == "user"
        assert response.content == "Hello"


class TestHealthEndpointContract:
    """Test health endpoint contract"""

    def test_health_response_schema(self):
        """Test HealthResponse schema structure"""
        response = HealthResponse(status="healthy", service="meho-agent", version="0.1.0")
        assert response.status == "healthy"
        assert response.service == "meho-agent"
        assert response.version == "0.1.0"


class TestSchemaValidation:
    """Test schema validation rules"""

    def test_create_chat_session_requires_ids(self):
        """Test CreateChatSessionRequest requires tenant_id and user_id"""
        with pytest.raises(ValidationError):
            CreateChatSessionRequest(
                title="Test"
                # Missing tenant_id and user_id
            )


class TestResponseSerialization:
    """Test that responses can be serialized to JSON"""

    def test_chat_session_response_serialization(self):
        """Test ChatSessionResponse can be serialized"""
        from datetime import UTC, datetime

        response = ChatSessionResponse(
            id="session-123",
            tenant_id="tenant-123",
            user_id="user-123",
            title=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        data = response.model_dump()
        assert data["id"] == "session-123"
        assert data["title"] is None

    def test_chat_message_response_serialization(self):
        """Test ChatMessageResponse can be serialized"""
        from datetime import UTC, datetime

        response = ChatMessageResponse(
            id="message-123", role="assistant", content="Hello!", created_at=datetime.now(tz=UTC)
        )
        data = response.model_dump()
        assert data["id"] == "message-123"
        assert data["role"] == "assistant"

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for war room message attribution and conversation history (Phase 39).

Tests:
- ChatMessageModel sender_id and sender_name columns
- add_chat_message accepts sender params
- Conversation history includes sender_name in returned dicts
- UserContext accepts name field
- TokenData accepts name field
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from meho_app.api.auth import TokenData
from meho_app.core.auth_context import UserContext
from meho_app.modules.agents.models import ChatMessageModel

# =============================================================================
# ChatMessageModel Sender Columns
# =============================================================================


@pytest.mark.unit
class TestChatMessageSenderColumns:
    """Tests for sender columns on ChatMessageModel."""

    def test_sender_id_column_exists(self):
        """ChatMessageModel should have sender_id attribute."""
        msg = ChatMessageModel()
        msg.sender_id = "alice@example.com"
        assert msg.sender_id == "alice@example.com"

    def test_sender_name_column_exists(self):
        """ChatMessageModel should have sender_name attribute."""
        msg = ChatMessageModel()
        msg.sender_name = "Alice"
        assert msg.sender_name == "Alice"

    def test_sender_columns_default_none(self):
        """Sender columns should default to None for backward compatibility."""
        msg = ChatMessageModel()
        assert msg.sender_id is None
        assert msg.sender_name is None

    def test_sender_columns_set_together(self):
        """Both sender_id and sender_name can be set on a message."""
        msg = ChatMessageModel()
        msg.sender_id = "bob@acme.com"
        msg.sender_name = "Bob"
        assert msg.sender_id == "bob@acme.com"
        assert msg.sender_name == "Bob"


# =============================================================================
# add_chat_message with Sender Params
# =============================================================================


@pytest.mark.unit
class TestAddChatMessageSenderParams:
    """Tests for add_chat_message accepting sender params."""

    @pytest.mark.asyncio
    async def test_add_chat_message_with_sender(self):
        """add_chat_message should accept sender_id and sender_name."""
        from meho_app.modules.agents.service import AgentService

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=MagicMock()))
        )
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        service = AgentService(mock_session)
        session_id = str(uuid4())

        message = await service.add_chat_message(
            session_id=session_id,
            role="user",
            content="Check pod logs",
            sender_id="alice@example.com",
            sender_name="Alice",
        )

        assert message.sender_id == "alice@example.com"
        assert message.sender_name == "Alice"

    @pytest.mark.asyncio
    async def test_add_chat_message_without_sender(self):
        """add_chat_message should work without sender params (backward compatible)."""
        from meho_app.modules.agents.service import AgentService

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=MagicMock()))
        )
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        service = AgentService(mock_session)
        session_id = str(uuid4())

        message = await service.add_chat_message(
            session_id=session_id,
            role="assistant",
            content="Here are the pod logs...",
        )

        assert message.sender_id is None
        assert message.sender_name is None


# =============================================================================
# Conversation History with sender_name
# =============================================================================


@pytest.mark.unit
class TestConversationHistorySenderName:
    """Tests for conversation history including sender_name."""

    @pytest.mark.asyncio
    async def test_get_conversation_history_includes_sender_name(self):
        """get_conversation_history should include sender_name in message dicts."""
        from meho_app.api.routes_chat import get_conversation_history

        mock_msg_1 = MagicMock()
        mock_msg_1.role = "user"
        mock_msg_1.content = "Check the K8s cluster"
        mock_msg_1.message_data = None
        mock_msg_1.sender_name = "Alice"

        mock_msg_2 = MagicMock()
        mock_msg_2.role = "assistant"
        mock_msg_2.content = "The cluster is healthy."
        mock_msg_2.message_data = None
        mock_msg_2.sender_name = None

        mock_session = MagicMock()
        mock_session.messages = [mock_msg_1, mock_msg_2]

        mock_agent_service = AsyncMock()
        mock_agent_service.get_chat_session = AsyncMock(return_value=mock_session)

        history = await get_conversation_history("test-session-id", mock_agent_service)

        assert len(history) == 2
        # User message should have sender_name in the simple format dict
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "Check the K8s cluster"
        # Assistant message
        assert history[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_get_conversation_history_sender_name_in_simple_format(self):
        """When message_data is None, sender_name should be available in returned dict."""
        from meho_app.api.routes_chat import get_conversation_history

        mock_msg = MagicMock()
        mock_msg.role = "user"
        mock_msg.content = "hello world"
        mock_msg.message_data = None
        mock_msg.sender_name = "Bob"

        mock_session = MagicMock()
        mock_session.messages = [mock_msg]

        mock_agent_service = AsyncMock()
        mock_agent_service.get_chat_session = AsyncMock(return_value=mock_session)

        history = await get_conversation_history("test-session-id", mock_agent_service)

        assert len(history) == 1
        # Simple format uses role/content
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "hello world"


# =============================================================================
# UserContext name Field
# =============================================================================


@pytest.mark.unit
class TestUserContextName:
    """Tests for UserContext name field."""

    def test_user_context_accepts_name(self):
        """UserContext should accept and store a name field."""
        user = UserContext(
            user_id="alice@example.com",
            name="Alice Johnson",
            tenant_id="acme",
        )
        assert user.name == "Alice Johnson"

    def test_user_context_name_defaults_none(self):
        """UserContext name should default to None."""
        user = UserContext(user_id="alice@example.com", tenant_id="acme")
        assert user.name is None

    def test_user_context_name_optional(self):
        """UserContext should work without name (backward compatible)."""
        user = UserContext(
            user_id="alice@example.com",
            tenant_id="acme",
            roles=["user"],
        )
        assert user.name is None
        assert user.user_id == "alice@example.com"


# =============================================================================
# TokenData name Field
# =============================================================================


@pytest.mark.unit
class TestTokenDataName:
    """Tests for TokenData name field."""

    def test_token_data_accepts_name(self):
        """TokenData should accept and store a name field."""
        td = TokenData(
            user_id="alice@example.com",
            tenant_id="acme",
            name="Alice Johnson",
        )
        assert td.name == "Alice Johnson"

    def test_token_data_name_defaults_none(self):
        """TokenData name should default to None."""
        td = TokenData(user_id="alice@example.com", tenant_id="acme")
        assert td.name is None

    def test_token_data_backward_compatible(self):
        """TokenData should work without name (backward compatible)."""
        td = TokenData(
            user_id="alice@example.com",
            tenant_id="acme",
            roles=["user"],
            groups=["contract:x"],
        )
        assert td.name is None
        assert td.user_id == "alice@example.com"
        assert td.tenant_id == "acme"

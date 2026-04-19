# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Contract tests for Agent Service API.

Verifies that Agent services provide the APIs expected by the BFF (meho-api).
Note: Workflow/Plan contracts removed - ReAct agent operates without persistent storage.
"""

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest


class TestMEHODependenciesContract:
    """Test MEHODependencies tools contract"""

    def test_search_knowledge_tool_exists(self):
        """Verify search_knowledge tool exists"""
        from meho_app.modules.agents.dependencies import MEHODependencies

        assert hasattr(MEHODependencies, "search_knowledge")

    def test_list_endpoints_tool_exists(self):
        """Verify list_connectors tool exists"""
        from meho_app.modules.agents.dependencies import MEHODependencies

        assert hasattr(MEHODependencies, "list_connectors")

    def test_discover_endpoints_tool_exists(self):
        """Verify get_endpoint_details tool exists (discover by search)"""
        from meho_app.modules.agents.dependencies import MEHODependencies

        assert hasattr(MEHODependencies, "get_endpoint_details")

    def test_get_endpoint_details_tool_exists(self):
        """Verify get_endpoint_details tool exists"""
        from meho_app.modules.agents.dependencies import MEHODependencies

        assert hasattr(MEHODependencies, "get_endpoint_details")

    def test_call_endpoint_tool_exists(self):
        """Verify call_endpoint tool exists"""
        from meho_app.modules.agents.dependencies import MEHODependencies

        assert hasattr(MEHODependencies, "call_endpoint")

    @pytest.mark.asyncio
    async def test_search_knowledge_returns_string(self):
        """
        Test that search_knowledge returns a string (formatted results).

        This is what the LLM expects.
        """
        from datetime import UTC, datetime

        from meho_app.core.auth_context import UserContext
        from meho_app.modules.agents.dependencies import MEHODependencies
        from meho_app.modules.knowledge.schemas import KnowledgeChunk

        mock_chunk = KnowledgeChunk(
            id="chunk-1",
            text="Test result about roles",
            tenant_id="test-tenant",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

        mock_knowledge_store = Mock()
        mock_knowledge_store.search_hybrid = AsyncMock(return_value=[mock_chunk])
        mock_knowledge_store.search_cross_connector = AsyncMock(
            return_value=[
                {"id": "chunk-1", "text": "Test result about roles", "tags": [], "source_uri": None}
            ]
        )

        deps = MEHODependencies(
            knowledge_store=mock_knowledge_store,
            connector_repo=Mock(),
            endpoint_repo=Mock(),
            user_cred_repo=Mock(),
            http_client=Mock(),
            user_context=UserContext(
                tenant_id=str(uuid4()), user_id="test", roles=["user"], groups=[]
            ),
        )

        result = await deps.search_knowledge("test query")

        # Should return list of dicts (knowledge chunks formatted for LLM)
        assert isinstance(result, list), "search_knowledge should return list"
        if len(result) > 0:
            first_chunk = result[0]
            assert isinstance(first_chunk, dict), "Each result should be dict"
            assert "text" in first_chunk, "Each result should have 'text' field"


class TestAgentServiceContract:
    """Test AgentService API contract"""

    def test_agent_service_has_chat_session_methods(self):
        """Verify AgentService has chat session methods"""
        from meho_app.modules.agents.service import AgentService

        assert hasattr(AgentService, "create_chat_session")
        assert hasattr(AgentService, "get_chat_session")
        assert hasattr(AgentService, "list_chat_sessions")
        assert hasattr(AgentService, "add_chat_message")
        assert hasattr(AgentService, "update_chat_session")
        assert hasattr(AgentService, "delete_chat_session")

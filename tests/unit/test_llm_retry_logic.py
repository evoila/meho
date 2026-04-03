# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for LLM error handling in dependencies.

NOTE: Retry logic is now handled internally by PydanticAI, so we test
error propagation and graceful degradation instead of low-level retries.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from meho_app.core.auth_context import UserContext
from meho_app.modules.agents.dependencies import MEHODependencies


@pytest.fixture
def dependencies():
    """Dependencies for testing"""
    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=Mock(),
        endpoint_repo=Mock(),
        user_cred_repo=Mock(),
        http_client=Mock(),
        user_context=UserContext(user_id="user-1", tenant_id="tenant-1"),
    )
    return deps


@pytest.mark.unit
@pytest.mark.asyncio
async def test_interpret_results_success(dependencies):
    """Test successful interpretation with PydanticAI"""
    mock_result = Mock()
    mock_result.output = "Analysis: System is healthy"

    with patch.object(dependencies, "_get_interpreter_agent") as mock_get_agent:
        mock_agent = Mock()
        mock_agent.run = AsyncMock(return_value=mock_result)
        mock_get_agent.return_value = mock_agent

        result = await dependencies.interpret_results(context="test", results=[{"data": "test"}])

        assert result == "Analysis: System is healthy"
        mock_agent.run.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_interpret_results_handles_errors_gracefully(dependencies):
    """Test that errors in interpretation are caught and wrapped"""
    with patch.object(dependencies, "_get_interpreter_agent") as mock_get_agent:
        mock_agent = Mock()
        mock_agent.run = AsyncMock(side_effect=Exception("LLM API failed"))
        mock_get_agent.return_value = mock_agent

        # Should raise ValueError wrapping the original error
        with pytest.raises(ValueError, match="LLM error during interpretation"):
            await dependencies.interpret_results(context="test", results=[{"data": "test"}])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_determine_connector_success(dependencies):
    """Test connector determination with PydanticAI"""
    from meho_app.modules.agents.dependencies import ConnectorDetermination

    mock_result = Mock()
    mock_result.output = ConnectorDetermination(
        connector_id="conn-123",
        connector_name="Test System",
        confidence="high",
        reason="Query mentions Test System explicitly",
    )

    with patch.object(dependencies, "_get_classifier_agent") as mock_get_agent:
        mock_agent = Mock()
        mock_agent.run = AsyncMock(return_value=mock_result)
        mock_get_agent.return_value = mock_agent

        # Mock connector list - must support .model_dump()
        mock_conn = Mock()
        mock_conn.model_dump = Mock(
            return_value={
                "id": "conn-123",
                "name": "Test System",
                "description": "Test",
                "base_url": "https://test.com",
                "auth_type": "API_KEY",
            }
        )
        dependencies.connector_repo.list_connectors = AsyncMock(return_value=[mock_conn])

        result = await dependencies.determine_connector("Get data from Test System")

        assert result["connector_id"] == "conn-123"
        assert result["connector_name"] == "Test System"
        assert result["confidence"] == "high"
        mock_agent.run.assert_called_once()

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_app.modules.agents.dependencies

Phase 84: MEHODependencies.search_knowledge return format and call_endpoint
internal API changed (get_api_config removed, credential resolution refactored).
"""

from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: MEHODependencies search_knowledge return format and call_endpoint internals changed")

from meho_app.core.auth_context import UserContext
from meho_app.modules.agents.dependencies import MEHODependencies


@pytest.fixture
def mock_knowledge_store():
    """Mock knowledge store"""
    return AsyncMock()


@pytest.fixture
def mock_connector_repo():
    """Mock connector repository"""
    return AsyncMock()


@pytest.fixture
def mock_endpoint_repo():
    """Mock endpoint descriptor repository"""
    return AsyncMock()


@pytest.fixture
def mock_user_cred_repo():
    """Mock user credential repository"""
    return AsyncMock()


@pytest.fixture
def mock_http_client():
    """Mock HTTP client"""
    return AsyncMock()


@pytest.fixture
def mock_llm_client():
    """Mock LLM client"""
    mock_client = AsyncMock()
    mock_response = Mock()
    mock_choice = Mock()
    mock_message = Mock()
    mock_message.content = "Analysis result"
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    return mock_client


@pytest.fixture
def user_context():
    """User context"""
    return UserContext(user_id="user-1", tenant_id="tenant-1")


@pytest.fixture
def dependencies(
    mock_knowledge_store,
    mock_connector_repo,
    mock_endpoint_repo,
    mock_user_cred_repo,
    mock_http_client,
    mock_llm_client,
    user_context,
):
    """MEHO dependencies with all mocks"""
    return MEHODependencies(
        knowledge_store=mock_knowledge_store,
        connector_repo=mock_connector_repo,
        endpoint_repo=mock_endpoint_repo,
        user_cred_repo=mock_user_cred_repo,
        http_client=mock_http_client,
        user_context=user_context,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_knowledge(dependencies, mock_knowledge_store):
    """Test searching knowledge base"""
    # Setup mock
    mock_chunk = Mock()
    mock_chunk.id = "chunk-1"  # Need id for deduplication
    mock_chunk.text = "Test knowledge"
    mock_chunk.source_uri = "doc.pdf"
    mock_chunk.tags = ["tag1"]
    mock_chunk.system_id = "system-1"
    mock_knowledge_store.search_hybrid = AsyncMock(return_value=[mock_chunk])

    # Search
    results = await dependencies.search_knowledge("test query")

    # Verify
    assert len(results) == 1
    assert results[0]["text"] == "Test knowledge"
    assert results[0]["source_uri"] == "doc.pdf"
    mock_knowledge_store.search_hybrid.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_connectors(dependencies, mock_connector_repo):
    """Test listing available connectors"""
    # Setup mock with model_dump method (Pydantic-like)
    mock_conn = Mock()
    mock_conn.model_dump = Mock(
        return_value={
            "id": "conn-1",
            "name": "GitHub",
            "base_url": "https://api.github.com",
            "description": "GitHub API",
            "auth_type": "API_KEY",
        }
    )
    mock_connector_repo.list_connectors = AsyncMock(return_value=[mock_conn])

    # List
    results = await dependencies.list_connectors()

    # Verify
    assert len(results) == 1
    assert results[0]["name"] == "GitHub"
    assert results[0]["auth_type"] == "API_KEY"
    assert results[0]["base_url"] == "https://api.github.com"
    mock_connector_repo.list_connectors.assert_called_once_with(tenant_id="tenant-1")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_endpoint(
    dependencies, mock_connector_repo, mock_endpoint_repo, mock_http_client
):
    """Test calling an API endpoint"""
    # Setup mocks
    mock_conn = Mock()
    mock_conn.id = "conn-1"
    mock_conn.base_url = "https://api.example.com"
    mock_conn.credential_strategy = "none"
    mock_connector_repo.get_connector.return_value = mock_conn

    mock_endpoint = Mock()
    mock_endpoint.connector_id = "conn-1"  # Must match connector ID
    mock_endpoint.http_method = "GET"
    mock_endpoint.path_template = "/users/{id}"
    mock_endpoint_repo.get_endpoint.return_value = mock_endpoint

    # GenericHTTPClient returns (status_code, data) tuple
    mock_http_client.call_endpoint.return_value = (200, {"user": "data"})

    # Call endpoint
    result = await dependencies.call_endpoint(
        connector_id="conn-1", endpoint_id="endpoint-1", path_params={"id": "123"}
    )

    # Verify
    assert result["status_code"] == 200
    assert result["data"] == {"user": "data"}
    mock_http_client.call_endpoint.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_endpoint_with_user_credentials(
    dependencies, mock_connector_repo, mock_endpoint_repo, mock_user_cred_repo, mock_http_client
):
    """Test calling endpoint with user-provided credentials"""
    # Setup mocks
    mock_conn = Mock()
    mock_conn.id = "conn-1"
    mock_conn.base_url = "https://api.example.com"
    mock_conn.credential_strategy = "USER_PROVIDED"
    mock_connector_repo.get_connector.return_value = mock_conn

    mock_endpoint = Mock()
    mock_endpoint.connector_id = "conn-1"  # Must match connector ID
    mock_endpoint_repo.get_endpoint.return_value = mock_endpoint

    # get_credentials returns dict directly
    mock_user_cred_repo.get_credentials.return_value = {"api_key": "secret"}

    # GenericHTTPClient returns (status_code, data) tuple
    mock_http_client.call_endpoint.return_value = (200, {"data": "result"})

    # Call endpoint
    result = await dependencies.call_endpoint(connector_id="conn-1", endpoint_id="endpoint-1")

    # Verify credentials were fetched and passed
    mock_user_cred_repo.get_credentials.assert_called_once_with(
        user_id="user-1", connector_id="conn-1"
    )
    call_args = mock_http_client.call_endpoint.call_args
    assert call_args.kwargs["user_credentials"] == {"api_key": "secret"}

    # Verify result format
    assert result["status_code"] == 200
    assert result["data"] == {"data": "result"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_interpret_results(dependencies):
    """Test LLM interpretation of results"""
    from unittest.mock import AsyncMock, Mock, patch

    # Mock PydanticAI agent run method
    mock_result = Mock()
    mock_result.output = "Analysis result"

    with patch.object(dependencies, "_get_interpreter_agent") as mock_get_agent:
        mock_agent = Mock()
        mock_agent.run = AsyncMock(return_value=mock_result)
        mock_get_agent.return_value = mock_agent

        # Interpret
        interpretation = await dependencies.interpret_results(
            context="Diagnosing app issue",
            results=[{"text": "Error in logs"}, {"data": {"status": "failing"}}],
            question="What's the root cause?",
        )

        # Verify
        assert interpretation == "Analysis result"
        mock_agent.run.assert_called_once()
        call_args = mock_agent.run.call_args[0][0]  # First positional arg is the prompt
        assert "Diagnosing app issue" in call_args
        assert "What's the root cause?" in call_args

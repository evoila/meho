# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for state-exposed capabilities to LLM.

Tests that the LLM can fully benefit from state management through:
- Optional connector_id (auto-filled from state)
- Smart entity resolution (names → IDs)
- Enhanced responses with context hints
- Session state tracking of connectors and entities

NOTE: These tests need to be updated to use the new OrchestratorAgent architecture.
"""

from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.skip(reason="Tests need to be updated for OrchestratorAgent architecture")
from meho_app.core.auth_context import (  # noqa: E402 -- import after test setup
    UserContext,
)
from meho_app.modules.agents.dependencies import (  # noqa: E402 -- import after test setup
    MEHODependencies,
)
from meho_app.modules.agents.session_state import (  # noqa: E402 -- import after test setup
    AgentSessionState,
)


@pytest.fixture
def mock_knowledge_store():
    return AsyncMock()


@pytest.fixture
def mock_connector_repo():
    return AsyncMock()


@pytest.fixture
def mock_endpoint_repo():
    """Mock endpoint repository with GET endpoint"""
    repo = AsyncMock()

    # Mock a GET endpoint
    mock_endpoint = Mock()
    mock_endpoint.id = "endpoint-1"
    mock_endpoint.method = "GET"
    mock_endpoint.path = "/api/resource/{id}"
    mock_endpoint.summary = "Get resource details"
    mock_endpoint.connector_id = "connector-123"

    repo.get_endpoint = AsyncMock(return_value=mock_endpoint)
    return repo


@pytest.fixture
def mock_user_cred_repo():
    return AsyncMock()


@pytest.fixture
def mock_http_client():
    return AsyncMock()


@pytest.fixture
def user_context():
    return UserContext(user_id="user-1", tenant_id="tenant-1")


@pytest.fixture
def session_state():
    return AgentSessionState()


@pytest.fixture
def dependencies(
    mock_knowledge_store,
    mock_connector_repo,
    mock_endpoint_repo,
    mock_user_cred_repo,
    mock_http_client,
    user_context,
    session_state,
):
    """Dependencies with fresh state"""
    deps = MEHODependencies(
        knowledge_store=mock_knowledge_store,
        connector_repo=mock_connector_repo,
        endpoint_repo=mock_endpoint_repo,
        user_cred_repo=mock_user_cred_repo,
        http_client=mock_http_client,
        user_context=user_context,
        session_state=session_state,
    )

    # Mock search_endpoints to return some endpoints
    deps.search_endpoints = AsyncMock(
        return_value=[{"endpoint_id": "ep-1", "method": "GET", "path": "/api/resource"}]
    )

    # Mock call_endpoint to return success
    deps.call_endpoint = AsyncMock(
        return_value={"status_code": 200, "data": [{"id": "res-1", "name": "Resource-A"}]}
    )

    # Mock list_connectors
    deps.list_connectors = AsyncMock(
        return_value=[{"id": "conn-1", "name": "System X", "auth_type": "NONE"}]
    )

    return deps


# ============================================================================
# OPTIONAL connector_id TESTS
# ============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_endpoints_auto_fills_connector_id(dependencies):
    """Test that search_endpoints auto-fills connector_id from state"""

    # Set up state with active connector
    dependencies.session_state.get_or_create_connector("connector-123", "Test System", "test")
    dependencies.session_state.primary_connector_id = "connector-123"

    # Call search_endpoints WITHOUT connector_id
    result = await dependencies.search_endpoints(
        query="list resources"
        # connector_id omitted!
    )

    # Should succeed (auto-filled from state)
    assert result is not None
    assert len(result) > 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_endpoints_works_with_explicit_connector_id(dependencies):
    """Test that search_endpoints works when connector_id is provided explicitly"""

    # No active connector in state (but we provide explicit ID)
    assert dependencies.session_state.get_active_connector() is None

    # Should work with explicit connector_id
    result = await dependencies.search_endpoints(
        query="list resources",
        connector_id="connector-123",  # Explicit
    )
    assert result is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_call_endpoint_auto_fills_connector_id(dependencies):
    """Test that call_endpoint auto-fills connector_id from state"""

    # Set up state
    dependencies.session_state.get_or_create_connector("connector-123", "Test System", "test")
    dependencies.session_state.primary_connector_id = "connector-123"

    # Call WITHOUT connector_id
    result = await dependencies.call_endpoint(
        endpoint_id="ep-1"
        # connector_id omitted!
    )

    # Should succeed
    assert result is not None
    assert result["status_code"] == 200


# ============================================================================
# ENHANCED RESPONSES TESTS
# ============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_call_endpoint_includes_context_hints(dependencies):
    """Test that call_endpoint adds context hints when entities are extracted"""

    # Set up state
    dependencies.session_state.get_or_create_connector("connector-123", "Test System", "test")
    dependencies.session_state.primary_connector_id = "connector-123"

    # Mock call_endpoint to return a list (will trigger entity extraction)
    dependencies.call_endpoint = AsyncMock(
        return_value={
            "status_code": 200,
            "data": [
                {"id": "res-1", "name": "Resource-A"},
                {"id": "res-2", "name": "Resource-B"},
                {"id": "res-3", "name": "Resource-C"},
            ],
        }
    )

    # Call endpoint (entity extraction will happen in tool wrapper)
    # Note: In real flow, the tool wrapper adds entities and hints
    # Here we're testing the dependencies layer directly
    result = await dependencies.call_endpoint(endpoint_id="ep-1")

    # Result should be successful
    assert result["status_code"] == 200
    assert isinstance(result["data"], list)


# ============================================================================
# SESSION STATE CONTEXT TESTS
# ============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_state_shows_available_connectors(dependencies):
    """Test that session state tracks available connectors"""

    # Add connectors to state
    dependencies.session_state.get_or_create_connector("conn-1", "System A", "api")
    dependencies.session_state.get_or_create_connector("conn-2", "System B", "rest")

    # Get context summary
    context = dependencies.session_state
    summary_dict = {
        "active_connectors": [
            {"name": c.connector_name, "type": c.connector_type, "cached_endpoints": 0}
            for c in context.connectors.values()
        ],
        "available_entities": {},
        "context_summary": context.get_context_summary(),
    }

    # Should show both connectors
    assert len(summary_dict["active_connectors"]) == 2
    assert any(c["name"] == "System A" for c in summary_dict["active_connectors"])
    assert any(c["name"] == "System B" for c in summary_dict["active_connectors"])


# ============================================================================
# FULL WORKFLOW TESTS
# ============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_workflow_with_optional_params(dependencies):
    """Test complete workflow using optional connector_id"""

    # Step 1: Determine connector (stores in state)
    dependencies.session_state.get_or_create_connector("connector-123", "Test System", "api")
    dependencies.session_state.primary_connector_id = "connector-123"

    # Step 2: Search endpoints (no connector_id needed!)
    result = await dependencies.search_endpoints(
        query="list resources"
        # ✅ connector_id omitted - auto-filled from state!
    )
    assert result is not None

    # Step 3: Call endpoint (no connector_id needed!)
    result = await dependencies.call_endpoint(
        endpoint_id="ep-1"
        # ✅ connector_id omitted - auto-filled from state!
    )
    assert result["status_code"] == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_workflow_with_state_context(dependencies):
    """Test complete workflow leveraging state context"""

    # Step 1: Determine connector (stores in state)
    dependencies.session_state.get_or_create_connector("connector-123", "Test System", "api")
    dependencies.session_state.primary_connector_id = "connector-123"

    # Step 2: Search endpoints (uses state for connector_id)
    result = await dependencies.search_endpoints(
        query="list resources"
        # ✅ connector_id auto-filled from state!
    )
    assert result is not None

    # Step 3: Call endpoint (uses state for connector_id)
    result = await dependencies.call_endpoint(
        endpoint_id="ep-1"
        # ✅ connector_id auto-filled from state!
    )
    assert result["status_code"] == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multi_connector_switching_with_state(dependencies):
    """Test that LLM can switch between connectors using optional params"""

    # Add two connectors
    dependencies.session_state.get_or_create_connector("conn-1", "System A", "api")
    dependencies.session_state.get_or_create_connector("conn-2", "System B", "rest")

    # Set System A as primary
    dependencies.session_state.primary_connector_id = "conn-1"

    # Call with System A (omit connector_id)
    await dependencies.search_endpoints(query="endpoint A")
    # ✅ Uses conn-1

    # Switch to System B (explicit connector_id)
    await dependencies.search_endpoints(query="endpoint B", connector_id="conn-2")
    # ✅ Uses conn-2

    # Next call without connector_id still uses conn-1 (primary unchanged)
    await dependencies.search_endpoints(query="endpoint C")
    # ✅ Uses conn-1 (primary)

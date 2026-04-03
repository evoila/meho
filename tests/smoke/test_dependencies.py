# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Smoke test: Verify dependencies can be instantiated.

Tests that key dependency classes can be created (even with mocks).
This catches initialization errors early.
"""

from unittest.mock import Mock
from uuid import uuid4


def test_meho_dependencies_structure():
    """Test that MEHODependencies has expected structure"""
    from meho_app.core.auth_context import UserContext
    from meho_app.modules.agents.dependencies import MEHODependencies

    # Create mock dependencies
    mock_knowledge_store = Mock()
    mock_connector_repo = Mock()
    mock_endpoint_repo = Mock()
    mock_user_cred_repo = Mock()
    mock_http_client = Mock()
    Mock()

    # Create user context
    user_context = UserContext(
        tenant_id=str(uuid4()), user_id="test-user", roles=["user"], groups=[]
    )

    # Should be able to create MEHODependencies
    deps = MEHODependencies(
        knowledge_store=mock_knowledge_store,
        connector_repo=mock_connector_repo,
        endpoint_repo=mock_endpoint_repo,
        user_cred_repo=mock_user_cred_repo,
        http_client=mock_http_client,
        user_context=user_context,
    )

    # Verify it has expected methods
    assert hasattr(deps, "search_knowledge")
    assert hasattr(deps, "get_endpoint_details")
    assert hasattr(deps, "list_connectors")
    assert hasattr(deps, "call_endpoint")


def test_knowledge_store_interface():
    """Test that KnowledgeStore has expected interface"""
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

    # Check that class has expected methods (don't instantiate, just check structure)
    assert hasattr(KnowledgeStore, "search")
    assert hasattr(KnowledgeStore, "search_hybrid")


def test_agent_dependencies_has_tools():
    """Test that MEHODependencies has all required tools"""
    from meho_app.modules.agents.dependencies import MEHODependencies

    # These are the tools that agents use
    required_tools = [
        "search_knowledge",
        "list_connectors",
        "get_endpoint_details",
        "call_endpoint",
        "interpret_results",
    ]

    for tool in required_tools:
        assert hasattr(MEHODependencies, tool), f"MEHODependencies missing tool: {tool}"


def test_user_context_structure():
    """Test that UserContext has expected fields"""
    from meho_app.core.auth_context import UserContext

    tenant_id = str(uuid4())
    user_context = UserContext(
        tenant_id=tenant_id, user_id="test-user", roles=["admin"], groups=["engineering"]
    )

    assert isinstance(user_context.tenant_id, str)
    assert isinstance(user_context.user_id, str)
    assert isinstance(user_context.roles, list)
    assert isinstance(user_context.groups, list)


def test_http_client_interface():
    """Test that GenericHTTPClient has expected interface"""
    from meho_app.modules.connectors.rest.http_client import GenericHTTPClient

    assert hasattr(GenericHTTPClient, "call_endpoint")
    assert hasattr(GenericHTTPClient, "__init__")


def test_schemas_are_importable():
    """Test that Pydantic schemas can be imported"""
    # Note: Plan/PlanStep/Workflow removed - ReAct agent operates without persistent plan storage
    # Recipe schemas are in meho_app/modules/agent/recipes/models.py
    from meho_app.modules.agents.recipes.models import Recipe, RecipeParameter
    from meho_app.modules.knowledge.schemas import KnowledgeChunk

    # Just verify they're classes
    assert isinstance(KnowledgeChunk, type)
    assert isinstance(Recipe, type)
    assert isinstance(RecipeParameter, type)

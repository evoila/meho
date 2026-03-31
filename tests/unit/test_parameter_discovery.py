# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for parameter discovery and schema awareness (Task 16b).

These tests validate that the planner can discover endpoint parameter
requirements before calling APIs.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from meho_app.core.auth_context import UserContext
from meho_app.modules.agents.dependencies import MEHODependencies


@pytest.fixture
def mock_endpoint_with_schemas():
    """Mock endpoint with full parameter schemas"""
    endpoint = Mock()
    endpoint.id = "endpoint-123"
    endpoint.method = "GET"
    endpoint.path = "/api/v1/namespaces/{namespace}/pods"
    endpoint.summary = "List pods in namespace"
    endpoint.required_params = ["namespace"]
    endpoint.path_params_schema = {
        "namespace": {
            "type": "string",
            "description": "Namespace name",
            "required": True,
            "example": "production",
        }
    }
    endpoint.query_params_schema = {
        "labelSelector": {
            "type": "string",
            "description": "Filter pods by labels",
            "required": False,
            "example": "app=my-app",
        },
        "fieldSelector": {
            "type": "string",
            "description": "Filter pods by fields",
            "required": False,
            "example": "status.phase!=Running",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of results",
            "required": False,
            "example": 50,
        },
    }
    return endpoint


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_endpoint_details_returns_parameter_info(mock_endpoint_with_schemas):
    """Test that get_endpoint_details returns full parameter information"""
    # Mock connector for security check
    mock_connector = Mock()
    mock_connector.id = "k8s-connector"
    mock_connector.tenant_id = "company"  # For security check

    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=AsyncMock(),
        endpoint_repo=AsyncMock(),
        user_cred_repo=Mock(),
        http_client=Mock(),
        user_context=UserContext(user_id="user", tenant_id="company"),
    )

    # Mock connector retrieval (security check)
    deps.connector_repo.get_connector = AsyncMock(return_value=mock_connector)

    # Mock endpoint repo to return our detailed endpoint
    deps.endpoint_repo.list_endpoints = AsyncMock(return_value=[mock_endpoint_with_schemas])

    # Call new tool
    details = await deps.get_endpoint_details(
        connector_id="k8s-connector", search_query="list pods"
    )

    # ASSERTIONS: Should return rich parameter info
    assert len(details) > 0
    endpoint_info = details[0]

    assert "required_params" in endpoint_info
    assert "namespace" in endpoint_info["required_params"]

    assert "optional_params" in endpoint_info
    assert "labelSelector" in endpoint_info["optional_params"]
    assert "fieldSelector" in endpoint_info["optional_params"]

    # Should include examples if present
    assert "usage_example" in endpoint_info
    if isinstance(endpoint_info["usage_example"], dict):  # noqa: SIM102 -- readability preferred over collapse
        # Only check if it's a proper dict (not a Mock)
        if "path_params" in endpoint_info["usage_example"]:
            assert endpoint_info["usage_example"]["path_params"]["namespace"] == "production"


@pytest.mark.unit
def test_planner_prompt_includes_parameter_guidance():
    """Test that streaming agent system prompt instructs use of search_operations"""
    # Dead test: streaming_agent module was removed during architecture simplification (Phase 22).
    # STREAMING_AGENT_PROMPT no longer exists in agents/.
    pytest.skip("streaming_agent module removed during architecture simplification")

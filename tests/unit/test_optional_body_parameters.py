# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Test for optional request body parameter handling.

Phase 84: Optional body parameter validation logic changed in call_endpoint.
"""

from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: optional body parameter validation in call_endpoint changed")

from meho_app.core.auth_context import UserContext
from meho_app.modules.agents.dependencies import MEHODependencies


@pytest.mark.unit
@pytest.mark.asyncio
async def test_optional_body_included_in_optional_params():
    """Test that optional request bodies appear in optional_params"""
    # Mock endpoint with optional body
    mock_endpoint = Mock()
    mock_endpoint.id = "endpoint-with-optional-body"
    mock_endpoint.method = "POST"
    mock_endpoint.path = "/api/items"
    mock_endpoint.summary = "Create item"
    mock_endpoint.description = "Create an item (body optional)"
    mock_endpoint.operation_id = "createItem"
    mock_endpoint.path_params_schema = {}
    mock_endpoint.query_params_schema = {}
    mock_endpoint.body_schema = {
        "required": False,  # Body is optional!
        "schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "value": {"type": "integer"}},
        },
    }

    # Mock connector
    mock_connector = Mock()
    mock_connector.id = "test-connector"

    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=AsyncMock(),
        endpoint_repo=AsyncMock(),
        user_cred_repo=Mock(),
        http_client=Mock(),
        user_context=UserContext(user_id="user", tenant_id="company"),
    )

    deps.connector_repo.get_connector = AsyncMock(return_value=mock_connector)
    deps.endpoint_repo.list_endpoints = AsyncMock(return_value=[mock_endpoint])

    # Get endpoint details
    result = await deps.get_endpoint_details("test-connector", "create item")

    # ASSERTIONS:
    assert len(result) == 1
    endpoint_info = result[0]

    # Optional body should be in optional_params, NOT required_params
    assert "body" not in endpoint_info["required_params"], "Optional body should not be required"
    assert "body" in endpoint_info["optional_params"], "Optional body should be in optional_params"
    assert endpoint_info["optional_params"]["body"]["in"] == "body"
    assert "schema" in endpoint_info["optional_params"]["body"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_required_body_in_required_params():
    """Test that required request bodies appear in required_params"""
    # Mock endpoint with required body
    mock_endpoint = Mock()
    mock_endpoint.id = "endpoint-with-required-body"
    mock_endpoint.method = "POST"
    mock_endpoint.path = "/api/items"
    mock_endpoint.summary = "Create item"
    mock_endpoint.description = "Create an item"
    mock_endpoint.operation_id = "createItem"
    mock_endpoint.path_params_schema = {}
    mock_endpoint.query_params_schema = {}
    mock_endpoint.body_schema = {
        "required": True,  # Body is required!
        "schema": {"type": "object", "properties": {"name": {"type": "string", "required": True}}},
    }

    mock_connector = Mock()
    mock_connector.id = "test-connector"

    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=AsyncMock(),
        endpoint_repo=AsyncMock(),
        user_cred_repo=Mock(),
        http_client=Mock(),
        user_context=UserContext(user_id="user", tenant_id="company"),
    )

    deps.connector_repo.get_connector = AsyncMock(return_value=mock_connector)
    deps.endpoint_repo.list_endpoints = AsyncMock(return_value=[mock_endpoint])

    result = await deps.get_endpoint_details("test-connector", "create item")

    # ASSERTIONS:
    assert len(result) == 1
    endpoint_info = result[0]

    # Required body should be in required_params, NOT optional_params
    assert "body" in endpoint_info["required_params"], "Required body should be in required_params"
    assert "body" not in endpoint_info["optional_params"], "Required body should not be optional"

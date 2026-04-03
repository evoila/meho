# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Security tests for get_endpoint_details tool.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from meho_app.core.auth_context import UserContext
from meho_app.modules.agents.dependencies import MEHODependencies


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_endpoint_details_enforces_tenant_isolation():
    """Test that get_endpoint_details verifies connector belongs to tenant"""
    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=AsyncMock(),
        endpoint_repo=AsyncMock(),
        user_cred_repo=Mock(),
        http_client=Mock(),
        user_context=UserContext(user_id="user-1", tenant_id="tenant-1"),
    )

    # Mock connector_repo to return None (connector doesn't belong to tenant)
    deps.connector_repo.get_connector = AsyncMock(return_value=None)

    # Try to access connector from another tenant
    with pytest.raises(ValueError, match="Connector .* not found"):  # noqa: RUF043 -- test uses broad pattern intentionally
        await deps.get_endpoint_details(
            connector_id="other-tenant-connector", search_query="list endpoints"
        )

    # CRITICAL: Verify tenant_id was passed to enforce isolation
    deps.connector_repo.get_connector.assert_called_once_with(
        "other-tenant-connector", tenant_id="tenant-1"
    )

    # Verify endpoint listing was NOT called (security check failed first)
    deps.endpoint_repo.list_endpoints.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_endpoint_details_succeeds_for_own_tenant():
    """Test that get_endpoint_details works for connectors in user's tenant"""
    mock_connector = Mock()
    mock_connector.id = "my-connector"

    mock_endpoint = Mock()
    mock_endpoint.id = "endpoint-1"
    mock_endpoint.method = "GET"
    mock_endpoint.path = "/api/test"
    mock_endpoint.summary = "Test endpoint"
    mock_endpoint.description = ""
    mock_endpoint.operation_id = None
    mock_endpoint.path_params_schema = {}
    mock_endpoint.query_params_schema = {}
    mock_endpoint.body_schema = None

    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=AsyncMock(),
        endpoint_repo=AsyncMock(),
        user_cred_repo=Mock(),
        http_client=Mock(),
        user_context=UserContext(user_id="user-1", tenant_id="tenant-1"),
    )

    # Mock connector belongs to tenant
    deps.connector_repo.get_connector = AsyncMock(return_value=mock_connector)
    deps.endpoint_repo.list_endpoints = AsyncMock(return_value=[mock_endpoint])

    # Should succeed
    result = await deps.get_endpoint_details(connector_id="my-connector", search_query="test")

    # Should return endpoint details
    assert len(result) == 1
    assert result[0]["endpoint_id"] == "endpoint-1"

    # Verify security check was performed
    deps.connector_repo.get_connector.assert_called_once_with("my-connector", tenant_id="tenant-1")

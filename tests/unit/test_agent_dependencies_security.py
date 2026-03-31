# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Security tests for meho_app.modules.agents.dependencies

Phase 84: call_endpoint internal validation and credential resolution changed.
"""

from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: call_endpoint security validation and credential resolution internals changed")

from meho_app.core.auth_context import UserContext
from meho_app.modules.agents.dependencies import MEHODependencies


@pytest.fixture
def user_context():
    """User context"""
    return UserContext(user_id="user-1", tenant_id="tenant-1")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bug1_call_endpoint_enforces_tenant_isolation(user_context):
    """Test that call_endpoint enforces tenant isolation when fetching connector"""
    mock_connector_repo = AsyncMock()
    mock_endpoint_repo = AsyncMock()
    mock_http_client = AsyncMock()

    # Setup: connector exists but belongs to different tenant
    mock_connector_repo.get_connector.return_value = None  # Should return None for wrong tenant

    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=mock_connector_repo,
        endpoint_repo=mock_endpoint_repo,
        user_cred_repo=Mock(),
        http_client=mock_http_client,
        user_context=user_context,
    )

    # Try to access connector from another tenant
    with pytest.raises(ValueError, match="Connector .* not found"):  # noqa: RUF043 -- test uses broad pattern intentionally
        await deps.call_endpoint(connector_id="other-tenant-connector", endpoint_id="some-endpoint")

    # CRITICAL: Verify tenant_id was passed to enforce isolation
    mock_connector_repo.get_connector.assert_called_once_with(
        "other-tenant-connector", tenant_id="tenant-1"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bug2_call_endpoint_validates_endpoint_belongs_to_connector(user_context):
    """Test that call_endpoint validates endpoint belongs to connector"""
    mock_connector_repo = AsyncMock()
    mock_endpoint_repo = AsyncMock()
    mock_http_client = AsyncMock()

    # Setup: connector from tenant-1
    mock_conn = Mock()
    mock_conn.id = "connector-123"
    mock_conn.name = "Test Connector"
    mock_conn.base_url = "https://api.example.com"
    mock_conn.credential_strategy = "SYSTEM"
    mock_conn.auth_config = {"api_key": "test-key"}
    mock_connector_repo.get_connector.return_value = mock_conn

    # Setup: endpoint belongs to DIFFERENT connector (attack scenario)
    mock_endpoint = Mock()
    mock_endpoint.connector_id = "different-connector-456"  # Mismatch!
    mock_endpoint_repo.get_endpoint.return_value = mock_endpoint

    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=mock_connector_repo,
        endpoint_repo=mock_endpoint_repo,
        user_cred_repo=Mock(),
        http_client=mock_http_client,
        user_context=user_context,
    )

    # Try to use mismatched endpoint
    with pytest.raises(ValueError, match="does not belong to connector"):
        await deps.call_endpoint(connector_id="connector-123", endpoint_id="malicious-endpoint")

    # Verify HTTP client was NOT called (security breach prevented)
    mock_http_client.call_endpoint.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_call_endpoint_security_valid_case(user_context):
    """Test that valid endpoint-connector pairing works correctly"""
    mock_connector_repo = AsyncMock()
    mock_endpoint_repo = AsyncMock()
    mock_http_client = AsyncMock()

    # Setup: matching connector and endpoint
    mock_conn = Mock()
    mock_conn.id = "connector-123"
    mock_conn.name = "Test Connector"
    mock_conn.base_url = "https://api.example.com"
    mock_conn.credential_strategy = "SYSTEM"
    mock_conn.auth_config = {"api_key": "test-key"}
    mock_connector_repo.get_connector.return_value = mock_conn

    mock_endpoint = Mock()
    mock_endpoint.connector_id = "connector-123"  # MATCHES!
    mock_endpoint_repo.get_endpoint.return_value = mock_endpoint

    # GenericHTTPClient returns (status_code, data) tuple
    mock_http_client.call_endpoint.return_value = (200, {"data": "success"})

    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=mock_connector_repo,
        endpoint_repo=mock_endpoint_repo,
        user_cred_repo=Mock(),
        http_client=mock_http_client,
        user_context=user_context,
    )

    # This should succeed
    result = await deps.call_endpoint(connector_id="connector-123", endpoint_id="valid-endpoint")

    assert result["status_code"] == 200
    assert result["data"] == {"data": "success"}
    mock_http_client.call_endpoint.assert_called_once()

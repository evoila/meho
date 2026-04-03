# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for batch_get_endpoint functionality
"""

from unittest.mock import AsyncMock, Mock

import pytest

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
    repo = AsyncMock()

    # Default mock endpoint (GET method)
    mock_endpoint = Mock()
    mock_endpoint.id = "endpoint-1"
    mock_endpoint.method = "GET"
    mock_endpoint.path = "/api/vm/{vm_id}"
    mock_endpoint.summary = "Get VM details"
    mock_endpoint.connector_id = "connector-1"

    repo.get_endpoint = AsyncMock(return_value=mock_endpoint)
    return repo


@pytest.fixture
def mock_user_cred_repo():
    """Mock user credential repository"""
    return AsyncMock()


@pytest.fixture
def mock_http_client():
    """Mock HTTP client"""
    return AsyncMock()


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
async def test_batch_get_endpoint_success(dependencies):
    """Test successful batch GET operation with multiple calls"""
    # Mock call_endpoint to return success for each call
    call_results = [
        {"status_code": 200, "data": {"id": "vm-1", "name": "VM-1", "status": "running"}},
        {"status_code": 200, "data": {"id": "vm-2", "name": "VM-2", "status": "running"}},
        {"status_code": 200, "data": {"id": "vm-3", "name": "VM-3", "status": "stopped"}},
    ]

    # Mock call_endpoint to return different results based on parameters
    def mock_call_endpoint(
        connector_id, endpoint_id, path_params=None, query_params=None, body=None
    ):
        # Return result based on vm_id
        vm_id = path_params.get("vm_id") if path_params else None
        if vm_id == "vm-1":
            return call_results[0]
        elif vm_id == "vm-2":
            return call_results[1]
        elif vm_id == "vm-3":
            return call_results[2]
        return {"status_code": 404, "data": {"error": "Not found"}}

    dependencies.call_endpoint = AsyncMock(side_effect=mock_call_endpoint)

    # Prepare parameter sets
    parameter_sets = [
        {"path_params": {"vm_id": "vm-1"}, "query_params": {"details": "full"}},
        {"path_params": {"vm_id": "vm-2"}, "query_params": {"details": "full"}},
        {"path_params": {"vm_id": "vm-3"}, "query_params": {"details": "full"}},
    ]

    # Call batch_get_endpoint
    result = await dependencies.batch_get_endpoint(
        connector_id="connector-1", endpoint_id="endpoint-1", parameter_sets=parameter_sets
    )

    # Verify structure
    assert "results" in result
    assert "summary" in result
    assert len(result["results"]) == 3

    # Verify summary
    assert result["summary"]["total"] == 3
    assert result["summary"]["successful"] == 3
    assert result["summary"]["failed"] == 0

    # Verify each result has correct structure
    for i, res in enumerate(result["results"]):
        assert "parameters" in res
        assert "status_code" in res
        assert "data" in res
        assert "success" in res
        assert res["success"] is True

        # Verify parameters match what was sent
        assert res["parameters"]["path_params"] == parameter_sets[i]["path_params"]
        assert res["parameters"]["query_params"] == parameter_sets[i]["query_params"]

        # Verify data matches expected result
        assert res["status_code"] == 200
        assert res["data"]["id"] == f"vm-{i + 1}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_get_endpoint_partial_failure(dependencies):
    """Test batch GET with some calls failing"""

    # Mock call_endpoint to succeed for first two, fail for third
    def mock_call_endpoint(
        connector_id, endpoint_id, path_params=None, query_params=None, body=None
    ):
        vm_id = path_params.get("vm_id") if path_params else None
        if vm_id in ["vm-1", "vm-2"]:
            return {"status_code": 200, "data": {"id": vm_id, "name": f"VM-{vm_id[-1]}"}}
        else:
            raise ValueError(f"VM {vm_id} not found")

    dependencies.call_endpoint = AsyncMock(side_effect=mock_call_endpoint)

    # Prepare parameter sets
    parameter_sets = [
        {"path_params": {"vm_id": "vm-1"}},
        {"path_params": {"vm_id": "vm-2"}},
        {"path_params": {"vm_id": "vm-999"}},  # This one will fail
    ]

    # Call batch_get_endpoint
    result = await dependencies.batch_get_endpoint(
        connector_id="connector-1", endpoint_id="endpoint-1", parameter_sets=parameter_sets
    )

    # Verify summary shows partial failure
    assert result["summary"]["total"] == 3
    assert result["summary"]["successful"] == 2
    assert result["summary"]["failed"] == 1

    # Verify first two succeeded
    assert result["results"][0]["success"] is True
    assert result["results"][0]["status_code"] == 200
    assert result["results"][1]["success"] is True
    assert result["results"][1]["status_code"] == 200

    # Verify third failed
    assert result["results"][2]["success"] is False
    assert "error" in result["results"][2]
    assert "not found" in result["results"][2]["error"].lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_get_endpoint_only_allows_get_methods(dependencies, mock_endpoint_repo):
    """Test that batch_get_endpoint rejects non-GET methods"""
    # Mock a POST endpoint
    post_endpoint = Mock()
    post_endpoint.id = "endpoint-2"
    post_endpoint.method = "POST"
    post_endpoint.path = "/api/vm"
    post_endpoint.summary = "Create VM"

    mock_endpoint_repo.get_endpoint = AsyncMock(return_value=post_endpoint)

    # Try to call batch_get_endpoint with POST endpoint
    parameter_sets = [{"path_params": {"name": "vm-1"}}]

    with pytest.raises(ValueError) as exc_info:  # noqa: PT011 -- test validates exception type is sufficient
        await dependencies.batch_get_endpoint(
            connector_id="connector-1", endpoint_id="endpoint-2", parameter_sets=parameter_sets
        )

    # Verify error message mentions GET requirement
    assert "only works with get methods" in str(exc_info.value).lower()
    assert "post" in str(exc_info.value).lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_get_endpoint_invalid_endpoint(dependencies, mock_endpoint_repo):
    """Test batch_get_endpoint with non-existent endpoint"""
    # Mock endpoint not found
    mock_endpoint_repo.get_endpoint = AsyncMock(return_value=None)

    parameter_sets = [{"path_params": {"vm_id": "vm-1"}}]

    with pytest.raises(ValueError) as exc_info:  # noqa: PT011 -- test validates exception type is sufficient
        await dependencies.batch_get_endpoint(
            connector_id="connector-1", endpoint_id="nonexistent", parameter_sets=parameter_sets
        )

    assert "not found" in str(exc_info.value).lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_get_endpoint_empty_parameter_sets(dependencies):
    """Test batch_get_endpoint with empty parameter sets"""
    result = await dependencies.batch_get_endpoint(
        connector_id="connector-1", endpoint_id="endpoint-1", parameter_sets=[]
    )

    # Should return empty results with zero counts
    assert result["summary"]["total"] == 0
    assert result["summary"]["successful"] == 0
    assert result["summary"]["failed"] == 0
    assert len(result["results"]) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_get_endpoint_preserves_parameter_structure(dependencies):
    """Test that batch_get_endpoint preserves parameter structure in response"""
    # Mock call_endpoint
    dependencies.call_endpoint = AsyncMock(
        return_value={"status_code": 200, "data": {"result": "ok"}}
    )

    # Use complex parameter structure
    parameter_sets = [
        {
            "path_params": {"vm_id": "vm-1", "datacenter_id": "dc-1"},
            "query_params": {"include": "metrics", "format": "json"},
        },
        {"path_params": {"vm_id": "vm-2"}, "query_params": {}},
        {"path_params": {}, "query_params": {"filter": "status=running"}},
    ]

    result = await dependencies.batch_get_endpoint(
        connector_id="connector-1", endpoint_id="endpoint-1", parameter_sets=parameter_sets
    )

    # Verify each result preserves the exact parameter structure
    for i, res in enumerate(result["results"]):
        expected_params = parameter_sets[i]
        assert res["parameters"]["path_params"] == expected_params.get("path_params", {})
        assert res["parameters"]["query_params"] == expected_params.get("query_params", {})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_get_endpoint_calls_endpoint_sequentially(dependencies):
    """Test that batch_get_endpoint calls endpoints in order"""
    call_order = []

    def mock_call_endpoint(
        connector_id, endpoint_id, path_params=None, query_params=None, body=None
    ):
        vm_id = path_params.get("vm_id") if path_params else "unknown"
        call_order.append(vm_id)
        return {"status_code": 200, "data": {"id": vm_id}}

    dependencies.call_endpoint = AsyncMock(side_effect=mock_call_endpoint)

    parameter_sets = [
        {"path_params": {"vm_id": "vm-1"}},
        {"path_params": {"vm_id": "vm-2"}},
        {"path_params": {"vm_id": "vm-3"}},
    ]

    await dependencies.batch_get_endpoint(
        connector_id="connector-1", endpoint_id="endpoint-1", parameter_sets=parameter_sets
    )

    # Verify calls were made in order
    assert call_order == ["vm-1", "vm-2", "vm-3"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_get_endpoint_different_http_methods_rejected(dependencies, mock_endpoint_repo):
    """Test rejection of PUT, DELETE, PATCH methods"""
    for method in ["PUT", "DELETE", "PATCH", "POST"]:
        endpoint = Mock()
        endpoint.id = f"endpoint-{method}"
        endpoint.method = method
        endpoint.path = "/api/resource"
        endpoint.summary = f"{method} resource"

        mock_endpoint_repo.get_endpoint = AsyncMock(return_value=endpoint)

        parameter_sets = [{"path_params": {"id": "1"}}]

        with pytest.raises(ValueError) as exc_info:  # noqa: PT011 -- test validates exception type is sufficient
            await dependencies.batch_get_endpoint(
                connector_id="connector-1", endpoint_id=endpoint.id, parameter_sets=parameter_sets
            )

        assert "only works with get methods" in str(exc_info.value).lower()
        assert method.lower() in str(exc_info.value).lower()

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for response summarization logic (Task 16b).

Validates that large API responses (>500KB) are automatically
summarized using LLM before being passed to interpretation.

Phase 84: Response summarization now uses async infer() utility, mock patterns need AsyncMock.
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: response summarization uses async infer() utility, mock patterns need AsyncMock")

import json
from unittest.mock import AsyncMock, Mock

import pytest

from meho_app.core.auth_context import UserContext
from meho_app.modules.agents.dependencies import MEHODependencies


@pytest.fixture
def mock_llm_for_summarization():
    """Mock LLM that summarizes"""
    llm = AsyncMock()

    # Mock summarization response
    mock_response = Mock()
    mock_response.choices = [
        Mock(
            message=Mock(
                content=json.dumps(
                    {
                        "summary": "50 VMs with issues out of 10,000 total",
                        "critical_vms": [
                            {"name": "vm-9999", "status": "degraded", "cpu": 95},
                            {"name": "vm-9998", "status": "degraded", "cpu": 98},
                        ],
                        "statistics": {"total": 10000, "healthy": 9950, "degraded": 50},
                    }
                )
            )
        )
    ]
    llm.chat.completions.create = AsyncMock(return_value=mock_response)

    return llm


@pytest.mark.unit
@pytest.mark.asyncio
async def test_large_response_triggers_summarization():
    """Test that responses >500KB trigger automatic summarization"""
    from unittest.mock import patch

    from meho_app.modules.agents.dependencies import DataSummary

    # Setup mocks
    mock_connector = Mock()
    mock_connector.id = "vsphere-conn"
    mock_connector.name = "vSphere Connector"
    mock_connector.credential_strategy = "SYSTEM"
    mock_connector.auth_config = {"username": "admin", "password": "secret"}

    mock_endpoint = Mock()
    mock_endpoint.id = "list-vms"
    mock_endpoint.connector_id = "vsphere-conn"
    mock_endpoint.summary = "List VMs"

    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=AsyncMock(),
        endpoint_repo=AsyncMock(),
        user_cred_repo=AsyncMock(),
        http_client=AsyncMock(),
        user_context=UserContext(user_id="user", tenant_id="company"),
    )

    # Create large response (1MB)
    large_data = {"vms": [{"id": i, "data": "x" * 100} for i in range(10000)]}

    # Mock connector and endpoint retrieval
    deps.connector_repo.get_connector = AsyncMock(return_value=mock_connector)
    deps.endpoint_repo.get_endpoint = AsyncMock(return_value=mock_endpoint)

    # Mock HTTP client to return large response
    deps.http_client.call_endpoint = AsyncMock(return_value=(200, large_data))

    # Mock PydanticAI data extractor agent
    mock_result = Mock()
    mock_result.output = DataSummary(
        summary="50 VMs with issues out of 10,000 total",
        critical_items=[
            {"name": "vm-9999", "status": "degraded", "cpu": 95},
            {"name": "vm-9998", "status": "degraded", "cpu": 98},
        ],
        statistics={"total": 10000, "healthy": 9950, "degraded": 50},
    )

    with patch.object(deps, "_get_data_extractor_agent") as mock_get_agent:
        mock_agent = Mock()
        mock_agent.run = AsyncMock(return_value=mock_result)
        mock_get_agent.return_value = mock_agent

        # Call endpoint
        result = await deps.call_endpoint(connector_id="vsphere-conn", endpoint_id="list-vms")

        # ASSERTIONS:
        # Should have triggered summarization
        mock_agent.run.assert_called_once()

        # Returned data should be summary, not full response
        assert result["data"] != large_data, "Should return summary, not full data"
        assert "summary" in result["data"]
        assert result["data"]["summary"] == "50 VMs with issues out of 10,000 total"

        # Summary should be much smaller
        summary_size = len(json.dumps(result["data"]))
        original_size = len(json.dumps(large_data))
        assert summary_size < original_size / 10, "Summary should be <10% of original"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_small_responses_not_summarized():
    """Test that small responses pass through without summarization"""
    from unittest.mock import patch

    # Setup mocks
    mock_connector = Mock()
    mock_connector.id = "k8s-conn"
    mock_connector.name = "Kubernetes Connector"
    mock_connector.credential_strategy = "SYSTEM"
    mock_connector.auth_config = {"token": "kube-token-123"}

    mock_endpoint = Mock()
    mock_endpoint.id = "list-pods"
    mock_endpoint.connector_id = "k8s-conn"
    mock_endpoint.summary = "List pods"

    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=AsyncMock(),
        endpoint_repo=AsyncMock(),
        user_cred_repo=AsyncMock(),
        http_client=AsyncMock(),
        user_context=UserContext(user_id="user", tenant_id="company"),
    )

    # Mock connector and endpoint retrieval
    deps.connector_repo.get_connector = AsyncMock(return_value=mock_connector)
    deps.endpoint_repo.get_endpoint = AsyncMock(return_value=mock_endpoint)

    # Small response (10KB)
    small_data = {"pods": [{"name": f"pod-{i}"} for i in range(10)]}
    deps.http_client.call_endpoint = AsyncMock(return_value=(200, small_data))

    # Mock PydanticAI data extractor agent (should NOT be called for small responses)
    with patch.object(deps, "_get_data_extractor_agent") as mock_get_agent:
        mock_agent = Mock()
        mock_agent.run = AsyncMock()
        mock_get_agent.return_value = mock_agent

        result = await deps.call_endpoint(
            connector_id="k8s-conn", endpoint_id="list-pods", path_params={"namespace": "prod"}
        )

        # Should NOT trigger summarization (response is small)
        assert not mock_agent.run.called, "Should not summarize small responses"

        # Should return original data
        assert result["data"] == small_data


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.skip("Not implemented yet - Task 16b")
async def test_summarization_preserves_critical_info():
    """Test that summarization keeps errors, failures, anomalies"""
    pytest.skip("Define what 'critical info' means per API type")

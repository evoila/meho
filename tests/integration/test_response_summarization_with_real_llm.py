# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration test for response summarization with REAL LLM.

Proves that large response summarization actually works end-to-end.

WARNING: This test requires a valid OPENAI_API_KEY environment variable
and will make real API calls to OpenAI, which incurs costs (~$0.001 per run).
"""

import json
import os
from unittest.mock import AsyncMock, Mock

import pytest
from dotenv import load_dotenv

from meho_app.core.auth_context import UserContext
from meho_app.modules.agents.dependencies import MEHODependencies


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_llm_summarizes_large_vsphere_response():
    """Test that real LLM can summarize a large vSphere-like response"""
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY not set")

    from openai import AsyncOpenAI
    from pydantic_ai import UsageLimits

    # Create real dependencies with real LLM
    AsyncOpenAI(api_key=api_key)

    # Setup mocks for other services
    mock_connector = Mock()
    mock_connector.id = "vsphere-conn"
    mock_connector.credential_strategy = "SYSTEM"

    mock_endpoint = Mock()
    mock_endpoint.id = "list-vms"
    mock_endpoint.connector_id = "vsphere-conn"
    mock_endpoint.summary = "List all virtual machines"

    deps = MEHODependencies(
        knowledge_store=Mock(),
        connector_repo=AsyncMock(),
        endpoint_repo=AsyncMock(),
        user_cred_repo=AsyncMock(),
        http_client=AsyncMock(),
        user_context=UserContext(user_id="test-user", tenant_id="test-tenant"),
        usage_limits=UsageLimits(
            request_limit=99999, input_tokens_limit=99999999, output_tokens_limit=99999999
        ),
    )

    # Mock connector/endpoint retrieval
    deps.connector_repo.get_connector = AsyncMock(return_value=mock_connector)
    deps.endpoint_repo.get_endpoint = AsyncMock(return_value=mock_endpoint)

    # Create realistic large response: 1000 VMs with 50 unhealthy
    large_vm_data = {
        "vms": [
            {
                "id": f"vm-{i}",
                "name": f"prod-vm-{i}",
                "status": "running" if i < 950 else "degraded",
                "cpu": 45 if i < 950 else 95,
                "memory": 60 if i < 950 else 98,
                "network": "normal" if i < 950 else "degraded",
                # Pad to make realistic size
                "config": {"cpus": 4, "ram": 8192, "disk": 100},
                "metadata": {"dc": "DC1", "cluster": "prod"},
                "padding": "x" * 300,  # Each VM ~800 bytes
            }
            for i in range(1000)
        ]
    }

    data_size = len(json.dumps(large_vm_data))
    print(f"\nOriginal response: {data_size / 1024:.1f}KB ({len(large_vm_data['vms'])} VMs)")
    assert data_size > 500 * 1024, "Should be >500KB to trigger summarization"

    # Mock HTTP client to return large response
    deps.http_client.call_endpoint = AsyncMock(return_value=(200, large_vm_data))

    # Call endpoint - should trigger REAL LLM summarization
    result = await deps.call_endpoint(connector_id="vsphere-conn", endpoint_id="list-vms")

    # ASSERTIONS:
    # Should have summarized
    assert result["data"] != large_vm_data, "Should return summary, not original data"

    # Summary should be much smaller
    summary_size = len(json.dumps(result["data"]))
    print(f"Summarized response: {summary_size / 1024:.1f}KB")
    print(f"Compression ratio: {100 * (1 - summary_size / data_size):.1f}% reduction")

    assert summary_size < data_size / 5, "Summary should be <20% of original"

    # Summary should have key fields
    summary_str = str(result["data"]).lower()
    assert "summary" in summary_str or "statistics" in summary_str or "degraded" in summary_str

    print("\n=== LLM Summary ===")
    print(json.dumps(result["data"], indent=2)[:500])
    print(
        f"\n✅ Real LLM successfully summarized {data_size / 1024:.0f}KB → {summary_size / 1024:.0f}KB"
    )

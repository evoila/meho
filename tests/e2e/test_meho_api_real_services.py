# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Real end-to-end tests for MEHO API against running services.

REQUIREMENTS:
- PostgreSQL running (test database)
- Qdrant running (test instance)
- OpenAI API key set
- All services migrated

Run with:
    pytest tests/e2e/test_meho_api_real_services.py -v

Or use the test script:
    ./scripts/run-e2e-tests.sh
"""

import asyncio
import json
import os
import time
from datetime import UTC

import httpx
import pytest

# Setup test environment (bypass .env file issues)
from tests.support.test_config import setup_test_environment

setup_test_environment()

# Configuration
API_BASE_URL = os.getenv("MEHO_API_URL", "http://localhost:8000")
TEST_TIMEOUT = 180.0  # Longer timeout for real LLM calls with multiple tool invocations


@pytest.fixture
async def auth_headers():
    """Create authorization headers by requesting token from API"""
    async with httpx.AsyncClient(base_url=API_BASE_URL) as client:
        response = await client.post(
            "/api/auth/test-token",
            json={"user_id": "e2e-test@example.com", "tenant_id": "e2e-tenant", "roles": ["admin"]},
        )
        assert response.status_code == 200, f"Failed to get test token: {response.text}"
        token = response.json()["token"]
        return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def http_client():
    """Create async HTTP client for tests"""
    async with httpx.AsyncClient(timeout=TEST_TIMEOUT) as client:
        yield client


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_api_health_check(http_client):
    """Test that API is running and healthy"""
    response = await http_client.get(f"{API_BASE_URL}/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "meho-api"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_authentication_required(http_client):
    """Test that endpoints require authentication"""
    response = await http_client.post(f"{API_BASE_URL}/api/chat", json={"message": "test"})

    assert response.status_code == 403


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_create_workflow_real_planner(http_client, auth_headers):
    """
    Test creating workflow with REAL PlannerAgent.

    This calls the actual OpenAI API!
    """
    response = await http_client.post(
        f"{API_BASE_URL}/api/workflows",
        json={"goal": "E2E test: What systems are available?"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()

    # Verify workflow created
    assert "id" in data
    assert data["goal"] == "E2E test: What systems are available?"
    assert data["status"] == "WAITING_APPROVAL"
    assert "plan" in data

    # Verify plan structure from real LLM
    plan = data["plan"]
    assert "goal" in plan
    assert "steps" in plan
    assert len(plan["steps"]) > 0

    # Verify steps have required fields
    for step in plan["steps"]:
        assert "id" in step
        assert "description" in step
        assert "tool_name" in step
        assert "tool_args" in step

    return data["id"]  # Return workflow ID for other tests


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_get_workflow_from_database(http_client, auth_headers):
    """Test retrieving workflow from real database"""
    # First create a workflow
    create_response = await http_client.post(
        f"{API_BASE_URL}/api/workflows",
        json={"goal": "E2E test: Check database persistence"},
        headers=auth_headers,
    )
    workflow_id = create_response.json()["id"]

    # Now retrieve it
    get_response = await http_client.get(
        f"{API_BASE_URL}/api/workflows/{workflow_id}", headers=auth_headers
    )

    assert get_response.status_code == 200
    data = get_response.json()

    assert data["id"] == workflow_id
    assert data["goal"] == "E2E test: Check database persistence"
    assert "created_at" in data
    assert "updated_at" in data


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_list_workflows_from_database(http_client, auth_headers):
    """Test listing workflows filters by tenant correctly"""
    # Create 2 workflows
    for i in range(2):
        await http_client.post(
            f"{API_BASE_URL}/api/workflows",
            json={"goal": f"E2E test list: workflow {i}"},
            headers=auth_headers,
        )

    # List workflows
    response = await http_client.get(f"{API_BASE_URL}/api/workflows", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()

    assert "workflows" in data
    assert "count" in data
    assert data["count"] >= 2

    # All workflows should be for e2e-tenant
    for wf in data["workflows"]:
        assert wf["tenant_id"] == "e2e-tenant"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chat_non_streaming_real_execution(http_client, auth_headers):
    """
    Test non-streaming chat with real agent execution.

    WARNING: This will use OpenAI API credits!
    """
    response = await http_client.post(
        f"{API_BASE_URL}/api/chat",
        json={"message": "E2E test: Hello, what can you help me with?"},
        headers=auth_headers,
        timeout=60.0,  # Longer timeout for real execution
    )

    assert response.status_code == 200
    data = response.json()

    assert "response" in data
    assert "workflow_id" in data
    assert len(data["response"]) > 0


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_sse_streaming_real(auth_headers):
    """
    Test Server-Sent Events streaming with REAL agent.

    This is the critical test - verifies SSE actually works!
    """
    import httpx_sse

    events = []

    async with (
        httpx.AsyncClient(timeout=60.0) as client,
        httpx_sse.aconnect_sse(
            client,
            "POST",
            f"{API_BASE_URL}/api/chat/stream",
            json={"message": "E2E SSE test: Quick hello"},
            headers=auth_headers,
        ) as event_source,
    ):
        async for sse in event_source.aiter_sse():
            event_data = json.loads(sse.data)
            events.append(event_data)

            # Stop after we get a completion event
            if event_data.get("type") in ["done", "execution_complete"]:
                break

    # Verify we got events
    assert len(events) > 0

    # Verify event types we expect
    event_types = [e.get("type") for e in events]
    assert "thinking" in event_types or "planning_start" in event_types

    # Verify we got completion
    assert any(e.get("type") in ["done", "execution_complete"] for e in events)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_workflow_approval_and_execution(http_client, auth_headers):
    """
    Test complete workflow: create, approve, execute.

    Tests the full lifecycle with real agents.
    """
    # 1. Create workflow
    create_response = await http_client.post(
        f"{API_BASE_URL}/api/workflows",
        json={"goal": "E2E test: List available tools"},
        headers=auth_headers,
    )
    assert create_response.status_code == 200, f"Create workflow failed: {create_response.json()}"
    workflow_id = create_response.json()["id"]

    # 2. Verify in WAITING_APPROVAL
    get_response = await http_client.get(
        f"{API_BASE_URL}/api/workflows/{workflow_id}", headers=auth_headers
    )
    assert get_response.json()["status"] in ["WAITING_APPROVAL", "PLANNING"]

    # 3. Approve workflow
    approve_response = await http_client.post(
        f"{API_BASE_URL}/api/workflows/{workflow_id}/approve", headers=auth_headers
    )
    assert approve_response.status_code == 200
    assert approve_response.json()["status"] == "RUNNING"

    # 4. Wait a bit for execution to start
    await asyncio.sleep(2)

    # 5. Check status (should be RUNNING or COMPLETED)
    status_response = await http_client.get(
        f"{API_BASE_URL}/api/workflows/{workflow_id}", headers=auth_headers
    )
    status = status_response.json()["status"]
    assert status in ["RUNNING", "COMPLETED", "FAILED"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_concurrent_workflow_creation(http_client, auth_headers):
    """Test that multiple workflows can be created concurrently"""
    tasks = []

    for i in range(3):
        task = http_client.post(
            f"{API_BASE_URL}/api/workflows",
            json={"goal": f"E2E concurrent test {i}"},
            headers=auth_headers,
        )
        tasks.append(task)

    responses = await asyncio.gather(*tasks)

    # All should succeed
    assert all(r.status_code == 200 for r in responses)

    # All should have unique IDs
    workflow_ids = [r.json()["id"] for r in responses]
    assert len(workflow_ids) == len(set(workflow_ids))


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_knowledge_search_real_vector_store(http_client, auth_headers):
    """
    Test knowledge search against real Qdrant.

    Note: This requires Qdrant to be running and have data.
    """
    response = await http_client.post(
        f"{API_BASE_URL}/api/knowledge/search",
        json={"query": "E2E test search", "top_k": 5},
        headers=auth_headers,
    )

    # Should succeed even if no results
    assert response.status_code == 200
    # Response format depends on knowledge service implementation


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_api_handles_large_plan(http_client, auth_headers):
    """Test that API can handle plans with many steps"""
    response = await http_client.post(
        f"{API_BASE_URL}/api/workflows",
        json={
            "goal": "E2E test: Diagnose why my-app is down, check all systems, "
            "verify GitHub commits, check Kubernetes pods, check ArgoCD status, "
            "check vSphere VMs, analyze logs, and provide recommendations"
        },
        headers=auth_headers,
        timeout=60.0,  # Longer timeout for complex plan
    )

    assert response.status_code == 200, f"Large plan creation failed: {response.json()}"
    plan = response.json()["plan"]

    # Should create steps (or empty with notes explaining why)
    # We're flexible here since LLM might determine no action needed
    assert "steps" in plan


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_invalid_workflow_id_returns_404(http_client, auth_headers):
    """Test that invalid workflow ID returns proper error"""
    fake_id = "00000000-0000-0000-0000-000000000000"

    response = await http_client.get(
        f"{API_BASE_URL}/api/workflows/{fake_id}", headers=auth_headers
    )

    assert response.status_code == 404


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_expired_token_rejected():
    """Test that expired tokens are rejected"""
    from datetime import datetime, timedelta

    from jose import jwt

    from meho_app.api.config import MEHOAPIConfig

    config = MEHOAPIConfig()

    # Create expired token
    payload = {
        "sub": "test@example.com",
        "tenant_id": "test",
        "exp": datetime.now(UTC) - timedelta(hours=1),
    }
    expired_token = jwt.encode(payload, config.jwt_secret_key, algorithm=config.jwt_algorithm)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{API_BASE_URL}/api/chat",
            json={"message": "test"},
            headers={"Authorization": f"Bearer {expired_token}"},
        )

        assert response.status_code == 401


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_api_performance_baseline(http_client, auth_headers):
    """Measure baseline performance of workflow creation"""
    start = time.time()

    response = await http_client.post(
        f"{API_BASE_URL}/api/workflows", json={"goal": "E2E performance test"}, headers=auth_headers
    )

    elapsed = time.time() - start

    assert response.status_code == 200
    # Should complete within reasonable time (considering LLM call)
    assert elapsed < 30.0, f"Workflow creation took {elapsed}s, expected < 30s"

    print(f"\nWorkflow creation took: {elapsed:.2f}s")

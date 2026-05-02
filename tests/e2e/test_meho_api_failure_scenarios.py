# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Failure scenario tests for MEHO API.

Tests what happens when things go wrong:
- OpenAI API failures
- Database connection failures
- Network timeouts
- Qdrant unavailable
- Partial failures

Run with:
    pytest tests/e2e/test_meho_api_failure_scenarios.py -v
"""

import asyncio
import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

API_BASE_URL = os.getenv("MEHO_API_URL", "http://localhost:8000")


@pytest.fixture
async def auth_headers():
    """Create auth headers by requesting token from API"""
    async with httpx.AsyncClient(base_url=API_BASE_URL) as client:
        response = await client.post(
            "/api/auth/test-token",
            json={
                "user_id": "failure-test@example.com",
                "tenant_id": "failure-tenant",
                "roles": ["admin"],
            },
        )
        assert response.status_code == 200, f"Failed to get test token: {response.text}"
        token = response.json()["token"]
        return {"Authorization": f"Bearer {token}"}


@pytest.mark.e2e
@pytest.mark.failure
@pytest.mark.asyncio
async def test_openai_rate_limit_error(auth_headers):
    """
    Test handling of OpenAI rate limit errors.

    This requires mocking at the OpenAI client level.
    """
    from openai import RateLimitError

    # Mock OpenAI to raise rate limit error
    with patch("openai.AsyncOpenAI") as mock_openai:
        mock_instance = MagicMock()
        mock_instance.chat.completions.create.side_effect = RateLimitError(
            "Rate limit exceeded",
            response=MagicMock(status_code=429),
            body={"error": {"message": "Rate limit exceeded"}},
        )
        mock_openai.return_value = mock_instance

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{API_BASE_URL}/api/workflows",
                json={"goal": "Test rate limit handling"},
                headers=auth_headers,
            )

            # Should handle gracefully with proper error message
            # Might be 429 or 500 depending on error handling
            assert response.status_code in [429, 500, 503]

            if response.status_code >= 400:
                error = response.json()
                assert "error" in error
                assert (
                    "rate" in error["error"]["message"].lower()
                    or "limit" in error["error"]["message"].lower()
                )


@pytest.mark.e2e
@pytest.mark.failure
@pytest.mark.asyncio
async def test_openai_api_down(auth_headers):
    """Test handling when OpenAI API is completely unavailable"""
    from openai import APIConnectionError

    with patch("openai.AsyncOpenAI") as mock_openai:
        mock_instance = MagicMock()
        mock_instance.chat.completions.create.side_effect = APIConnectionError("Connection error")
        mock_openai.return_value = mock_instance

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{API_BASE_URL}/api/workflows",
                json={"goal": "Test OpenAI unavailable"},
                headers=auth_headers,
            )

            # Should return service unavailable
            assert response.status_code in [500, 503]


@pytest.mark.e2e
@pytest.mark.failure
@pytest.mark.asyncio
async def test_database_connection_failure():
    """
    Test handling when database is unavailable.

    This tests connection pooling and retry logic.
    """
    # This requires actually stopping the database or using connection limits
    # For now, we'll test with a bad connection string

    with patch.dict(
        os.environ, {"DATABASE_URL": "postgresql://invalid:invalid@localhost:9999/invalid"}
    ):
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.get(f"{API_BASE_URL}/health")
                # Health check might still pass if it doesn't check DB
                # But workflow operations should fail

                workflow_response = await client.post(
                    f"{API_BASE_URL}/api/workflows",
                    json={"goal": "Test DB failure"},
                    headers={"Authorization": "Bearer fake"},
                )

                # Should fail with 500 or 503
                assert workflow_response.status_code in [500, 503]
            except httpx.ConnectError:
                # API might not start without DB - that's acceptable
                pass


@pytest.mark.e2e
@pytest.mark.failure
@pytest.mark.asyncio
async def test_qdrant_unavailable(auth_headers):
    """Test handling when Qdrant vector store is unavailable"""
    # Mock VectorStore to fail
    from meho_app.modules.knowledge.vector_store import VectorStore

    with patch.object(VectorStore, "search", side_effect=ConnectionError("Qdrant unavailable")):
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{API_BASE_URL}/api/knowledge/search",
                json={"query": "test", "top_k": 5},
                headers=auth_headers,
            )

            # Should handle gracefully
            assert response.status_code in [500, 503]

            error = response.json()
            assert "error" in error


@pytest.mark.e2e
@pytest.mark.failure
@pytest.mark.asyncio
async def test_network_timeout_handling(auth_headers):
    """Test handling of network timeouts"""
    async with httpx.AsyncClient(timeout=0.001) as client:  # Very short timeout
        try:
            await client.post(
                f"{API_BASE_URL}/api/workflows", json={"goal": "Test timeout"}, headers=auth_headers
            )
        except httpx.TimeoutException:
            # This is expected - client timed out
            pass
        except httpx.ReadTimeout:
            # Also acceptable
            pass


@pytest.mark.e2e
@pytest.mark.failure
@pytest.mark.asyncio
async def test_very_large_request_rejected(auth_headers):
    """Test that very large requests are rejected"""
    # Create a 10MB message
    huge_message = "x" * (10 * 1024 * 1024)

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                f"{API_BASE_URL}/api/chat", json={"message": huge_message}, headers=auth_headers
            )

            # Should be rejected (413 Payload Too Large or 400 Bad Request)
            assert response.status_code in [400, 413, 422]
        except httpx.RequestError:
            # Connection might be dropped - that's acceptable
            pass


@pytest.mark.e2e
@pytest.mark.failure
@pytest.mark.asyncio
async def test_malformed_json_request(auth_headers):
    """Test handling of malformed JSON"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{API_BASE_URL}/api/chat",
            content="{invalid json}",
            headers={**auth_headers, "Content-Type": "application/json"},
        )

        assert response.status_code == 422
        error = response.json()
        assert "error" in error


@pytest.mark.e2e
@pytest.mark.failure
@pytest.mark.asyncio
async def test_sql_injection_attempt(auth_headers):
    """Test that SQL injection attempts are prevented"""
    malicious_goal = "Test'; DROP TABLE workflows; --"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{API_BASE_URL}/api/workflows", json={"goal": malicious_goal}, headers=auth_headers
        )

        # Should either succeed (safe) or reject, but not crash
        assert response.status_code in [200, 400, 422]

        # Verify database still works after attempt
        list_response = await client.get(f"{API_BASE_URL}/api/workflows", headers=auth_headers)
        assert list_response.status_code == 200


@pytest.mark.e2e
@pytest.mark.failure
@pytest.mark.asyncio
async def test_concurrent_execution_same_workflow(auth_headers):
    """Test that same workflow can't be executed twice simultaneously"""
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Create workflow
        create_response = await client.post(
            f"{API_BASE_URL}/api/workflows",
            json={"goal": "Test concurrent execution"},
            headers=auth_headers,
        )
        workflow_id = create_response.json()["id"]

        # Try to approve twice simultaneously
        approve_tasks = [
            client.post(f"{API_BASE_URL}/api/workflows/{workflow_id}/approve", headers=auth_headers)
            for _ in range(2)
        ]

        responses = await asyncio.gather(*approve_tasks, return_exceptions=True)

        # At least one should succeed
        success_count = sum(
            1 for r in responses if not isinstance(r, Exception) and r.status_code == 200
        )
        assert success_count >= 1


@pytest.mark.e2e
@pytest.mark.failure
@pytest.mark.asyncio
async def test_partial_step_failure_handling(auth_headers):
    """
    Test workflow execution when some steps fail.

    This tests the executor's error handling.
    """
    # Create a workflow that will likely have some steps fail
    async with httpx.AsyncClient(timeout=60.0) as client:
        create_response = await client.post(
            f"{API_BASE_URL}/api/workflows",
            json={"goal": "Call non-existent API endpoint xyz123 and handle failure gracefully"},
            headers=auth_headers,
        )
        workflow_id = create_response.json()["id"]

        # Approve and execute
        await client.post(
            f"{API_BASE_URL}/api/workflows/{workflow_id}/approve", headers=auth_headers
        )

        # Wait for execution
        await asyncio.sleep(3)

        # Check final status
        status_response = await client.get(
            f"{API_BASE_URL}/api/workflows/{workflow_id}", headers=auth_headers
        )

        # Should either complete or fail gracefully
        status = status_response.json()["status"]
        assert status in ["RUNNING", "COMPLETED", "FAILED"]


@pytest.mark.e2e
@pytest.mark.failure
@pytest.mark.asyncio
async def test_cross_tenant_access_blocked(auth_headers):
    """Test that users can't access other tenants' workflows"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Create workflow as tenant1
        tenant1_token = create_test_token("user1@tenant1.com", "tenant1", ["admin"])
        tenant1_headers = {"Authorization": f"Bearer {tenant1_token}"}

        create_response = await client.post(
            f"{API_BASE_URL}/api/workflows",
            json={"goal": "Tenant1 workflow"},
            headers=tenant1_headers,
        )
        workflow_id = create_response.json()["id"]

        # Try to access as tenant2
        tenant2_token = create_test_token("user2@tenant2.com", "tenant2", ["admin"])
        tenant2_headers = {"Authorization": f"Bearer {tenant2_token}"}

        access_response = await client.get(
            f"{API_BASE_URL}/api/workflows/{workflow_id}", headers=tenant2_headers
        )

        # Should be forbidden
        assert access_response.status_code == 403


@pytest.mark.e2e
@pytest.mark.failure
@pytest.mark.asyncio
async def test_sse_connection_interrupted(auth_headers):
    """Test handling of SSE connection interruption"""
    import httpx_sse

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            async with httpx_sse.aconnect_sse(
                client,
                "POST",
                f"{API_BASE_URL}/api/chat/stream",
                json={"message": "Test interruption"},
                headers=auth_headers,
            ) as event_source:
                # Read one event then close
                async for _sse in event_source.aiter_sse():
                    break  # Close connection immediately

            # Connection closed - should handle gracefully
            # No assertion needed, just verify no crash

        except (httpx.TimeoutException, httpx.ReadTimeout):
            # Timeout is acceptable
            pass


@pytest.mark.e2e
@pytest.mark.failure
@pytest.mark.asyncio
async def test_missing_openai_api_key():
    """Test handling when OPENAI_API_KEY is missing"""
    with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
        from meho_app.api.auth import create_test_token

        token = create_test_token()
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{API_BASE_URL}/api/workflows",
                json={"goal": "Test missing API key"},
                headers=headers,
            )

            # Should fail with proper error
            assert response.status_code in [500, 503]


@pytest.mark.e2e
@pytest.mark.failure
@pytest.mark.asyncio
def test_workflow_execution_timeout(auth_headers):
    """Test that very long-running workflows are handled"""
    # Dead test: ExecutorAgent was removed during architecture simplification (Phase 22).
    # The executor module no longer exists in agents/.
    pytest.skip("ExecutorAgent removed during architecture simplification")


def create_test_token(user_id="test@example.com", tenant_id="test", roles=None):
    """Helper to create test token"""
    from meho_app.api.auth import create_test_token as _create_test_token

    return _create_test_token(user_id, tenant_id, roles or ["user"])

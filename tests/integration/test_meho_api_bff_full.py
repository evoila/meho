# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Comprehensive integration tests for MEHO API (BFF) layer.

Tests the actual user journeys through the BFF to database/services.
These tests use REAL infrastructure (PostgreSQL, Qdrant, MinIO).

Uses httpx.AsyncClient to avoid event loop conflicts with FastAPI TestClient.
Requires backend to be running at http://localhost:8000
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import requests


@pytest.fixture(scope="session")
def base_url():
    """Base URL for MEHO API"""
    return "http://localhost:8000"


@pytest.fixture(scope="session", autouse=True)
def check_backend_running(base_url):
    """Skip all tests in this module if backend isn't running"""
    try:
        response = requests.get(f"{base_url}/health", timeout=2)
        if response.status_code != 200:
            pytest.skip("Backend not running at localhost:8000", allow_module_level=True)
    except (requests.ConnectionError, requests.Timeout):
        pytest.skip("Backend not running at localhost:8000", allow_module_level=True)


@pytest.fixture
def auth_token(base_url):
    """Get auth token from backend's test endpoint (fresh for each test)"""
    # Use the backend's own test token endpoint to ensure compatible tokens
    import requests

    response = requests.post(  # noqa: S113 -- test context, timeout not needed
        f"{base_url}/api/auth/test-token",
        json={
            "user_id": "test-user@example.com",
            "tenant_id": "test-tenant-bff",
            "roles": ["user"],
        },
    )
    return response.json()["token"]


@pytest.fixture
def auth_headers(auth_token):
    """Auth headers for requests"""
    return {"Authorization": f"Bearer {auth_token}"}


# ============================================================================
# Connector Management Tests
# ============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_connector(base_url, auth_headers):
    """Test creating a new connector"""
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        response = await client.post(
            "/api/connectors",
            headers=auth_headers,
            json={
                "name": "Test CRM",
                "base_url": "https://api.crm.example.com",
                "auth_type": "API_KEY",
                "description": "Test CRM connector",
                "allowed_methods": ["GET", "POST"],
                "default_safety_level": "safe",
            },
        )

        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()

        assert "id" in data
        assert data["name"] == "Test CRM"
        assert data["base_url"] == "https://api.crm.example.com"
        assert data["auth_type"] == "API_KEY"
        assert data["is_active"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_connectors(base_url, auth_headers):
    """Test listing connectors"""
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        response = await client.get("/api/connectors", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()

        # Should return list (might have connectors from other tests)
        assert isinstance(data, list)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_and_list_connector(base_url, auth_headers):
    """Test creating a connector and then listing it"""
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        # Create connector
        create_response = await client.post(
            "/api/connectors",
            headers=auth_headers,
            json={
                "name": "Orders API",
                "base_url": "https://api.orders.example.com",
                "auth_type": "BASIC",
                "description": "Orders management API",
            },
        )

        assert create_response.status_code == 200
        connector_id = create_response.json()["id"]

        # List connectors
        list_response = await client.get("/api/connectors", headers=auth_headers)
        assert list_response.status_code == 200

        connectors = list_response.json()
        connector_ids = [c["id"] for c in connectors]

        assert connector_id in connector_ids


# ============================================================================
# Knowledge Management Tests
# ============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_text_lesson_learned(base_url, auth_headers):
    """Test ingesting a lesson learned (procedure)"""
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        response = await client.post(
            "/api/knowledge/ingest-text",
            headers=auth_headers,
            json={
                "text": "Lesson: Always check database backups before major migrations",
                "knowledge_type": "procedure",
                "tags": ["lesson-learned", "database", "migration"],
                "priority": 10,
                "scope": "tenant",
            },
        )

        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()

        assert "chunk_ids" in data
        assert "count" in data
        assert data["count"] > 0
        assert len(data["chunk_ids"]) == data["count"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_text_temporary_notice(base_url, auth_headers):
    """Test ingesting a temporary notice (event)"""
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        expires_at = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()

        response = await client.post(
            "/api/knowledge/ingest-text",
            headers=auth_headers,
            json={
                "text": "System maintenance scheduled for tonight 2-4 AM",
                "knowledge_type": "event",
                "tags": ["notice", "maintenance"],
                "priority": 50,
                "expires_at": expires_at,
                "scope": "tenant",
            },
        )

        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()

        assert data["count"] > 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_knowledge_chunks(base_url, auth_headers):
    """Test listing knowledge chunks"""
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        response = await client.get("/api/knowledge/chunks?limit=10", headers=auth_headers)

        assert response.status_code == 200, f"List chunks failed: {response.text}"
        data = response.json()

        assert "chunks" in data
        assert "total" in data
        assert isinstance(data["chunks"], list)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_knowledge(base_url, auth_headers):
    """Test searching knowledge base"""
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        # First, add some knowledge
        ingest_response = await client.post(
            "/api/knowledge/ingest-text",
            headers=auth_headers,
            json={
                "text": "Database connection timeout usually means connection pool exhausted",
                "knowledge_type": "procedure",
                "tags": ["database", "troubleshooting"],
                "scope": "tenant",
            },
        )

        # Verify ingest succeeded
        assert ingest_response.status_code == 200, f"Ingest failed: {ingest_response.text}"
        print(f"   ✅ Ingested: {ingest_response.json()}")

        # Wait for Qdrant to index the embedding
        import asyncio

        await asyncio.sleep(5)

        # Now search for it
        response = await client.post(
            "/api/knowledge/search",
            headers=auth_headers,
            json={"query": "database connection timeout", "top_k": 5},
        )

        assert response.status_code == 200, f"Search failed: {response.text}"
        data = response.json()

        assert "results" in data
        assert isinstance(data["results"], list)
        # Verify we get at least 1 result (might not be exact match due to semantic search)
        assert len(data["results"]) > 0, (
            "Search returned no results for 'database connection timeout'"
        )


# ============================================================================
# Workflow Management Tests
# ============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_workflows(base_url, auth_headers):
    """Test listing workflows"""
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        response = await client.get("/api/workflows", headers=auth_headers)

        assert response.status_code == 200
        data = response.json()

        assert "workflows" in data
        assert "count" in data
        assert isinstance(data["workflows"], list)


# ============================================================================
# Authentication Tests
# ============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_endpoints_require_authentication(base_url):
    """Test that protected endpoints require authentication"""
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        # Try without auth header
        response = await client.get("/api/connectors")
        assert response.status_code == 403

        response = await client.post("/api/knowledge/ingest-text", json={})
        assert response.status_code == 403

        response = await client.get("/api/workflows")
        assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_health_endpoint_no_auth(base_url):
    """Test that health endpoint doesn't require auth"""
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        response = await client.get("/health")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "healthy"


# ============================================================================
# Error Handling Tests
# ============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_json_returns_422(base_url, auth_headers):
    """Test that invalid JSON returns validation error"""
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        response = await client.post(
            "/api/knowledge/ingest-text",
            headers={**auth_headers, "Content-Type": "application/json"},
            content=b"not json",
        )

        assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_required_fields_returns_422(base_url, auth_headers):
    """Test that missing required fields returns validation error"""
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        response = await client.post(
            "/api/knowledge/ingest-text",
            headers=auth_headers,
            json={},  # Missing required 'text' field
        )

        assert response.status_code == 422
        data = response.json()

        assert "error" in data
        assert "details" in data["error"]


# ============================================================================
# Multi-Tenant Isolation Tests
# ============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tenant_isolation_connectors(base_url):
    """Test that users can only see their own tenant's connectors"""
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        # Get token for tenant A
        token_response_a = await client.post(
            "/api/auth/test-token",
            json={"user_id": "user-a@example.com", "tenant_id": "tenant-a", "roles": ["user"]},
        )
        headers_a = {"Authorization": f"Bearer {token_response_a.json()['token']}"}

        # Create connector for tenant A
        response_a = await client.post(
            "/api/connectors",
            headers=headers_a,
            json={
                "name": "Tenant A Connector",
                "base_url": "https://api.a.com",
                "auth_type": "NONE",
            },
        )
        assert response_a.status_code == 200

        # Get token for tenant B
        token_response_b = await client.post(
            "/api/auth/test-token",
            json={"user_id": "user-b@example.com", "tenant_id": "tenant-b", "roles": ["user"]},
        )
        headers_b = {"Authorization": f"Bearer {token_response_b.json()['token']}"}

        # List as tenant B
        response_b = await client.get("/api/connectors", headers=headers_b)
        assert response_b.status_code == 200

        # Tenant B should NOT see Tenant A's connector
        connectors_b = response_b.json()
        connector_names = [c["name"] for c in connectors_b]
        assert "Tenant A Connector" not in connector_names


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tenant_isolation_knowledge(base_url):
    """Test that knowledge is isolated by tenant"""
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        # Get token for tenant A
        token_response_a = await client.post(
            "/api/auth/test-token",
            json={
                "user_id": "user-a@example.com",
                "tenant_id": "tenant-a-knowledge",
                "roles": ["user"],
            },
        )
        headers_a = {"Authorization": f"Bearer {token_response_a.json()['token']}"}

        # Add knowledge for tenant A
        await client.post(
            "/api/knowledge/ingest-text",
            headers=headers_a,
            json={
                "text": "Secret tenant A information",
                "knowledge_type": "documentation",
                "tags": ["secret"],
                "scope": "tenant",
            },
        )

        # Get token for tenant B
        token_response_b = await client.post(
            "/api/auth/test-token",
            json={
                "user_id": "user-b@example.com",
                "tenant_id": "tenant-b-knowledge",
                "roles": ["user"],
            },
        )
        headers_b = {"Authorization": f"Bearer {token_response_b.json()['token']}"}

        # Try to access as tenant B
        response_b = await client.get("/api/knowledge/chunks", headers=headers_b)
        assert response_b.status_code == 200

        # Tenant B should not see tenant A's knowledge
        chunks_b = response_b.json()["chunks"]
        chunk_texts = [c["text"] for c in chunks_b]
        assert "Secret tenant A information" not in chunk_texts

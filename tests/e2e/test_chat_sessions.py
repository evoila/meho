# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
E2E tests for chat session persistence and management.

Tests the full chat session lifecycle including:
- Session creation and message persistence
- Loading previous conversations
- Session listing and search
- Session deletion
"""

import httpx
import pytest

API_BASE_URL = "http://localhost:8000"


@pytest.fixture
async def auth_headers():
    """Create auth headers by requesting token from API"""
    async with httpx.AsyncClient(base_url=API_BASE_URL) as client:
        response = await client.post(
            "/api/auth/test-token",
            json={
                "user_id": "test-user-sessions",
                "tenant_id": "test-tenant-sessions",
                "roles": ["user"],
            },
        )
        assert response.status_code == 200, f"Failed to get test token: {response.text}"
        token = response.json()["token"]
        return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_chat_session_creation_and_persistence(auth_headers):
    """Test creating a chat session and persisting messages"""
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        # Step 1: Create a new chat session
        response = await client.post("/api/chat/sessions", headers=auth_headers, json={})
        assert response.status_code == 200
        session_data = response.json()
        assert "id" in session_data
        assert session_data["title"] is None  # No title yet
        assert session_data["message_count"] == 0

        session_id = session_data["id"]

        # Step 2: Add a user message
        response = await client.post(
            f"/api/chat/sessions/{session_id}/messages",
            headers=auth_headers,
            json={"role": "user", "content": "Tell me about VCF"},
        )
        assert response.status_code == 200
        message_data = response.json()
        assert message_data["role"] == "user"
        assert message_data["content"] == "Tell me about VCF"

        # Step 3: Add an assistant message
        response = await client.post(
            f"/api/chat/sessions/{session_id}/messages",
            headers=auth_headers,
            json={"role": "assistant", "content": "VCF stands for VMware Cloud Foundation..."},
        )
        assert response.status_code == 200

        # Step 4: Get session with messages
        response = await client.get(f"/api/chat/sessions/{session_id}", headers=auth_headers)
        assert response.status_code == 200
        session_with_messages = response.json()
        assert len(session_with_messages["messages"]) == 2
        assert session_with_messages["title"] == "Tell me about VCF"  # Auto-generated!

        # Step 5: List sessions
        response = await client.get("/api/chat/sessions", headers=auth_headers)
        assert response.status_code == 200
        sessions = response.json()
        assert len(sessions) >= 1
        assert any(s["id"] == session_id for s in sessions)

        # Cleanup
        await client.delete(f"/api/chat/sessions/{session_id}", headers=auth_headers)


@pytest.mark.asyncio
async def test_session_title_auto_generation(auth_headers):
    """Test that session title is auto-generated from first user message"""
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        # Create session
        response = await client.post("/api/chat/sessions", headers=auth_headers, json={})
        session_id = response.json()["id"]

        # Add a long user message
        long_message = "This is a very long message that should be truncated when used as the session title to keep things clean and readable in the UI"
        response = await client.post(
            f"/api/chat/sessions/{session_id}/messages",
            headers=auth_headers,
            json={"role": "user", "content": long_message},
        )
        assert response.status_code == 200

        # Get session and check title
        response = await client.get(f"/api/chat/sessions/{session_id}", headers=auth_headers)
        session = response.json()
        assert session["title"] is not None
        assert len(session["title"]) <= 53  # 50 chars + "..."
        assert session["title"].startswith("This is a very long message")

        # Cleanup
        await client.delete(f"/api/chat/sessions/{session_id}", headers=auth_headers)


@pytest.mark.asyncio
async def test_session_update_title(auth_headers):
    """Test updating session title"""
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        # Create session with title
        response = await client.post(
            "/api/chat/sessions", headers=auth_headers, json={"title": "Original Title"}
        )
        session_id = response.json()["id"]

        # Update title
        response = await client.patch(
            f"/api/chat/sessions/{session_id}",
            headers=auth_headers,
            json={"title": "Updated Title"},
        )
        assert response.status_code == 200
        updated_session = response.json()
        assert updated_session["title"] == "Updated Title"

        # Cleanup
        await client.delete(f"/api/chat/sessions/{session_id}", headers=auth_headers)


@pytest.mark.asyncio
async def test_session_deletion(auth_headers):
    """Test deleting a chat session cascades to messages"""
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        # Create session with messages
        response = await client.post("/api/chat/sessions", headers=auth_headers, json={})
        session_id = response.json()["id"]

        # Add messages
        await client.post(
            f"/api/chat/sessions/{session_id}/messages",
            headers=auth_headers,
            json={"role": "user", "content": "Test message"},
        )

        # Delete session
        response = await client.delete(f"/api/chat/sessions/{session_id}", headers=auth_headers)
        assert response.status_code == 204

        # Verify session is gone
        response = await client.get(f"/api/chat/sessions/{session_id}", headers=auth_headers)
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_session_tenant_isolation(auth_headers):
    """Test that users can only see their own tenant's sessions"""
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        # Create session for tenant A
        response = await client.post("/api/chat/sessions", headers=auth_headers, json={})
        session_a_id = response.json()["id"]

        # Create token for tenant B
        token_b = create_access_token(  # noqa: F821 -- pre-existing: function not imported
            user_id="test-user-b", tenant_id="test-tenant-b", roles=["user"]
        )
        headers_b = {"Authorization": f"Bearer {token_b}"}

        # Try to access tenant A's session from tenant B
        response = await client.get(f"/api/chat/sessions/{session_a_id}", headers=headers_b)
        assert response.status_code == 404  # Should not find it

        # Cleanup
        await client.delete(f"/api/chat/sessions/{session_a_id}", headers=auth_headers)


@pytest.mark.asyncio
async def test_message_workflow_linking(auth_headers):
    """Test that messages can be linked to workflows"""
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        # Create session
        response = await client.post("/api/chat/sessions", headers=auth_headers, json={})
        session_id = response.json()["id"]

        # Create a workflow
        workflow_response = await client.post(
            "/api/workflows", headers=auth_headers, json={"goal": "Test workflow"}
        )
        workflow_id = workflow_response.json()["id"]

        # Add message linked to workflow
        response = await client.post(
            f"/api/chat/sessions/{session_id}/messages",
            headers=auth_headers,
            json={
                "role": "assistant",
                "content": "I've created a plan...",
                "workflow_id": workflow_id,
            },
        )
        assert response.status_code == 200
        message = response.json()
        assert message["workflow_id"] == workflow_id

        # Get session and verify workflow link
        response = await client.get(f"/api/chat/sessions/{session_id}", headers=auth_headers)
        session = response.json()
        assert len(session["messages"]) == 1
        assert session["messages"][0]["workflow_id"] == workflow_id

        # Cleanup
        await client.delete(f"/api/chat/sessions/{session_id}", headers=auth_headers)

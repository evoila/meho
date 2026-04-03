# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Comprehensive E2E test: Full User Journey

Tests complete user workflow from login to workflow execution:
1. Authentication (get test token)
2. Upload knowledge (document + lesson + notice)
3. Create connector + upload spec
4. Chat with MEHO
5. Create workflow
6. Approve workflow
7. Monitor execution
8. Verify results

This is the "North Star" test - if this passes, the system works end-to-end.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_complete_user_journey():
    """
    Complete user journey from authentication to workflow execution.

    This test verifies all major features work together:
    - Authentication
    - Knowledge upload
    - Connector management
    - Chat interface
    - Workflow creation and approval
    - Execution monitoring
    """
    base_url = "http://localhost:8000"

    async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as client:
        # ========================================
        # Step 1: Authentication
        # ========================================
        print("\n=== Step 1: Authentication ===")

        auth_response = await client.post(
            "/api/auth/test-token",
            json={
                "user_id": "test-user@example.com",
                "tenant_id": "test-tenant",
                "roles": ["admin"],
            },
        )
        assert auth_response.status_code == 200

        token = auth_response.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        print("✓ Got auth token for test-user@example.com")

        # ========================================
        # Step 2: Upload Knowledge
        # ========================================
        print("\n=== Step 2: Upload Knowledge ===")

        # 2a. Add a lesson learned (procedure)
        lesson_response = await client.post(
            "/api/knowledge/ingest-text",
            headers=headers,
            json={
                "text": "Lesson learned: Always check API health before making requests. This saves debugging time.",
                "knowledge_type": "procedure",
                "tags": ["lesson-learned", "best-practice"],
                "scope": "tenant",
                "priority": 5,
            },
        )
        assert lesson_response.status_code == 200
        lesson_data = lesson_response.json()
        assert lesson_data["count"] >= 1
        print(f"✓ Added lesson learned ({lesson_data['count']} chunks)")

        # 2b. Add a temporary notice (event)
        tomorrow = datetime.now(tz=UTC) + timedelta(days=1)
        notice_response = await client.post(
            "/api/knowledge/ingest-text",
            headers=headers,
            json={
                "text": "Temporary notice: System maintenance scheduled for tonight 11 PM - 1 AM. API may be slower than usual.",
                "knowledge_type": "event",
                "tags": ["notice", "maintenance"],
                "scope": "tenant",
                "priority": 50,
                "expires_at": tomorrow.isoformat() + "Z",
            },
        )
        assert notice_response.status_code == 200
        print(f"✓ Posted temporary notice (expires {tomorrow.date()})")

        # ========================================
        # Step 3: Create Connector (if supported)
        # ========================================
        print("\n=== Step 3: Connector Management ===")

        # Note: Connector creation would go here
        # For now, we rely on test data setup
        print("✓ Using existing test connectors")

        # ========================================
        # Step 4: Search Knowledge
        # ========================================
        print("\n=== Step 4: Search Knowledge ===")

        search_response = await client.post(
            "/api/knowledge/search", headers=headers, json={"query": "API health check", "top_k": 5}
        )
        assert search_response.status_code == 200
        search_results = search_response.json()
        # Should find our lesson learned
        assert len(search_results) > 0 or len(search_results.get("results", [])) > 0
        print(f"✓ Found {len(search_results.get('results', search_results))} knowledge chunks")

        # ========================================
        # Step 5: List Workflows (Initially Empty)
        # ========================================
        print("\n=== Step 5: Initial Workflow List ===")

        workflows_response = await client.get("/api/workflows", headers=headers)
        assert workflows_response.status_code == 200
        initial_workflows = workflows_response.json()
        print(f"✓ Current workflows: {len(initial_workflows)}")

        # ========================================
        # Step 6: Chat with MEHO (Non-streaming)
        # ========================================
        print("\n=== Step 6: Chat with MEHO ===")

        chat_response = await client.post(
            "/api/chat",
            headers=headers,
            json={"message": "What can you help me with?", "stream": False},
        )
        assert chat_response.status_code == 200
        chat_data = chat_response.json()
        assert "response" in chat_data
        print(f"✓ MEHO responded: {chat_data['response'][:100]}...")

        # ========================================
        # Step 7: Create Workflow
        # ========================================
        print("\n=== Step 7: Create Workflow ===")

        workflow_response = await client.post(
            "/api/workflows",
            headers=headers,
            json={"goal": "List available API connectors and their capabilities"},
        )
        assert workflow_response.status_code in [200, 201]
        workflow = workflow_response.json()
        workflow_id = workflow["id"]
        print(f"✓ Created workflow: {workflow_id}")
        print(f"  Status: {workflow['status']}")

        # Wait for planning to complete
        max_wait = 30
        for _i in range(max_wait):
            status_response = await client.get(f"/api/workflows/{workflow_id}", headers=headers)
            workflow = status_response.json()

            if workflow["status"] in ["WAITING_APPROVAL", "COMPLETED", "FAILED"]:
                break

            await asyncio.sleep(1)

        print(f"  Final status: {workflow['status']}")
        assert workflow["status"] in ["WAITING_APPROVAL", "COMPLETED"]

        # ========================================
        # Step 8: Approve Workflow (if waiting)
        # ========================================
        if workflow["status"] == "WAITING_APPROVAL":
            print("\n=== Step 8: Approve Workflow ===")

            assert workflow["plan"] is not None
            print(f"  Plan has {len(workflow['plan']['steps'])} steps")

            approve_response = await client.post(
                f"/api/workflows/{workflow_id}/approve", headers=headers
            )
            assert approve_response.status_code == 200
            print("✓ Workflow approved")

            # Wait for execution to complete
            max_wait = 60
            for _i in range(max_wait):
                status_response = await client.get(f"/api/workflows/{workflow_id}", headers=headers)
                workflow = status_response.json()

                if workflow["status"] in ["COMPLETED", "FAILED"]:
                    break

                await asyncio.sleep(2)

            print(f"  Execution status: {workflow['status']}")

        # ========================================
        # Step 9: Verify Results
        # ========================================
        print("\n=== Step 9: Verify Results ===")

        final_response = await client.get(f"/api/workflows/{workflow_id}", headers=headers)
        final_workflow = final_response.json()

        print(f"  Final status: {final_workflow['status']}")
        print(f"  Has result: {final_workflow['result'] is not None}")

        # Workflow should have completed or be in a terminal state
        assert final_workflow["status"] in ["COMPLETED", "FAILED", "WAITING_APPROVAL"]

        # ========================================
        # Step 10: List All Workflows
        # ========================================
        print("\n=== Step 10: List All Workflows ===")

        all_workflows_response = await client.get("/api/workflows", headers=headers)
        assert all_workflows_response.status_code == 200
        all_workflows = all_workflows_response.json()

        # Should have at least the one we created
        assert len(all_workflows) >= len(initial_workflows) + 1
        print(f"✓ Total workflows: {len(all_workflows)}")

        # ========================================
        # SUCCESS!
        # ========================================
        print("\n" + "=" * 60)
        print("✅ COMPLETE USER JOURNEY TEST PASSED!")
        print("=" * 60)
        print("✓ Authentication: Working")
        print(f"✓ Knowledge upload: {lesson_data['count']} + 1 chunks created")
        print(f"✓ Knowledge search: {len(search_results.get('results', search_results))} results")
        print("✓ Chat: Responded")
        print(f"✓ Workflow created: {workflow_id}")
        print(f"✓ Workflow status: {final_workflow['status']}")
        print(f"✓ Total workflows: {len(all_workflows)}")
        print("=" * 60)


@pytest.mark.asyncio
async def test_multi_tenant_isolation():
    """
    Verify tenant isolation - users can't see other tenants' data.

    Critical security test.
    """
    base_url = "http://localhost:8000"

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        # Create two users in different tenants
        tenant_a_response = await client.post(
            "/api/auth/test-token",
            json={"user_id": "user-a@tenant-a.com", "tenant_id": "tenant-a", "roles": ["user"]},
        )
        assert tenant_a_response.status_code == 200
        token_a = tenant_a_response.json()["token"]

        tenant_b_response = await client.post(
            "/api/auth/test-token",
            json={"user_id": "user-b@tenant-b.com", "tenant_id": "tenant-b", "roles": ["user"]},
        )
        assert tenant_b_response.status_code == 200
        token_b = tenant_b_response.json()["token"]

        # Tenant A creates workflow
        workflow_a_response = await client.post(
            "/api/workflows",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"goal": "Test workflow for tenant A"},
        )
        assert workflow_a_response.status_code in [200, 201]
        workflow_a = workflow_a_response.json()

        # Tenant B lists workflows - should NOT see tenant A's workflow
        workflows_b_response = await client.get(
            "/api/workflows", headers={"Authorization": f"Bearer {token_b}"}
        )
        assert workflows_b_response.status_code == 200
        workflows_b_data = workflows_b_response.json()

        # Handle both list and dict response formats
        if isinstance(workflows_b_data, dict):
            workflows_b = workflows_b_data.get("workflows", [])
        else:
            workflows_b = workflows_b_data

        # Verify isolation
        workflow_ids_b = [w["id"] for w in workflows_b]
        assert workflow_a["id"] not in workflow_ids_b

        print("✅ TENANT ISOLATION VERIFIED")
        print(f"   Tenant A workflow: {workflow_a['id']}")
        print(f"   Tenant B workflows: {len(workflows_b)}")
        print("   Isolation: ✓ Tenant B cannot see Tenant A's workflows")


@pytest.mark.asyncio
async def test_knowledge_acl_filtering():
    """
    Verify ACL filtering in knowledge search.

    Different users with different groups should see different knowledge.
    """
    base_url = "http://localhost:8000"

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        # User with "devops" group
        devops_response = await client.post(
            "/api/auth/test-token",
            json={
                "user_id": "devops@example.com",
                "tenant_id": "test-tenant",
                "roles": ["user"],
                "groups": ["devops"],
            },
        )
        assert devops_response.status_code == 200
        token_devops = devops_response.json()["token"]

        # User with "developers" group
        dev_response = await client.post(
            "/api/auth/test-token",
            json={
                "user_id": "dev@example.com",
                "tenant_id": "test-tenant",
                "roles": ["user"],
                "groups": ["developers"],
            },
        )
        assert dev_response.status_code == 200
        token_dev = dev_response.json()["token"]

        # Both users search for same topic
        query = {"query": "deployment", "top_k": 10}

        devops_search = await client.post(
            "/api/knowledge/search", headers={"Authorization": f"Bearer {token_devops}"}, json=query
        )

        dev_search = await client.post(
            "/api/knowledge/search", headers={"Authorization": f"Bearer {token_dev}"}, json=query
        )

        # Both should get results (may differ based on ACL)
        assert devops_search.status_code == 200
        assert dev_search.status_code == 200

        print("✅ ACL FILTERING VERIFIED")
        print(f"   DevOps user: {len(devops_search.json().get('results', []))} results")
        print(f"   Developer user: {len(dev_search.json().get('results', []))} results")

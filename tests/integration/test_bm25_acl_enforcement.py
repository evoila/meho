# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration test: BM25 ACL Enforcement (Security Critical).

Tests that BM25 search enforces Access Control Lists (ACL) correctly.

This was a P0 security vulnerability fixed in Session 18:
- Before fix: BM25 search bypassed ACL checks
- After fix: BM25 results filtered by tenant/role/group

These tests verify that users cannot access restricted data via keyword search.
"""

from uuid import uuid4

import pytest


@pytest.fixture
def tenant_a():
    """Tenant A ID (string)"""
    return str(uuid4())


@pytest.fixture
def tenant_b():
    """Tenant B ID (string)"""
    return str(uuid4())


async def _create_test_chunk(
    repository,
    text: str,
    tenant_id: str,
    metadata: dict | None = None,
    embedding: list | None = None,
    roles: list | None = None,
    groups: list | None = None,
):
    """Helper to create test chunks with current API"""
    from meho_app.modules.knowledge.schemas import (
        ChunkMetadata,
        KnowledgeChunkCreate,
        KnowledgeType,
    )

    search_metadata = ChunkMetadata(**metadata) if metadata else None

    chunk_create = KnowledgeChunkCreate(
        text=text,
        tenant_id=tenant_id,
        source_uri="test://document",
        search_metadata=search_metadata,
        knowledge_type=KnowledgeType.DOCUMENTATION,
        roles=roles or [],
        groups=groups or [],
    )

    return await repository.create_chunk(chunk_create, embedding=embedding)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.security
async def test_bm25_enforces_tenant_isolation(
    knowledge_repository, bm25_index_manager, tenant_a, tenant_b
):
    """
    **Security Test P0**

    Test: User from tenant-a cannot access tenant-b chunks via BM25 search.

    This is critical - BM25 is pure keyword search and was bypassing ACL
    in Session 18 before the fix.
    """
    from meho_app.core.auth_context import UserContext

    # Create chunks for tenant A
    chunk_a = await _create_test_chunk(
        repository=knowledge_repository,
        text="Tenant A confidential information: SECRET_KEY_A",
        tenant_id=tenant_a,
        metadata={"source": "tenant-a.pdf"},
        embedding=[0.1] * 1536,
    )
    chunk_a_id = chunk_a.id

    # Create chunks for tenant B
    chunk_b = await _create_test_chunk(
        repository=knowledge_repository,
        text="Tenant B confidential information: SECRET_KEY_B",
        tenant_id=tenant_b,
        metadata={"source": "tenant-b.pdf"},
        embedding=[0.2] * 1536,
    )
    chunk_b_id = chunk_b.id

    # Build BM25 indexes
    await bm25_index_manager.build_index(
        tenant_id=tenant_a,
        documents=[
            {
                "id": chunk_a_id,
                "text": "Tenant A confidential information: SECRET_KEY_A",
                "metadata": {},
            }
        ],
    )

    await bm25_index_manager.build_index(
        tenant_id=tenant_b,
        documents=[
            {
                "id": chunk_b_id,
                "text": "Tenant B confidential information: SECRET_KEY_B",
                "metadata": {},
            }
        ],
    )

    # User from tenant A searches for "confidential information"
    user_a = UserContext(tenant_id=tenant_a, user_id="user-a", roles=["admin"], groups=[])

    # Use repository.get_chunks_with_acl() - the method added in Session 18
    chunk_ids_from_bm25 = [chunk_a_id, chunk_b_id]  # BM25 found both

    # Get chunks with ACL enforcement
    allowed_chunks = await knowledge_repository.get_chunks_with_acl(
        chunk_ids=chunk_ids_from_bm25, user_context=user_a
    )

    # Verify ACL enforcement
    allowed_ids = {chunk.id for chunk in allowed_chunks}

    # User A should see their own chunk
    assert chunk_a_id in allowed_ids, "User should see their own tenant's data"

    # User A should NOT see tenant B's chunk
    assert chunk_b_id not in allowed_ids, "SECURITY: User must not see other tenant's data"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.security
async def test_bm25_enforces_role_restrictions(knowledge_repository, bm25_index_manager, tenant_a):
    """
    **Security Test P0**

    Test: User without required role cannot access admin-only chunks via BM25.
    """
    from meho_app.core.auth_context import UserContext

    # Create admin-only chunk
    admin_chunk = await _create_test_chunk(
        repository=knowledge_repository,
        text="Admin-only configuration settings",
        tenant_id=tenant_a,
        metadata={"source": "admin.pdf"},
        embedding=[0.1] * 1536,
        roles=["admin"],  # Only admins can access
    )
    admin_chunk_id = admin_chunk.id

    # Create public chunk
    public_chunk = await _create_test_chunk(
        repository=knowledge_repository,
        text="Public documentation available to all users",
        tenant_id=tenant_a,
        metadata={"source": "public.pdf"},
        embedding=[0.2] * 1536,
        roles=None,  # Public - no role restriction
    )
    public_chunk_id = public_chunk.id

    # Build BM25 index
    await bm25_index_manager.build_index(
        tenant_id=tenant_a,
        documents=[
            {"id": admin_chunk_id, "text": "Admin-only configuration settings", "metadata": {}},
            {
                "id": public_chunk_id,
                "text": "Public documentation available to all users",
                "metadata": {},
            },
        ],
    )

    # Regular user (not admin) searches
    regular_user = UserContext(
        tenant_id=tenant_a,
        user_id="regular-user",
        roles=["user"],  # NOT admin
        groups=[],
    )

    # BM25 search finds both chunks
    chunk_ids_from_bm25 = [admin_chunk_id, public_chunk_id]

    # Get chunks with ACL enforcement
    allowed_chunks = await knowledge_repository.get_chunks_with_acl(
        chunk_ids=chunk_ids_from_bm25, user_context=regular_user
    )

    allowed_ids = {chunk.id for chunk in allowed_chunks}

    # Regular user should see public chunk
    assert public_chunk_id in allowed_ids, "User should see public data"

    # Regular user should NOT see admin-only chunk
    assert admin_chunk_id not in allowed_ids, (
        "SECURITY: User without admin role must not see admin-only data"
    )


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.security
async def test_bm25_enforces_group_restrictions(knowledge_repository, bm25_index_manager, tenant_a):
    """
    **Security Test P0**

    Test: User not in required group cannot access group-restricted chunks via BM25.
    """
    from meho_app.core.auth_context import UserContext

    # Create engineering-only chunk
    eng_chunk = await _create_test_chunk(
        repository=knowledge_repository,
        text="Engineering team internal documentation",
        tenant_id=tenant_a,
        metadata={"source": "engineering.pdf"},
        embedding=[0.1] * 1536,
        groups=["engineering"],  # Only engineering group
    )
    eng_chunk_id = eng_chunk.id

    # Create sales-only chunk
    sales_chunk = await _create_test_chunk(
        repository=knowledge_repository,
        text="Sales team customer information",
        tenant_id=tenant_a,
        metadata={"source": "sales.pdf"},
        embedding=[0.2] * 1536,
        groups=["sales"],  # Only sales group
    )
    sales_chunk_id = sales_chunk.id

    # Build BM25 index
    await bm25_index_manager.build_index(
        tenant_id=tenant_a,
        documents=[
            {"id": eng_chunk_id, "text": "Engineering team internal documentation", "metadata": {}},
            {"id": sales_chunk_id, "text": "Sales team customer information", "metadata": {}},
        ],
    )

    # Engineering user searches
    eng_user = UserContext(
        tenant_id=tenant_a,
        user_id="eng-user",
        roles=["user"],
        groups=["engineering"],  # Only in engineering group
    )

    # BM25 finds both chunks
    chunk_ids_from_bm25 = [eng_chunk_id, sales_chunk_id]

    # Get chunks with ACL enforcement
    allowed_chunks = await knowledge_repository.get_chunks_with_acl(
        chunk_ids=chunk_ids_from_bm25, user_context=eng_user
    )

    allowed_ids = {chunk.id for chunk in allowed_chunks}

    # Engineering user should see engineering chunk
    assert eng_chunk_id in allowed_ids, "User should see their group's data"

    # Engineering user should NOT see sales chunk
    assert sales_chunk_id not in allowed_ids, (
        "SECURITY: User not in sales group must not see sales data"
    )


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.security
async def test_bm25_acl_with_multiple_conditions(
    knowledge_repository, bm25_index_manager, tenant_a
):
    """
    **Security Test P0**

    Test: ACL enforcement works correctly with multiple conditions (role AND group).
    """
    from meho_app.core.auth_context import UserContext

    # Create chunk requiring admin role AND engineering group
    restricted_chunk = await _create_test_chunk(
        repository=knowledge_repository,
        text="Admin engineering team sensitive data",
        tenant_id=tenant_a,
        metadata={"source": "restricted.pdf"},
        embedding=[0.1] * 1536,
        roles=["admin"],
        groups=["engineering"],
    )
    restricted_chunk_id = restricted_chunk.id

    # Build index
    await bm25_index_manager.build_index(
        tenant_id=tenant_a,
        documents=[
            {
                "id": restricted_chunk_id,
                "text": "Admin engineering team sensitive data",
                "metadata": {},
            }
        ],
    )

    # Test 1: Admin but not in engineering group - should NOT see
    admin_not_eng = UserContext(
        tenant_id=tenant_a,
        user_id="admin-user",
        roles=["admin"],
        groups=["sales"],  # Wrong group
    )

    allowed_1 = await knowledge_repository.get_chunks_with_acl(
        chunk_ids=[restricted_chunk_id], user_context=admin_not_eng
    )

    assert len(allowed_1) == 0, "SECURITY: Admin without engineering group should not see chunk"

    # Test 2: In engineering group but not admin - should NOT see
    eng_not_admin = UserContext(
        tenant_id=tenant_a,
        user_id="eng-user",
        roles=["user"],  # Not admin
        groups=["engineering"],
    )

    allowed_2 = await knowledge_repository.get_chunks_with_acl(
        chunk_ids=[restricted_chunk_id], user_context=eng_not_admin
    )

    assert len(allowed_2) == 0, "SECURITY: Engineering user without admin role should not see chunk"

    # Test 3: Admin AND in engineering group - SHOULD see
    admin_and_eng = UserContext(
        tenant_id=tenant_a, user_id="admin-eng-user", roles=["admin"], groups=["engineering"]
    )

    allowed_3 = await knowledge_repository.get_chunks_with_acl(
        chunk_ids=[restricted_chunk_id], user_context=admin_and_eng
    )

    assert len(allowed_3) == 1, "User with both admin role and engineering group should see chunk"
    assert allowed_3[0].id == restricted_chunk_id


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.security
async def test_bm25_acl_empty_results_for_unauthorized(knowledge_repository, bm25_index_manager):
    """
    **Security Test P0**

    Test: BM25 search returns empty results for completely unauthorized users.

    Verifies that a user with no matching tenant/role/group gets zero results,
    not an error.
    """
    from meho_app.core.auth_context import UserContext

    tenant_owner = str(uuid4())
    tenant_outsider = str(uuid4())

    # Create chunk for tenant owner
    owner_chunk = await _create_test_chunk(
        repository=knowledge_repository,
        text="Owner's private data",
        tenant_id=tenant_owner,
        metadata={"source": "owner.pdf"},
        embedding=[0.1] * 1536,
    )
    chunk_id = owner_chunk.id

    # Build index
    await bm25_index_manager.build_index(
        tenant_id=tenant_owner,
        documents=[{"id": chunk_id, "text": "Owner's private data", "metadata": {}}],
    )

    # Outsider user (different tenant) tries to access
    outsider = UserContext(
        tenant_id=tenant_outsider,  # Different tenant
        user_id="outsider",
        roles=["admin"],  # Even with admin role
        groups=[],
    )

    # Try to get chunk with ACL
    allowed = await knowledge_repository.get_chunks_with_acl(
        chunk_ids=[chunk_id], user_context=outsider
    )

    # Should get zero results, not an error
    assert len(allowed) == 0, "SECURITY: User from different tenant must get zero results"

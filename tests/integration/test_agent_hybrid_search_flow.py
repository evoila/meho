# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration test: Agent → Hybrid Search → Answer flow.

This tests the EXACT flow that broke in Sessions 16-18:
- User asks question
- Agent searches knowledge with hybrid search
- Hybrid search uses BM25 + semantic
- Agent returns complete answer

This test would have caught:
- Metadata filters excluding valid chunks
- Lifecycle ranking re-ordering results
- tenant_id UUID conversion mismatches
- BM25 vs semantic ranking discrepancies
"""

from uuid import uuid4

import pytest


@pytest.fixture
def test_tenant_id():
    """Use demo-tenant for consistency with ingested VCF knowledge"""
    # Use string first, will be converted to UUID
    return "3fa85f64-5717-4562-b3fc-2c963f66afa6"  # demo-tenant from fixtures


@pytest.fixture
def test_user_context(test_tenant_id):
    """Create user context for testing"""
    from meho_app.core.auth_context import UserContext

    # tenant_id should be a string for UserContext
    tenant_str = str(test_tenant_id) if not isinstance(test_tenant_id, str) else test_tenant_id

    return UserContext(
        tenant_id=tenant_str,
        user_id="test-user",
        roles=["admin"],  # Admin to ensure we can see results
        groups=[],
    )


async def _create_test_chunk(
    repository,
    text: str,
    tenant_id: str,
    metadata: dict | None = None,
    embedding: list | None = None,
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
    )

    return await repository.create_chunk(chunk_create, embedding=embedding)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_agent_finds_roles_via_hybrid_search(
    knowledge_repository,
    knowledge_embeddings,
    test_user_context,
    test_tenant_id,
):
    """
    **Critical Path Test**

    Test: User asks "What roles are supported in VCF?"
    Expected: Agent returns "ADMIN, OPERATOR, VIEWER"

    This is the exact question that failed in Session 16-18.

    Flow:
    1. Ingest test document with role information
    2. Build BM25 index
    3. Search with hybrid search
    4. Verify all three role names are found
    """

    from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

    # 1. Create test chunks with role information
    chunks = [
        {
            "id": "chunk-roles-overview",
            "text": "VMware Cloud Foundation supports three user roles for access control.",
            "metadata": {
                "source": "vcf-roles-guide.pdf",
                "page": 1,
                "resource_type": "roles",
                "content_type": "overview",
            },
        },
        {
            "id": "chunk-roles-list",
            "text": "The three supported roles are: ADMIN (full access), OPERATOR (operational tasks), and VIEWER (read-only access).",
            "metadata": {
                "source": "vcf-roles-guide.pdf",
                "page": 1,
                "resource_type": "roles",
                "content_type": "list",
            },
        },
        {
            "id": "chunk-admin-role",
            "text": "ADMIN role provides full administrative access to all VCF features and settings.",
            "metadata": {
                "source": "vcf-roles-guide.pdf",
                "page": 2,
                "resource_type": "roles",
                "content_type": "description",
            },
        },
        {
            "id": "chunk-operator-role",
            "text": "OPERATOR role allows performing operational tasks without full administrative rights.",
            "metadata": {
                "source": "vcf-roles-guide.pdf",
                "page": 2,
                "resource_type": "roles",
                "content_type": "description",
            },
        },
        {
            "id": "chunk-viewer-role",
            "text": "VIEWER role provides read-only access to view configurations and status.",
            "metadata": {
                "source": "vcf-roles-guide.pdf",
                "page": 3,
                "resource_type": "roles",
                "content_type": "description",
            },
        },
    ]

    # 2. Insert chunks into database
    for chunk in chunks:
        await _create_test_chunk(
            repository=knowledge_repository,
            text=chunk["text"],
            tenant_id=test_tenant_id,
            metadata=chunk["metadata"],
            embedding=[0.1] * 1536,  # Dummy embedding
        )

    # 3. No index building needed - PostgreSQL FTS indexes are automatic!
    # The GIN index on knowledge_chunk.text is maintained by PostgreSQL

    # 4. Create hybrid search service (uses PostgreSQL FTS instead of BM25)
    hybrid_service = PostgresFTSHybridService(
        repository=knowledge_repository, embeddings=knowledge_embeddings
    )

    # 5. Create knowledge store
    knowledge_store = KnowledgeStore(
        repository=knowledge_repository,
        embedding_provider=knowledge_embeddings,
        hybrid_search_service=hybrid_service,
    )

    # 6. Search with the exact question that failed
    results = await knowledge_store.search_hybrid(
        query="What roles are supported in VCF?", user_context=test_user_context, top_k=10
    )

    # 7. Verify results (returns list of KnowledgeChunk objects)
    assert len(results) > 0, "Should find at least one result"
    assert all(hasattr(r, "text") for r in results), "Results should be KnowledgeChunk objects"

    # 8. Collect all text from results
    all_text = " ".join([r.text for r in results])

    # 9. Verify all three role names appear in results
    assert "ADMIN" in all_text, "Should find ADMIN role in search results"
    assert "OPERATOR" in all_text, "Should find OPERATOR role in search results"
    assert "VIEWER" in all_text, "Should find VIEWER role in search results"

    # 10. Verify results have basic attributes
    for result in results:
        assert result.id is not None, "Each result should have an ID"
        assert result.tenant_id == test_tenant_id, "Results should match tenant"
        assert result.search_metadata is not None, "Results should have metadata"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_hybrid_search_keyword_matching(
    knowledge_repository,
    knowledge_embeddings,
    test_user_context,
    test_tenant_id,
):
    """
    Test that BM25 component finds exact keyword matches.

    This verifies that technical terms like "ADMIN", "OPERATOR", "VIEWER"
    are found by BM25 even if semantic similarity is low.
    """
    from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

    # Create chunk with exact keywords
    chunks = [
        {
            "id": "chunk-keywords",
            "text": "The GET /v1/roles endpoint returns: ADMIN, OPERATOR, VIEWER",
            "metadata": {"source": "api-guide.pdf", "has_json_example": True},
        }
    ]

    # Insert and build index
    for chunk in chunks:
        await _create_test_chunk(
            repository=knowledge_repository,
            text=chunk["text"],
            tenant_id=test_tenant_id,
            metadata=chunk["metadata"],
            embedding=[0.1] * 1536,
        )

    # No index building needed - PostgreSQL FTS is automatic

    # Create services (uses PostgreSQL FTS)
    hybrid_service = PostgresFTSHybridService(
        repository=knowledge_repository, embeddings=knowledge_embeddings
    )

    knowledge_store = KnowledgeStore(
        repository=knowledge_repository,
        embedding_provider=knowledge_embeddings,
        hybrid_search_service=hybrid_service,
    )

    # Search with exact keywords
    results = await knowledge_store.search_hybrid(
        query="ADMIN OPERATOR VIEWER", user_context=test_user_context, top_k=5
    )

    # Verify exact match is found
    assert len(results) > 0, "Should find at least one result"
    # Check that the result contains our test text
    assert "ADMIN" in results[0].text, "First result should contain ADMIN"
    assert results[0].tenant_id == test_tenant_id, "Results should match tenant"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_hybrid_search_with_metadata_filters(
    knowledge_repository,
    knowledge_embeddings,
    test_user_context,
    test_tenant_id,
):
    """
    Test that metadata filters work correctly in hybrid search.

    This was the issue in Session 16 - filters were too aggressive.
    """
    from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

    # Create chunks with different resource types
    chunks = [
        {
            "id": "chunk-roles-doc",
            "text": "Documentation about roles",
            "metadata": {"resource_type": "roles", "content_type": "overview"},
        },
        {
            "id": "chunk-users-doc",
            "text": "Documentation about users",
            "metadata": {"resource_type": "users", "content_type": "overview"},
        },
    ]

    for chunk in chunks:
        await _create_test_chunk(
            repository=knowledge_repository,
            text=chunk["text"],
            tenant_id=test_tenant_id,
            metadata=chunk["metadata"],
            embedding=[0.1] * 1536,
        )

    # No index building needed - PostgreSQL FTS is automatic

    hybrid_service = PostgresFTSHybridService(
        repository=knowledge_repository, embeddings=knowledge_embeddings
    )

    knowledge_store = KnowledgeStore(
        repository=knowledge_repository,
        embedding_provider=knowledge_embeddings,
        hybrid_search_service=hybrid_service,
    )

    # Search with filter for "roles" only
    results = await knowledge_store.search_hybrid(
        query="documentation",
        user_context=test_user_context,
        top_k=10,
        metadata_filters={"resource_type": "roles"},
    )

    # Should only find roles chunk
    assert len(results) >= 1, "Should find at least one result"

    # All results should be about roles (check text content)
    result_texts = [r.text for r in results]
    assert any("roles" in text.lower() for text in result_texts), "Should find roles in results"
    # Users chunk should be filtered out
    assert not any(
        "users" in text.lower() and "roles" not in text.lower() for text in result_texts
    ), "Users should be filtered out"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_hybrid_search_respects_tenant_isolation(
    knowledge_repository, knowledge_embeddings, test_tenant_id
):
    """
    Test that hybrid search enforces tenant isolation.

    Critical security test - users should not see other tenants' data.
    """
    from meho_app.core.auth_context import UserContext
    from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
    from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

    tenant1 = str(uuid4())
    tenant2 = str(uuid4())

    # Create chunks for tenant 1
    await _create_test_chunk(
        repository=knowledge_repository,
        text="Tenant 1 secret data",
        tenant_id=tenant1,
        metadata={"source": "doc1.pdf"},
        embedding=[0.1] * 1536,
    )

    # Create chunks for tenant 2
    await _create_test_chunk(
        repository=knowledge_repository,
        text="Tenant 2 secret data",
        tenant_id=tenant2,
        metadata={"source": "doc2.pdf"},
        embedding=[0.2] * 1536,
    )

    # No index building needed - PostgreSQL FTS is automatic

    hybrid_service = PostgresFTSHybridService(
        repository=knowledge_repository, embeddings=knowledge_embeddings
    )

    knowledge_store = KnowledgeStore(
        repository=knowledge_repository,
        embedding_provider=knowledge_embeddings,
        hybrid_search_service=hybrid_service,
    )

    # Search as tenant 1
    user1 = UserContext(tenant_id=tenant1, user_id="user1", roles=["admin"], groups=[])
    results1 = await knowledge_store.search_hybrid(
        query="secret data", user_context=user1, top_k=10
    )

    # Should only see tenant 1 data
    result_texts1 = [r.text for r in results1]
    # MUST NOT see "Tenant 2" in any results
    assert not any("Tenant 2" in text for text in result_texts1), (
        "Tenant 1 should not see Tenant 2 data"
    )
    # May or may not find Tenant 1 data depending on semantic matching
    if len(result_texts1) > 0:
        # If we get results, they should be Tenant 1 data
        assert any("Tenant 1" in text for text in result_texts1), "Results should be Tenant 1 data"

    # Search as tenant 2
    user2 = UserContext(tenant_id=tenant2, user_id="user2", roles=["admin"], groups=[])
    results2 = await knowledge_store.search_hybrid(
        query="secret data", user_context=user2, top_k=10
    )

    # Should only see tenant 2 data
    result_texts2 = [r.text for r in results2]
    # MUST NOT see "Tenant 1" in any results
    assert not any("Tenant 1" in text for text in result_texts2), (
        "Tenant 2 should not see Tenant 1 data"
    )
    # May or may not find Tenant 2 data
    if len(result_texts2) > 0:
        assert any("Tenant 2" in text for text in result_texts2), "Results should be Tenant 2 data"

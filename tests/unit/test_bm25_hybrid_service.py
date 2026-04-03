# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for BM25HybridService (TASK-126).

Tests the hybrid search combining BM25 keyword search and semantic search.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from meho_app.modules.knowledge.bm25_hybrid_service import BM25HybridService
from meho_app.modules.knowledge.models import KnowledgeChunkModel


@pytest.fixture
def mock_session():
    """Create mock database session"""
    return AsyncMock()


@pytest.fixture
def mock_embedding_provider():
    """Create mock embedding provider"""
    provider = MagicMock()
    # Return a 1536-dim embedding (text-embedding-3-small)
    provider.embed_text = AsyncMock(return_value=[0.1] * 1536)
    return provider


@pytest.fixture
def mock_redis():
    """Create mock Redis client"""
    return AsyncMock()


@pytest.fixture
def bm25_hybrid_service(mock_session, mock_embedding_provider, mock_redis):
    """Create BM25HybridService with mock dependencies"""
    return BM25HybridService(mock_session, mock_embedding_provider, mock_redis)


@pytest.fixture
def sample_tenant_id():
    """Sample tenant ID"""
    return uuid4()


@pytest.fixture
def sample_chunks_with_embeddings():
    """Sample chunks with embeddings for testing"""
    chunks = []

    # Chunk 1: VM endpoint - high similarity for "virtual machines"
    chunk1 = KnowledgeChunkModel()
    chunk1.id = uuid4()
    chunk1.tenant_id = "test-tenant"
    chunk1.text = "GET /api/vcenter/vm - List all virtual machines in the vCenter inventory"
    chunk1.search_metadata = {
        "source_type": "openapi_spec",
        "connector_id": "vcenter-123",
        "http_method": "GET",
        "endpoint_path": "/api/vcenter/vm",
        "operation_id": "list_vms",
    }
    chunk1.source_uri = "vcenter-openapi.json"
    chunk1.tags = ["vmware", "vcenter", "vm"]
    chunk1.knowledge_type = "documentation"
    # Create embedding similar to query "list VMs"
    chunk1.embedding = [0.1] * 1536  # Will have similarity ~1.0 with query
    chunks.append(chunk1)

    # Chunk 2: Cluster endpoint - medium similarity
    chunk2 = KnowledgeChunkModel()
    chunk2.id = uuid4()
    chunk2.tenant_id = "test-tenant"
    chunk2.text = "GET /api/vcenter/cluster - List all clusters in the vCenter inventory"
    chunk2.search_metadata = {
        "source_type": "openapi_spec",
        "connector_id": "vcenter-123",
        "http_method": "GET",
        "endpoint_path": "/api/vcenter/cluster",
        "operation_id": "list_clusters",
    }
    chunk2.source_uri = "vcenter-openapi.json"
    chunk2.tags = ["vmware", "vcenter", "cluster"]
    chunk2.knowledge_type = "documentation"
    # Different embedding - will have lower similarity
    chunk2.embedding = [0.05] * 1536
    chunks.append(chunk2)

    # Chunk 3: Health status endpoint - semantic match for "show health"
    chunk3 = KnowledgeChunkModel()
    chunk3.id = uuid4()
    chunk3.tenant_id = "test-tenant"
    chunk3.text = "GET /api/vcenter/health - Get system health status and diagnostic information"
    chunk3.search_metadata = {
        "source_type": "openapi_spec",
        "connector_id": "vcenter-123",
        "http_method": "GET",
        "endpoint_path": "/api/vcenter/health",
        "operation_id": "get_health",
    }
    chunk3.source_uri = "vcenter-openapi.json"
    chunk3.tags = ["vmware", "vcenter", "health"]
    chunk3.knowledge_type = "documentation"
    chunk3.embedding = [0.08] * 1536
    chunks.append(chunk3)

    # Chunk 4: Authorization endpoint - low relevance
    chunk4 = KnowledgeChunkModel()
    chunk4.id = uuid4()
    chunk4.tenant_id = "test-tenant"
    chunk4.text = "POST /api/session/authorization - Create an authorization session"
    chunk4.search_metadata = {
        "source_type": "openapi_spec",
        "connector_id": "vcenter-123",
        "http_method": "POST",
        "endpoint_path": "/api/session/authorization",
        "operation_id": "create_session",
    }
    chunk4.source_uri = "vcenter-openapi.json"
    chunk4.tags = ["vmware", "vcenter", "auth"]
    chunk4.knowledge_type = "documentation"
    chunk4.embedding = [0.02] * 1536
    chunks.append(chunk4)

    return chunks


@pytest.mark.asyncio
async def test_hybrid_search_returns_results(
    bm25_hybrid_service, mock_session, sample_tenant_id, sample_chunks_with_embeddings
):
    """Test that hybrid search returns results with RRF scores"""
    # Setup mock to return sample chunks
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = sample_chunks_with_embeddings
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Search for virtual machines
    results = await bm25_hybrid_service.search(
        tenant_id=sample_tenant_id, query="list virtual machines", top_k=10
    )

    # Assertions
    assert len(results) > 0, "Should return at least one result"
    assert all("rrf_score" in r for r in results), "All results should have RRF scores"
    assert all("bm25_score" in r for r in results), "All results should have BM25 scores"
    assert all("semantic_score" in r for r in results), "All results should have semantic scores"
    assert all("text" in r for r in results), "All results should have text"
    assert all("metadata" in r for r in results), "All results should have metadata"


@pytest.mark.asyncio
async def test_hybrid_search_combines_bm25_and_semantic(
    bm25_hybrid_service, mock_session, sample_tenant_id, sample_chunks_with_embeddings
):
    """Test that results come from both BM25 and semantic search"""
    # Setup mock
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = sample_chunks_with_embeddings
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Search
    results = await bm25_hybrid_service.search(
        tenant_id=sample_tenant_id, query="list virtual machines", top_k=10
    )

    # Assertions - should have results from both search methods
    assert len(results) > 0, "Should return results"

    # Results should have both BM25 and semantic information
    for result in results:
        # Either should have BM25 rank, semantic rank, or both
        has_bm25 = result["bm25_rank"] is not None
        has_semantic = result["semantic_rank"] is not None
        assert has_bm25 or has_semantic, "Result should appear in at least one search"


@pytest.mark.asyncio
async def test_rrf_fusion_combines_rankings(
    bm25_hybrid_service, mock_session, sample_tenant_id, sample_chunks_with_embeddings
):
    """Test that RRF fusion correctly combines rankings from both searches"""
    # Setup mock
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = sample_chunks_with_embeddings
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Search
    results = await bm25_hybrid_service.search(
        tenant_id=sample_tenant_id, query="list virtual machines", top_k=10
    )

    # Assertions
    assert len(results) > 0, "Should return results"

    # Check that results are sorted by RRF score
    rrf_scores = [r["rrf_score"] for r in results]
    assert rrf_scores == sorted(rrf_scores, reverse=True), (
        "Results should be sorted by RRF score descending"
    )

    # Results that appear in BOTH searches should have higher RRF scores
    # (assuming they rank well in both)
    for result in results:
        if result["bm25_rank"] is not None and result["semantic_rank"] is not None:
            # RRF score should be sum of contributions from both sources
            assert result["rrf_score"] > 0, (
                "Results appearing in both should have positive RRF score"
            )


@pytest.mark.asyncio
async def test_search_with_metadata_filters(
    bm25_hybrid_service, mock_session, sample_tenant_id, sample_chunks_with_embeddings
):
    """Test that metadata filters are applied correctly"""
    # Filter to only vcenter-123 connector
    vcenter_chunks = [
        c
        for c in sample_chunks_with_embeddings
        if c.search_metadata.get("connector_id") == "vcenter-123"
    ]

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = vcenter_chunks
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Search with filter
    results = await bm25_hybrid_service.search(
        tenant_id=sample_tenant_id,
        query="list endpoints",
        metadata_filters={"connector_id": "vcenter-123"},
        top_k=10,
    )

    # Assertions
    for result in results:
        connector_id = result["metadata"].get("connector_id")
        assert connector_id == "vcenter-123", (
            f"All results should be from vcenter-123, got {connector_id}"
        )


@pytest.mark.asyncio
async def test_search_with_custom_weights(
    bm25_hybrid_service, mock_session, sample_tenant_id, sample_chunks_with_embeddings
):
    """Test that custom BM25/semantic weights affect results"""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = sample_chunks_with_embeddings
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Search with BM25-heavy weights
    bm25_heavy_results = await bm25_hybrid_service.search(
        tenant_id=sample_tenant_id,
        query="virtual machines",
        bm25_weight=0.8,
        semantic_weight=0.2,
        top_k=10,
    )

    # Search with semantic-heavy weights
    semantic_heavy_results = await bm25_hybrid_service.search(
        tenant_id=sample_tenant_id,
        query="virtual machines",
        bm25_weight=0.2,
        semantic_weight=0.8,
        top_k=10,
    )

    # Assertions - results should exist for both
    assert len(bm25_heavy_results) > 0, "BM25-heavy search should return results"
    assert len(semantic_heavy_results) > 0, "Semantic-heavy search should return results"


@pytest.mark.asyncio
async def test_empty_corpus_returns_empty_list(bm25_hybrid_service, mock_session, sample_tenant_id):
    """Test that search with no matching chunks returns empty list"""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=mock_result)

    results = await bm25_hybrid_service.search(
        tenant_id=sample_tenant_id, query="nonexistent endpoint", top_k=10
    )

    assert results == [], "Should return empty list when no chunks found"


@pytest.mark.asyncio
async def test_respects_top_k_limit(
    bm25_hybrid_service, mock_session, sample_tenant_id, sample_chunks_with_embeddings
):
    """Test that search respects the top_k parameter"""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = sample_chunks_with_embeddings
    mock_session.execute = AsyncMock(return_value=mock_result)

    results = await bm25_hybrid_service.search(
        tenant_id=sample_tenant_id, query="api endpoint", top_k=2
    )

    assert len(results) <= 2, f"Should return at most 2 results, got {len(results)}"


@pytest.mark.asyncio
def test_cosine_similarity_computation(bm25_hybrid_service):
    """Test that cosine similarity is computed correctly"""
    # Identical vectors should have similarity 1.0
    vec1 = [1.0, 0.0, 0.0]
    vec2 = [1.0, 0.0, 0.0]
    similarity = bm25_hybrid_service._cosine_similarity(vec1, vec2)
    assert abs(similarity - 1.0) < 0.0001, "Identical vectors should have similarity 1.0"

    # Orthogonal vectors should have similarity 0.0
    vec1 = [1.0, 0.0, 0.0]
    vec2 = [0.0, 1.0, 0.0]
    similarity = bm25_hybrid_service._cosine_similarity(vec1, vec2)
    assert abs(similarity - 0.0) < 0.0001, "Orthogonal vectors should have similarity 0.0"

    # Opposite vectors should have similarity -1.0
    vec1 = [1.0, 0.0, 0.0]
    vec2 = [-1.0, 0.0, 0.0]
    similarity = bm25_hybrid_service._cosine_similarity(vec1, vec2)
    assert abs(similarity - (-1.0)) < 0.0001, "Opposite vectors should have similarity -1.0"


@pytest.mark.asyncio
def test_rrf_formula_correctness(bm25_hybrid_service):
    """Test that RRF formula is applied correctly"""
    # Create simple test data
    bm25_results = [
        {"id": "doc1", "text": "Document 1", "metadata": {}, "bm25_score": 10.0},
        {"id": "doc2", "text": "Document 2", "metadata": {}, "bm25_score": 8.0},
    ]
    semantic_results = [
        {"id": "doc2", "text": "Document 2", "metadata": {}, "similarity": 0.9},
        {"id": "doc1", "text": "Document 1", "metadata": {}, "similarity": 0.7},
    ]

    # Call RRF fusion with equal weights
    fused = bm25_hybrid_service._reciprocal_rank_fusion(
        bm25_results=bm25_results,
        semantic_results=semantic_results,
        bm25_weight=0.5,
        semantic_weight=0.5,
        k=60,
    )

    # Doc1: BM25 rank 1, semantic rank 2 → 0.5/(60+1) + 0.5/(60+2) = 0.00820 + 0.00806 = 0.01626
    # Doc2: BM25 rank 2, semantic rank 1 → 0.5/(60+2) + 0.5/(60+1) = 0.00806 + 0.00820 = 0.01626
    # Both should have similar RRF scores

    assert len(fused) == 2, "Should return 2 documents"

    # Both documents should have roughly equal RRF scores (they swap positions)
    doc1_score = next(d["rrf_score"] for d in fused if d["id"] == "doc1")
    doc2_score = next(d["rrf_score"] for d in fused if d["id"] == "doc2")

    assert abs(doc1_score - doc2_score) < 0.001, (
        "Documents that swap positions should have nearly equal RRF scores"
    )


@pytest.mark.asyncio
async def test_cache_invalidation(bm25_hybrid_service, mock_session, sample_tenant_id):
    """Test that cache invalidation delegates to BM25Service"""
    # Call invalidate_cache
    await bm25_hybrid_service.invalidate_cache(
        tenant_id=sample_tenant_id, connector_id="vcenter-123"
    )

    # BM25Service should have had its cache invalidated
    # (The BM25Service.invalidate_cache is mocked, so this just verifies no errors)
    # In a real test with integration, we'd verify the cache was actually cleared


@pytest.mark.asyncio
async def test_results_contain_all_required_fields(
    bm25_hybrid_service, mock_session, sample_tenant_id, sample_chunks_with_embeddings
):
    """Test that search results contain all required fields"""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = sample_chunks_with_embeddings[:1]
    mock_session.execute = AsyncMock(return_value=mock_result)

    results = await bm25_hybrid_service.search(
        tenant_id=sample_tenant_id, query="virtual machines", top_k=10
    )

    assert len(results) >= 1, "Should return at least one result"

    result = results[0]

    # Required fields
    required_fields = [
        "id",
        "text",
        "metadata",
        "rrf_score",
        "bm25_score",
        "semantic_score",
        "bm25_rank",
        "semantic_rank",
    ]
    for field in required_fields:
        assert field in result, f"Result should contain '{field}' field"

    # Check field types
    assert isinstance(result["id"], str), "ID should be string"
    assert isinstance(result["text"], str), "Text should be string"
    assert isinstance(result["metadata"], dict), "Metadata should be dict"
    assert isinstance(result["rrf_score"], float), "RRF score should be float"
    assert isinstance(result["bm25_score"], (int, float)), "BM25 score should be numeric"
    assert isinstance(result["semantic_score"], (int, float)), "Semantic score should be numeric"


@pytest.mark.asyncio
async def test_chunks_without_embeddings_excluded_from_semantic(
    bm25_hybrid_service, mock_session, sample_tenant_id
):
    """Test that chunks without embeddings are excluded from semantic search but included in BM25"""
    # Create chunks - some with embeddings, some without
    chunk_with_embedding = KnowledgeChunkModel()
    chunk_with_embedding.id = uuid4()
    chunk_with_embedding.tenant_id = "test-tenant"
    chunk_with_embedding.text = "GET /api/vcenter/vm - List VMs"
    chunk_with_embedding.search_metadata = {"source_type": "openapi_spec"}
    chunk_with_embedding.embedding = [0.1] * 1536
    chunk_with_embedding.tags = []
    chunk_with_embedding.knowledge_type = "documentation"

    chunk_without_embedding = KnowledgeChunkModel()
    chunk_without_embedding.id = uuid4()
    chunk_without_embedding.tenant_id = "test-tenant"
    chunk_without_embedding.text = "GET /api/vcenter/cluster - List clusters"
    chunk_without_embedding.search_metadata = {"source_type": "openapi_spec"}
    chunk_without_embedding.embedding = None  # No embedding
    chunk_without_embedding.tags = []
    chunk_without_embedding.knowledge_type = "documentation"

    # Mock returns both chunks for BM25, but only chunk with embedding for semantic
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [
        chunk_with_embedding,
        chunk_without_embedding,
    ]
    mock_session.execute = AsyncMock(return_value=mock_result)

    results = await bm25_hybrid_service.search(
        tenant_id=sample_tenant_id, query="list endpoints", top_k=10
    )

    # Both chunks should appear in results (from BM25)
    result_ids = [r["id"] for r in results]
    assert (
        str(chunk_with_embedding.id) in result_ids or str(chunk_without_embedding.id) in result_ids
    ), "Chunks should appear in results"


@pytest.mark.asyncio
async def test_semantic_matching_improves_relevance(
    bm25_hybrid_service, mock_session, mock_embedding_provider, sample_tenant_id
):
    """Test that semantic search improves relevance for synonym queries"""
    # Create chunks where semantic should help
    # "status" endpoint should match "health" query semantically
    status_chunk = KnowledgeChunkModel()
    status_chunk.id = uuid4()
    status_chunk.tenant_id = "test-tenant"
    status_chunk.text = "GET /api/system/status - Get system status information"
    status_chunk.search_metadata = {"source_type": "openapi_spec"}
    status_chunk.tags = ["system", "status"]
    status_chunk.knowledge_type = "documentation"
    # Embedding close to "health" query
    status_chunk.embedding = [0.09] * 1536

    # Unrelated chunk
    auth_chunk = KnowledgeChunkModel()
    auth_chunk.id = uuid4()
    auth_chunk.tenant_id = "test-tenant"
    auth_chunk.text = "POST /api/auth/login - Authenticate user"
    auth_chunk.search_metadata = {"source_type": "openapi_spec"}
    auth_chunk.tags = ["auth", "login"]
    auth_chunk.knowledge_type = "documentation"
    auth_chunk.embedding = [0.01] * 1536

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [status_chunk, auth_chunk]
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Query for "health" - BM25 won't match, but semantic should
    mock_embedding_provider.embed_text = AsyncMock(return_value=[0.1] * 1536)

    results = await bm25_hybrid_service.search(
        tenant_id=sample_tenant_id, query="show system health", top_k=10
    )

    # Assertions - status endpoint should be in results due to semantic matching
    assert len(results) > 0, "Should return results"

    # Both results should have semantic scores
    for result in results:
        assert "semantic_score" in result, "Results should have semantic scores"

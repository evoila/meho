# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for BM25Service (on-the-fly BM25 search).

Tests the on-the-fly BM25 search functionality for endpoint search.
"""

import base64
import json
import pickle
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from meho_app.modules.knowledge.bm25_service import BM25Service
from meho_app.modules.knowledge.models import KnowledgeChunkModel


@pytest.fixture
def mock_session():
    """Create mock database session"""
    return AsyncMock()


@pytest.fixture
def bm25_service(mock_session):
    """Create BM25Service with mock session"""
    return BM25Service(mock_session)


@pytest.fixture
def sample_tenant_id():
    """Sample tenant ID"""
    return uuid4()


@pytest.fixture
def sample_endpoint_chunks():
    """Sample endpoint chunks for testing"""
    chunks = []

    # Chunk 1: VM endpoint
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
    chunks.append(chunk1)

    # Chunk 2: Cluster endpoint
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
    chunks.append(chunk2)

    # Chunk 3: Authorization endpoint (should rank lower for VM query)
    chunk3 = KnowledgeChunkModel()
    chunk3.id = uuid4()
    chunk3.tenant_id = "test-tenant"
    chunk3.text = "POST /api/session/authorization - Create an authorization session"
    chunk3.search_metadata = {
        "source_type": "openapi_spec",
        "connector_id": "vcenter-123",
        "http_method": "POST",
        "endpoint_path": "/api/session/authorization",
        "operation_id": "create_session",
    }
    chunk3.source_uri = "vcenter-openapi.json"
    chunk3.tags = ["vmware", "vcenter", "auth"]
    chunk3.knowledge_type = "documentation"
    chunks.append(chunk3)

    # Chunk 4: Different connector (should be filtered out)
    chunk4 = KnowledgeChunkModel()
    chunk4.id = uuid4()
    chunk4.tenant_id = "test-tenant"
    chunk4.text = "GET /api/v1/pods - List all pods in the Kubernetes cluster"
    chunk4.search_metadata = {
        "source_type": "openapi_spec",
        "connector_id": "k8s-456",
        "http_method": "GET",
        "endpoint_path": "/api/v1/pods",
        "operation_id": "list_pods",
    }
    chunk4.source_uri = "k8s-openapi.json"
    chunk4.tags = ["kubernetes", "k8s", "pods"]
    chunk4.knowledge_type = "documentation"
    chunks.append(chunk4)

    return chunks


@pytest.mark.asyncio
async def test_basic_search_returns_results(
    bm25_service, mock_session, sample_tenant_id, sample_endpoint_chunks
):
    """Test that basic search returns results sorted by relevance"""
    # Setup mock to return sample chunks
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = sample_endpoint_chunks[:3]  # First 3 chunks
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Search for virtual machines
    results = await bm25_service.search(
        tenant_id=sample_tenant_id, query="list virtual machines", top_k=10
    )

    # Assertions
    assert len(results) > 0, "Should return at least one result"
    assert all("bm25_score" in r for r in results), "All results should have BM25 scores"
    assert all("text" in r for r in results), "All results should have text"
    assert all("metadata" in r for r in results), "All results should have metadata"

    # Check that VM endpoint is ranked highest
    top_result = results[0]
    assert "virtual machine" in top_result["text"].lower() or "vm" in top_result["text"].lower(), (
        "Top result should be VM-related"
    )

    # Check that results are sorted by score (descending)
    scores = [r["bm25_score"] for r in results]
    assert scores == sorted(scores, reverse=True), "Results should be sorted by score descending"


@pytest.mark.asyncio
async def test_search_with_metadata_filters(
    bm25_service, mock_session, sample_tenant_id, sample_endpoint_chunks
):
    """Test that metadata filters correctly filter results by connector"""
    # Setup mock - only return chunks for vcenter-123 connector
    vcenter_chunks = [
        c for c in sample_endpoint_chunks if c.search_metadata.get("connector_id") == "vcenter-123"
    ]
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = vcenter_chunks
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Search with connector filter
    results = await bm25_service.search(
        tenant_id=sample_tenant_id,
        query="list pods",
        metadata_filters={"connector_id": "vcenter-123"},
        top_k=10,
    )

    # Assertions
    assert len(results) == 3, "Should return exactly 3 vCenter endpoints"

    # Verify all results are from vcenter-123 connector
    for result in results:
        connector_id = result["metadata"].get("connector_id")
        assert connector_id == "vcenter-123", (
            f"All results should be from vcenter-123 connector, got {connector_id}"
        )

    # Verify no Kubernetes endpoints are returned
    for result in results:
        assert "kubernetes" not in result["text"].lower(), "Should not return Kubernetes endpoints"
        assert "pods" not in result["text"].lower() or "vcenter" in result["text"].lower(), (
            "Should not return K8s pods endpoint"
        )


@pytest.mark.asyncio
async def test_search_with_empty_corpus_returns_empty_list(
    bm25_service, mock_session, sample_tenant_id
):
    """Test that search with no matching chunks returns empty list"""
    # Setup mock to return empty list
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Search
    results = await bm25_service.search(
        tenant_id=sample_tenant_id, query="nonexistent endpoint", top_k=10
    )

    # Assertions
    assert results == [], "Should return empty list when no chunks found"


@pytest.mark.asyncio
async def test_bm25_scoring_prioritizes_keyword_matches(
    bm25_service, mock_session, sample_tenant_id, sample_endpoint_chunks
):
    """Test that BM25 correctly scores keyword matches higher"""
    # Setup mock to return sample chunks
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = sample_endpoint_chunks[:3]
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Search for "virtual machines" - should rank VM endpoint highest
    results = await bm25_service.search(
        tenant_id=sample_tenant_id, query="virtual machines", top_k=10
    )

    # Assertions
    assert len(results) > 0, "Should return results"

    # Top result should be VM endpoint
    top_result = results[0]
    assert (
        "/api/vcenter/vm" in top_result["text"] or "virtual machine" in top_result["text"].lower()
    ), f"Top result should be VM endpoint, got: {top_result['text']}"

    # VM endpoint should have higher score than authorization endpoint
    vm_score = next((r["bm25_score"] for r in results if "vm" in r["text"].lower()), 0)
    auth_score = next((r["bm25_score"] for r in results if "authorization" in r["text"].lower()), 0)

    assert vm_score > auth_score, (
        f"VM endpoint (score: {vm_score}) should score higher than auth endpoint (score: {auth_score})"
    )


@pytest.mark.asyncio
async def test_tokenization_handles_mixed_case_and_special_chars(bm25_service, sample_tenant_id):
    """Test that tokenization correctly handles various input formats"""
    # Test tokenization directly
    test_cases = [
        ("GET /api/vcenter/vm", ["get", "/api/vcenter/vm"]),
        ("List Virtual Machines", ["list", "virtual", "machin"]),  # stemmed: machines -> machin
        ("VMware vCenter API", ["vmware", "vcenter", "api"]),
        ("cluster-management", ["cluster-manag"]),  # stemmed: cluster-management -> cluster-manag
    ]

    for input_text, expected_tokens in test_cases:
        tokens = bm25_service._tokenize(input_text)
        assert tokens == expected_tokens, (
            f"Tokenization of '{input_text}' should produce {expected_tokens}, got {tokens}"
        )


@pytest.mark.asyncio
async def test_search_returns_all_required_fields(
    bm25_service, mock_session, sample_tenant_id, sample_endpoint_chunks
):
    """Test that search results contain all required fields"""
    # Setup mock
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = sample_endpoint_chunks[:1]
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Search
    results = await bm25_service.search(
        tenant_id=sample_tenant_id, query="virtual machines", top_k=10
    )

    # Assertions
    assert len(results) == 1, "Should return one result"

    result = results[0]

    # Required fields
    required_fields = [
        "id",
        "text",
        "metadata",
        "source_uri",
        "bm25_score",
        "tags",
        "knowledge_type",
    ]
    for field in required_fields:
        assert field in result, f"Result should contain '{field}' field"

    # Check field types
    assert isinstance(result["id"], str), "ID should be string (UUID)"
    assert isinstance(result["text"], str), "Text should be string"
    assert isinstance(result["metadata"], dict), "Metadata should be dict"
    assert isinstance(result["bm25_score"], float), "BM25 score should be float"
    assert isinstance(result["tags"], list), "Tags should be list"
    assert isinstance(result["knowledge_type"], str), "Knowledge type should be string"


@pytest.mark.asyncio
async def test_search_respects_top_k_limit(
    bm25_service, mock_session, sample_tenant_id, sample_endpoint_chunks
):
    """Test that search respects the top_k parameter"""
    # Setup mock to return all chunks
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = sample_endpoint_chunks
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Search with top_k=2
    results = await bm25_service.search(tenant_id=sample_tenant_id, query="api endpoint", top_k=2)

    # Assertions
    assert len(results) <= 2, f"Should return at most 2 results, got {len(results)}"

    # Verify these are the top 2 by score
    if len(results) == 2:
        assert results[0]["bm25_score"] >= results[1]["bm25_score"], (
            "Results should be sorted by score"
        )


@pytest.mark.asyncio
async def test_performance_with_large_corpus(bm25_service, mock_session, sample_tenant_id):
    """Test that BM25 performs well with ~500 endpoints (realistic connector size)"""
    # Create 500 synthetic endpoint chunks
    large_corpus = []
    for i in range(500):
        chunk = KnowledgeChunkModel()
        chunk.id = uuid4()
        chunk.tenant_id = "test-tenant"
        chunk.text = f"GET /api/resource/{i} - Endpoint {i} for resource management"
        chunk.search_metadata = {
            "source_type": "openapi_spec",
            "connector_id": "large-connector",
            "http_method": "GET",
            "endpoint_path": f"/api/resource/{i}",
            "operation_id": f"get_resource_{i}",
        }
        chunk.source_uri = "large-openapi.json"
        chunk.tags = ["api", "resource"]
        chunk.knowledge_type = "documentation"
        large_corpus.append(chunk)

    # Add one special "virtual machines" endpoint
    vm_chunk = KnowledgeChunkModel()
    vm_chunk.id = uuid4()
    vm_chunk.tenant_id = "test-tenant"
    vm_chunk.text = "GET /api/vcenter/vm - List all virtual machines"
    vm_chunk.search_metadata = {
        "source_type": "openapi_spec",
        "connector_id": "large-connector",
        "http_method": "GET",
        "endpoint_path": "/api/vcenter/vm",
        "operation_id": "list_vms",
    }
    vm_chunk.source_uri = "large-openapi.json"
    vm_chunk.tags = ["vmware", "vm"]
    vm_chunk.knowledge_type = "documentation"
    large_corpus.append(vm_chunk)

    # Setup mock
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = large_corpus
    mock_session.execute = AsyncMock(return_value=mock_result)

    # Search - should complete quickly (< 50ms in production)
    import time

    start = time.time()
    results = await bm25_service.search(
        tenant_id=sample_tenant_id, query="list virtual machines", top_k=10
    )
    duration = time.time() - start

    # Assertions
    assert len(results) > 0, "Should return results"
    assert duration < 1.0, (
        f"Search should complete quickly even with 500 docs, took {duration:.3f}s"
    )

    # VM endpoint should be in top results
    top_5_texts = [r["text"] for r in results[:5]]
    assert any("virtual machine" in text.lower() for text in top_5_texts), (
        "VM endpoint should be in top 5 results"
    )


# === JSON Caching Tests (Task 1: pickle -> JSON migration) ===


@pytest.fixture
def mock_redis():
    """Create mock Redis client for caching tests."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    redis.delete = AsyncMock()
    return redis


@pytest.fixture
def bm25_service_with_redis(mock_session, mock_redis):
    """Create BM25Service with mock session and Redis."""
    return BM25Service(mock_session, redis=mock_redis)


@pytest.mark.asyncio
async def test_cache_corpus_uses_json_serialization(bm25_service_with_redis, mock_redis):
    """Test that _cache_corpus serializes tokenized corpus using JSON, not pickle."""
    tokenized_corpus = [["list", "virtual", "machin"], ["get", "pod"]]
    cache_key = "bm25:test:connector:abc123"

    await bm25_service_with_redis._cache_corpus(cache_key, tokenized_corpus)

    # Verify Redis was called with setex
    mock_redis.setex.assert_called_once()
    call_args = mock_redis.setex.call_args
    stored_value = call_args[0][2]  # third positional arg is the value

    # Decode the stored base64 string back to bytes, then to data
    decoded_bytes = base64.b64decode(stored_value)
    decoded_data = json.loads(decoded_bytes)

    assert decoded_data == tokenized_corpus, (
        f"Cached data should roundtrip via JSON. Got {decoded_data}"
    )


@pytest.mark.asyncio
async def test_get_cached_corpus_uses_json_deserialization(bm25_service_with_redis, mock_redis):
    """Test that _get_cached_corpus deserializes using JSON, not pickle."""
    tokenized_corpus = [["list", "virtual", "machin"], ["get", "pod"]]

    # Prepare JSON-encoded base64 string (what JSON caching would store)
    json_bytes = json.dumps(tokenized_corpus).encode("utf-8")
    cached_str = base64.b64encode(json_bytes).decode("ascii")
    mock_redis.get = AsyncMock(return_value=cached_str)

    result = await bm25_service_with_redis._get_cached_corpus("bm25:test:connector:abc123")

    assert result == tokenized_corpus, (
        f"Should correctly deserialize JSON-cached corpus. Got {result}"
    )


@pytest.mark.asyncio
async def test_old_pickle_cache_is_gracefully_invalidated(bm25_service_with_redis, mock_redis):
    """Test that old pickle-encoded cache entries are handled gracefully (cache miss, not crash)."""
    tokenized_corpus = [["list", "virtual", "machin"], ["get", "pod"]]

    # Prepare PICKLE-encoded base64 string (old format)
    pickle_bytes = pickle.dumps(tokenized_corpus)
    cached_str = base64.b64encode(pickle_bytes).decode("ascii")
    mock_redis.get = AsyncMock(return_value=cached_str)

    # Should NOT crash -- should return None (cache miss) and invalidate the entry
    result = await bm25_service_with_redis._get_cached_corpus("bm25:test:old-pickle:abc123")

    assert result is None, "Old pickle cache should be treated as corrupted and return None"
    # Verify the corrupted entry was deleted
    mock_redis.delete.assert_called_once_with("bm25:test:old-pickle:abc123")


@pytest.mark.asyncio
async def test_bm25_search_roundtrip_with_caching(
    bm25_service_with_redis, mock_session, mock_redis, sample_tenant_id, sample_endpoint_chunks
):
    """Test that BM25 search results are identical whether cache is cold or warm."""
    # Setup mock to return sample chunks
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = sample_endpoint_chunks[:3]
    mock_session.execute = AsyncMock(return_value=mock_result)

    # First search (cold cache)
    mock_redis.get = AsyncMock(return_value=None)
    results_cold = await bm25_service_with_redis.search(
        tenant_id=sample_tenant_id, query="list virtual machines", top_k=10
    )

    # Capture what was cached
    assert mock_redis.setex.called, "Should have cached the corpus"
    cached_value = mock_redis.setex.call_args[0][2]

    # Second search (warm cache -- return the cached value)
    mock_redis.get = AsyncMock(return_value=cached_value)
    mock_session.execute = AsyncMock(return_value=mock_result)
    results_warm = await bm25_service_with_redis.search(
        tenant_id=sample_tenant_id, query="list virtual machines", top_k=10
    )

    # Results should be identical
    assert len(results_cold) == len(results_warm), "Cold and warm cache should return same count"
    for cold, warm in zip(results_cold, results_warm, strict=True):
        assert cold["id"] == warm["id"], "Same documents should be returned"
        assert cold["bm25_score"] == warm["bm25_score"], (
            f"Scores should match: cold={cold['bm25_score']} warm={warm['bm25_score']}"
        )


@pytest.mark.asyncio
async def test_no_pickle_import_in_bm25_service():
    """Test that pickle is not imported in the bm25_service module."""
    import meho_app.modules.knowledge.bm25_service as mod
    import importlib
    importlib.reload(mod)

    source_file = mod.__file__
    with open(source_file) as f:
        source_code = f.read()

    assert "import pickle" not in source_code, (
        "bm25_service.py should not import pickle (security: RCE vector)"
    )
    assert "pickle.loads" not in source_code, (
        "bm25_service.py should not use pickle.loads"
    )
    assert "pickle.dumps" not in source_code, (
        "bm25_service.py should not use pickle.dumps"
    )

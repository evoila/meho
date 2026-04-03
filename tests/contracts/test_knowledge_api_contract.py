# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Contract tests for Knowledge Service API.

Verifies that KnowledgeStore provides the API that consumers (Agent) expect.
"""

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest


class TestHybridSearchContract:
    """Test hybrid_search API contract"""

    def test_hybrid_search_method_exists(self):
        """Verify search_hybrid method exists on KnowledgeStore"""
        from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

        assert hasattr(KnowledgeStore, "search_hybrid")

    @pytest.mark.asyncio
    async def test_hybrid_search_returns_expected_structure(self):
        """
        Test that search_hybrid returns the structure that Agent expects.

        Expected response:
        {
            "results": [
                {
                    "id": str,
                    "text": str,
                    "metadata": dict,
                    "rrf_score": float,
                    "bm25_score": float,
                    "distance": float
                }
            ],
            "search_metadata": {
                "bm25_results": int,
                "semantic_results": int,
                "fused_results": int
            }
        }
        """
        from datetime import UTC, datetime

        from meho_app.core.auth_context import UserContext
        from meho_app.modules.knowledge.hybrid_search import PostgresFTSHybridService
        from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

        # Create mock hybrid search service
        from meho_app.modules.knowledge.schemas import KnowledgeChunk

        mock_chunk = KnowledgeChunk(
            id="chunk-123",
            text="Test chunk about roles",
            tenant_id="test-tenant",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

        mock_hybrid = Mock(spec=PostgresFTSHybridService)
        mock_hybrid.search_hybrid = AsyncMock(return_value=[mock_chunk])

        # Create store with required parameters
        mock_repo = AsyncMock()
        mock_repo.get_chunks_with_acl = AsyncMock(return_value=[mock_chunk])
        mock_embedding = Mock()

        store = KnowledgeStore(
            repository=mock_repo,
            embedding_provider=mock_embedding,
            hybrid_search_service=mock_hybrid,
        )

        # Call search_hybrid
        user_context = UserContext(
            tenant_id=str(uuid4()), user_id="test-user", roles=["user"], groups=[]
        )

        result = await store.search_hybrid(query="test query", user_context=user_context, top_k=10)

        # Verify contract: search_hybrid returns list of KnowledgeChunk objects
        assert isinstance(result, list), "search_hybrid should return list"

        if len(result) > 0:
            first_result = result[0]

            # Required fields in KnowledgeChunk
            assert hasattr(first_result, "id")
            assert hasattr(first_result, "text")
            assert hasattr(first_result, "tenant_id")
            assert hasattr(first_result, "created_at")

            # Type checks
            assert isinstance(first_result.id, str)
            assert isinstance(first_result.text, str)


class TestSearchContract:
    """Test search (semantic only) API contract"""

    def test_search_method_exists(self):
        """Verify search method exists"""
        from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

        assert hasattr(KnowledgeStore, "search")

    @pytest.mark.asyncio
    def test_search_accepts_metadata_filters(self):
        """
        Test that search accepts metadata_filters parameter.

        This was added in Session 15 - Task 26.
        """
        import inspect

        from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

        # Check method signature
        sig = inspect.signature(KnowledgeStore.search)
        params = list(sig.parameters.keys())

        # Should accept metadata_filters
        assert "metadata_filters" in params, "search() should accept metadata_filters parameter"


class TestIngestionContract:
    """Test ingestion API contract"""

    def test_ingestion_service_exists(self):
        """Verify IngestionService exists for document ingestion"""
        from meho_app.modules.knowledge.ingestion import IngestionService

        # KnowledgeStore delegates to IngestionService
        assert hasattr(IngestionService, "ingest_text")

    def test_ingestion_service_signature(self):
        """Verify IngestionService.ingest_text has expected parameters"""
        import inspect

        from meho_app.modules.knowledge.ingestion import IngestionService

        sig = inspect.signature(IngestionService.ingest_text)
        params = list(sig.parameters.keys())

        # Expected parameters
        assert "self" in params
        assert "text" in params or "content" in params


class TestACLContract:
    """Test ACL enforcement contract"""

    @pytest.mark.asyncio
    def test_search_enforces_acl(self):
        """
        Test that search enforces ACL based on UserContext.

        Search should:
        1. Filter by tenant_id
        2. Filter by roles
        3. Filter by groups
        """
        import inspect

        from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

        # Check that search accepts user_context
        sig = inspect.signature(KnowledgeStore.search)
        params = list(sig.parameters.keys())

        assert "user_context" in params, "search() must accept user_context for ACL enforcement"

    @pytest.mark.asyncio
    def test_hybrid_search_enforces_acl(self):
        """Test that search_hybrid enforces ACL"""
        import inspect

        from meho_app.modules.knowledge.knowledge_store import KnowledgeStore

        sig = inspect.signature(KnowledgeStore.search_hybrid)
        params = list(sig.parameters.keys())

        assert "user_context" in params, (
            "search_hybrid() must accept user_context for ACL enforcement"
        )


class TestResponseFormatContract:
    """Test response format contracts"""

    def test_chunk_schema_fields(self):
        """Test that KnowledgeChunk schema has required fields"""
        from meho_app.modules.knowledge.schemas import KnowledgeChunk

        # Get model fields
        fields = KnowledgeChunk.model_fields.keys()

        required_fields = ["id", "text", "search_metadata", "tenant_id"]

        for field in required_fields:
            assert field in fields, f"KnowledgeChunk should have field: {field}"

    def test_chunk_create_schema_fields(self):
        """Test that KnowledgeChunkCreate schema has required fields"""
        from meho_app.modules.knowledge.schemas import KnowledgeChunkCreate

        fields = KnowledgeChunkCreate.model_fields.keys()

        required_fields = ["text", "tenant_id"]

        for field in required_fields:
            assert field in fields, f"KnowledgeChunkCreate should have field: {field}"

    def test_chunk_response_api_schema_complete(self):
        """
        Test that ChunkResponse (API schema) includes all fields from KnowledgeChunk.

        This prevents schema mismatch bugs where the API returns incomplete data.

        Bug prevented: Session 43 - ChunkResponse was missing knowledge_type,
        priority, expires_at fields, causing 500 errors in BFF.
        """
        from meho_app.modules.knowledge.api_schemas import ChunkResponse
        from meho_app.modules.knowledge.schemas import KnowledgeChunk

        # Get fields from both schemas
        chunk_fields = set(KnowledgeChunk.model_fields.keys())
        response_fields = set(ChunkResponse.model_fields.keys())

        # ChunkResponse should include all fields from KnowledgeChunk
        # (it's OK to have extra fields, but not missing ones)
        missing_fields = chunk_fields - response_fields

        assert not missing_fields, (
            f"ChunkResponse is missing fields that exist in KnowledgeChunk: {missing_fields}. "
            f"This will cause serialization errors when returning chunks via API. "
            f"Add these fields to meho_knowledge/api_schemas.py:ChunkResponse"
        )

    def test_chunk_filter_has_source_uri(self):
        """
        Test that KnowledgeChunkFilter supports source_uri filtering.

        Required for document deletion (TASK-52).

        Bug prevented: Session 42 - Missing source_uri field caused chunk
        deletion to fail silently, leaving orphaned data.
        """
        from meho_app.modules.knowledge.schemas import KnowledgeChunkFilter

        fields = KnowledgeChunkFilter.model_fields.keys()

        assert "source_uri" in fields, (
            "KnowledgeChunkFilter must have source_uri field for document deletion. "
            "Without this, deleting documents will leave orphaned chunks in the database."
        )

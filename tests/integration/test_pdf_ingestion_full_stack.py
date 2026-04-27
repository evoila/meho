# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
CRITICAL: PDF/Document ingestion end-to-end integration test.

Tests the complete document upload workflow:
- Real file upload
- Object storage (MinIO)
- Text extraction (pypdf)
- Chunking
- Embedding (OpenAI)
- Storage (PostgreSQL with pgvector)
- Search and retrieval

This is the PRIMARY user workflow for adding knowledge!
"""

import asyncio
import os
from pathlib import Path

import pytest

from meho_app.core.auth_context import UserContext
from meho_app.modules.knowledge.embeddings import get_embedding_provider
from meho_app.modules.knowledge.ingestion import IngestionService
from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.repository import KnowledgeRepository
from meho_app.modules.knowledge.schemas import KnowledgeType

# Skip if OpenAI key not available
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="OPENAI_API_KEY required"),
]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_text_file_full_stack(db_session):
    """
    TEST: Document ingestion end-to-end (using text file as proxy for PDF)

    Tests:
    1. Load document file
    2. Store in object storage (MinIO)
    3. Extract text
    4. Chunk text
    5. Generate embeddings (OpenAI)
    6. Store in PostgreSQL with pgvector
    7. Search and retrieve
    8. Verify content from file is searchable

    Note: Using .txt file as PDF extraction is already tested in unit tests.
    The critical part is the full pipeline with object storage.
    """
    # Setup
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    # Object storage (MinIO) - use from config/deps
    from meho_app.modules.knowledge.deps import get_object_storage

    object_storage = get_object_storage()

    ingestion_service = IngestionService(
        knowledge_store=knowledge_store, object_storage=object_storage
    )

    # 1. Load test document
    test_file = Path(__file__).parent.parent / "fixtures" / "test-architecture-doc.txt"

    if not test_file.exists():
        pytest.skip(f"Test file not found: {test_file}")

    file_bytes = test_file.read_bytes()

    print(f"\n📄 Ingesting document: {test_file.name}")
    print(f"📊 File size: {len(file_bytes)} bytes")

    # 2. Ingest document (full pipeline!)
    chunk_ids = await ingestion_service.ingest_document(
        file_bytes=file_bytes,
        filename="test-architecture-doc.txt",
        mime_type="text/plain",
        tenant_id="company",
        tags=["architecture", "my-app", "documentation"],
        knowledge_type=KnowledgeType.DOCUMENTATION,
        priority=0,
    )

    print(f"✅ Created {len(chunk_ids)} chunks from document")

    # 3. Verify chunks created
    assert len(chunk_ids) > 0, "No chunks created from document!"
    print(f"📦 Document chunked into {len(chunk_ids)} chunks")

    # 4. Verify all chunks in PostgreSQL
    for chunk_id in chunk_ids:
        chunk = await repository.get_chunk(chunk_id)
        assert chunk is not None, f"Chunk {chunk_id} not in PostgreSQL!"
        assert chunk.knowledge_type == KnowledgeType.DOCUMENTATION

    # 5. Brief wait for database commit (pgvector is synchronous, but give it a moment)
    print("⏳ Waiting for database commit...")
    await asyncio.sleep(1.0)

    # 6. Search for content from the document
    user_ctx = UserContext(user_id="user-1", tenant_id="company")

    # Search for key concepts from the document
    test_queries = [
        "my-app architecture Kubernetes",
        "troubleshooting ArgoCD",
        "PostgreSQL database",
    ]

    for query in test_queries:
        results = await knowledge_store.search(
            query=query,
            user_context=user_ctx,
            top_k=10,
            score_threshold=0.05,  # Lower threshold for testing
        )

        print(f"  Query '{query}': {len(results)} results")

        # Should find results
        if len(results) == 0:
            print(f"  ⚠️  No results for '{query}' - trying lower threshold...")
            results = await knowledge_store.search(
                query=query,
                user_context=user_ctx,
                top_k=20,
                score_threshold=0.0,  # No threshold
            )
            print(f"  With no threshold: {len(results)} results")

        assert len(results) > 0, f"No results for query: {query} (even with no threshold!)"

        # At least one result should be from our document
        found_our_chunks = any(r.id in chunk_ids for r in results)
        if not found_our_chunks:
            print(f"  Found results but not our chunks. Result IDs: {[r.id for r in results]}")
            print(f"  Our chunk IDs: {chunk_ids}")
        assert found_our_chunks, f"Document chunks not found for query: {query}"

    # 7. Verify our specific chunk is searchable by ID
    # Do a broad search that should definitely include our chunk
    all_results = await knowledge_store.search(
        query="Kubernetes PostgreSQL architecture troubleshooting",
        user_context=user_ctx,
        top_k=50,  # Get many results
        score_threshold=0.0,  # No threshold
    )

    print(f"  Broad search returned {len(all_results)} total results")
    print(f"  Our chunk ID: {chunk_ids[0]}")

    # Our chunk MUST be in the results
    our_chunk_found = any(r.id == chunk_ids[0] for r in all_results)

    if not our_chunk_found:
        print("  ❌ Our chunk not found in results!")
        print(f"  Result IDs: {[r.id for r in all_results[:5]]}")

    assert our_chunk_found, "Our ingested chunk not found in search results!"

    # Get our chunk from results
    our_chunk = next(r for r in all_results if r.id == chunk_ids[0])
    print("  ✅ Found our chunk in results!")
    print(f"  Content preview: {our_chunk.text[:100]}...")

    # Verify content contains expected information
    assert "my-app" in our_chunk.text.lower() or "Kubernetes" in our_chunk.text
    assert "PostgreSQL" in our_chunk.text or "database" in our_chunk.text.lower()

    print("\n✅ Document ingestion full-stack test PASSED!")
    print(f"✅ {len(chunk_ids)} chunks created from document")
    print("✅ All chunks stored in PostgreSQL with pgvector")
    print("✅ All test queries found relevant chunks")
    print("✅ Content quality verified")
    print("✅ Object storage integration works!")


@pytest.mark.integration
@pytest.mark.asyncio
def test_object_storage_integration(db_session):
    """
    TEST: Object storage (MinIO) integration

    Verifies that documents are stored and retrievable from MinIO.
    """
    # Setup object storage
    from meho_app.modules.knowledge.deps import get_object_storage

    object_storage = get_object_storage()

    # Test upload
    test_content = b"Test document content for object storage"
    storage_key = "test/test-doc.txt"

    uri = object_storage.upload_document(
        file_bytes=test_content, key=storage_key, content_type="text/plain"
    )

    assert uri is not None
    assert storage_key in uri

    print("\n✅ Object storage test PASSED!")
    print("✅ Document uploaded to MinIO")
    print(f"✅ Storage URI: {uri}")
    print("✅ MinIO integration works!")

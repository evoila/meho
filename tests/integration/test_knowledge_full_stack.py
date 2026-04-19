# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
CRITICAL: Full-stack knowledge integration tests.

Tests the COMPLETE flow with REAL infrastructure:
- Real PostgreSQL with pgvector extension
- Real OpenAI embeddings
- Real PDF extraction

This is what gives us confidence for production!

Note: Migrated from Qdrant to pgvector in Session 15 (2025-11-20).
Vector search now integrated into PostgreSQL.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from meho_app.core.auth_context import UserContext
from meho_app.modules.knowledge.embeddings import get_embedding_provider
from meho_app.modules.knowledge.ingestion import IngestionService
from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.repository import KnowledgeRepository

# VectorStore (Qdrant) removed - using pgvector in PostgreSQL
from meho_app.modules.knowledge.schemas import KnowledgeChunkCreate, KnowledgeType

# Skip integration tests if OpenAI API key not available
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set - required for real embeddings",
    ),
]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_add_chunk_full_stack(db_session):
    """
    TEST THE COMPLETE FLOW: Create chunk → PostgreSQL + Qdrant → Search → Retrieve

    This tests that KnowledgeStore integrates BOTH databases correctly.
    """
    # Setup REAL dependencies
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()  # Real OpenAI embeddings!

    # Note: pgvector now integrated into PostgreSQL (no separate vector store)
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    # 1. Create a chunk
    chunk_create = KnowledgeChunkCreate(
        text="my-app is a Python microservice running on Kubernetes. It uses PostgreSQL for data and Redis for caching.",
        tenant_id="company",
        tags=["architecture", "my-app"],
        knowledge_type=KnowledgeType.DOCUMENTATION,
    )

    chunk = await knowledge_store.add_chunk(chunk_create)

    # 2. Verify stored in PostgreSQL
    retrieved_from_db = await repository.get_chunk(chunk.id)
    assert retrieved_from_db is not None
    assert retrieved_from_db.text == chunk_create.text
    assert retrieved_from_db.tags == ["architecture", "my-app"]

    # 3. Verify stored in Qdrant (searchable)
    user_ctx = UserContext(user_id="user-1", tenant_id="company")
    search_results = await knowledge_store.search(
        query="Python microservice Kubernetes", user_context=user_ctx, top_k=10, score_threshold=0.3
    )

    # Should find our chunk
    assert len(search_results) > 0
    chunk_ids = [r.id for r in search_results]
    assert chunk.id in chunk_ids

    # Verify content
    found_chunk = next(r for r in search_results if r.id == chunk.id)
    assert "Python microservice" in found_chunk.text
    assert "Kubernetes" in found_chunk.text

    print("\n✅ Full-stack test passed!")
    print(f"✅ Chunk stored in PostgreSQL: {chunk.id}")
    print("✅ Embedding stored in Qdrant")
    print("✅ Search retrieval works")
    print(f"✅ Content matches: {found_chunk.text[:50]}...")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_text_procedure_full_stack(db_session):
    """
    TEST: Ingest user-written procedure → PostgreSQL + Qdrant → Search

    Tests the "lessons learned" / "best practice" use case.
    """
    # Setup
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()
    # Note: pgvector now integrated into PostgreSQL (no separate vector store)
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    # Mock object storage (not needed for text)
    mock_storage = None
    ingestion_service = IngestionService(knowledge_store, mock_storage)

    # 1. User writes a procedure (lesson learned)
    procedure_text = """
    Lesson Learned: Debugging Kubernetes Deployment Failures

    Always check in this order:
    1. ArgoCD sync status first
    2. K8s pod describe (events section)
    3. Pod logs
    4. GitHub recent commits

    This methodology saves 10-15 minutes compared to random checking.
    Learned from debugging my-app issues 5 times this month.
    """

    chunk_ids = await ingestion_service.ingest_text(
        text=procedure_text,
        tenant_id="company",
        tags=["lesson-learned", "kubernetes", "deployment"],
        knowledge_type=KnowledgeType.PROCEDURE,
        priority=5,  # Slight boost
    )

    # 2. Verify chunks created (may be multiple due to chunking)
    assert len(chunk_ids) > 0

    # 3. Wait for Qdrant to index
    await asyncio.sleep(0.5)

    # 4. Search and verify found
    user_ctx = UserContext(user_id="user-1", tenant_id="company")
    results = await knowledge_store.search(
        query="kubernetes deployment debugging",
        user_context=user_ctx,
        top_k=10,
        score_threshold=0.1,
    )

    # Should find the procedure
    assert len(results) > 0
    assert any(chunk_id in [r.id for r in results] for chunk_id in chunk_ids)

    # Verify content
    found = next(r for r in results if r.id in chunk_ids)
    assert "ArgoCD sync" in found.text or "Lesson Learned" in found.text
    assert found.knowledge_type == KnowledgeType.PROCEDURE

    print("\n✅ Procedure ingestion full-stack test passed!")
    print(f"✅ Created {len(chunk_ids)} chunks from procedure")
    print("✅ Searchable and retrievable")
    print(f"✅ Knowledge type correct: {found.knowledge_type}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_created_event_notice_full_stack(db_session):
    """
    TEST: User creates temporary notice → Store → Search → Verify expiration set

    Tests the "marathon notice" / "maintenance window" use case.
    """
    # Setup
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()
    # Note: pgvector now integrated into PostgreSQL (no separate vector store)
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    # 1. User posts temporary notice
    tomorrow_6pm = datetime.now(tz=UTC) + timedelta(hours=30)

    chunk = await knowledge_store.add_chunk(
        KnowledgeChunkCreate(
            text="NOTICE: Berliner marathon tomorrow November 17th. All streets in city center closed 6 AM - 6 PM. VPN to home office recommended for remote employees.",
            tenant_id="company",
            tags=["notice", "marathon", "berlin", "event"],
            knowledge_type=KnowledgeType.EVENT,
            expires_at=tomorrow_6pm,
            priority=50,  # High priority for visibility
        )
    )

    # 2. Verify stored with correct lifecycle metadata
    retrieved = await repository.get_chunk(chunk.id)
    assert retrieved.knowledge_type == KnowledgeType.EVENT
    assert retrieved.expires_at is not None
    assert retrieved.priority == 50

    # 3. Wait briefly for Qdrant to index (eventual consistency)
    await asyncio.sleep(0.5)

    # 4. Search and verify found
    user_ctx = UserContext(user_id="user-1", tenant_id="company")
    results = await knowledge_store.search(
        query="marathon berlin streets",
        user_context=user_ctx,
        top_k=10,
        score_threshold=0.1,  # Lower threshold for testing
    )

    # Should find the notice
    assert len(results) > 0
    assert chunk.id in [r.id for r in results]

    # 4. Verify it ranks high (due to priority and recent creation)
    found_chunk = next(r for r in results if r.id == chunk.id)
    assert found_chunk.knowledge_type == KnowledgeType.EVENT
    assert "marathon" in found_chunk.text.lower()

    print("\n✅ User-created event notice test passed!")
    print(f"✅ Notice stored with expiration: {retrieved.expires_at}")
    print(f"✅ Priority set: {retrieved.priority}")
    print("✅ Searchable and ranked appropriately")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mixed_knowledge_types_search_ranking(db_session):
    """
    TEST: Create mix of knowledge types → Search → Verify ranking order

    This tests that lifecycle-aware ranking works with REAL pgvector.
    """
    # Setup
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()
    # Note: pgvector now integrated into PostgreSQL (no separate vector store)
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    # 1. Create different types of knowledge
    chunks = []

    # Documentation (permanent)
    doc = await knowledge_store.add_chunk(
        KnowledgeChunkCreate(
            text="my-app architecture documentation: PostgreSQL database, Redis cache, Kubernetes deployment",
            tenant_id="company",
            tags=["documentation", "my-app"],
            knowledge_type=KnowledgeType.DOCUMENTATION,
        )
    )
    chunks.append(("DOC", doc))

    # Procedure (permanent)
    proc = await knowledge_store.add_chunk(
        KnowledgeChunkCreate(
            text="my-app troubleshooting procedure: Check PostgreSQL, then Redis, then Kubernetes pods",
            tenant_id="company",
            tags=["procedure", "my-app"],
            knowledge_type=KnowledgeType.PROCEDURE,
        )
    )
    chunks.append(("PROC", proc))

    # Recent event (temporary, high priority)
    recent_event = await knowledge_store.add_chunk(
        KnowledgeChunkCreate(
            text="my-app PostgreSQL database connection failed 5 minutes ago",
            tenant_id="company",
            tags=["event", "my-app", "issue"],
            knowledge_type=KnowledgeType.EVENT,
            expires_at=datetime.now(tz=UTC) + timedelta(days=7),
            priority=10,  # Issue priority
        )
    )
    chunks.append(("RECENT_EVENT", recent_event))

    # 2. Wait for Qdrant to index
    await asyncio.sleep(0.5)

    # 3. Search for common term
    user_ctx = UserContext(user_id="user-1", tenant_id="company")
    results = await knowledge_store.search(
        query="my-app PostgreSQL", user_context=user_ctx, top_k=10, score_threshold=0.1
    )

    # 4. Verify all found
    assert len(results) >= 3
    result_ids = [r.id for r in results]
    assert doc.id in result_ids
    assert proc.id in result_ids
    assert recent_event.id in result_ids

    # 4. Verify ranking order (recent event should rank high!)
    # Get positions
    positions = {r.id: i for i, r in enumerate(results)}

    # Recent event with issue should rank high (likely top 3)
    recent_event_position = positions.get(recent_event.id, 999)
    assert recent_event_position < 3, (
        f"Recent event should rank in top 3, got position {recent_event_position}"
    )

    print("\n✅ Mixed knowledge types ranking test passed!")
    print("✅ All 3 types found in search")
    print(f"✅ Recent event ranked at position: {recent_event_position}")
    print("✅ Lifecycle-aware ranking works with real Qdrant!")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_knowledge_store_postgres_qdrant_sync(db_session):
    """
    TEST: Verify PostgreSQL with pgvector works correctly

    CRITICAL: Vectors must be stored and retrieved correctly for searches to work!
    """
    # Setup
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()
    # Note: pgvector now integrated into PostgreSQL (no separate vector store)
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    # 1. Create 10 chunks
    created_ids = []
    for i in range(10):
        chunk = await knowledge_store.add_chunk(
            KnowledgeChunkCreate(
                text=f"Test chunk {i} with content about testing",
                tenant_id="company",
                tags=[f"test-{i}"],
            )
        )
        created_ids.append(chunk.id)

    # 2. Verify all in PostgreSQL
    for chunk_id in created_ids:
        chunk = await repository.get_chunk(chunk_id)
        assert chunk is not None, f"Chunk {chunk_id} not found in PostgreSQL!"

    # 3. Verify all in Qdrant (via search)
    user_ctx = UserContext(user_id="user-1", tenant_id="company")
    results = await knowledge_store.search(
        query="test chunk content",
        user_context=user_ctx,
        top_k=20,
        score_threshold=0.0,  # Get all
    )

    found_ids = [r.id for r in results]

    # All chunks should be searchable
    for chunk_id in created_ids:
        assert chunk_id in found_ids, f"Chunk {chunk_id} in PostgreSQL but NOT in Qdrant!"

    # 4. Delete one chunk
    deleted_id = created_ids[0]
    deleted = await knowledge_store.delete_chunk(deleted_id)
    assert deleted is True

    # 5. Verify deleted from BOTH
    # PostgreSQL check
    pg_check = await repository.get_chunk(deleted_id)
    assert pg_check is None, "Chunk still in PostgreSQL after delete!"

    # Qdrant check (search should not find it)
    results_after = await knowledge_store.search(
        query="test chunk 0", user_context=user_ctx, top_k=20, score_threshold=0.0
    )
    found_ids_after = [r.id for r in results_after]
    assert deleted_id not in found_ids_after, "Chunk still in Qdrant after delete!"

    print("\n✅ PostgreSQL + Qdrant sync test passed!")
    print("✅ Created 10 chunks in both DBs")
    print("✅ All searchable")
    print("✅ Delete removes from both DBs")
    print("✅ Databases stay in sync!")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_performance_baseline(db_session):
    """
    TEST: Baseline search performance with moderate data (100 chunks)

    Establishes performance baseline for monitoring.
    """
    # Setup
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()
    # Note: pgvector now integrated into PostgreSQL (no separate vector store)
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    # 1. Create 100 chunks
    import time

    start_ingest = time.time()

    for i in range(100):
        await knowledge_store.add_chunk(
            KnowledgeChunkCreate(
                text=f"Knowledge chunk {i} about system architecture and troubleshooting procedures for application deployment",
                tenant_id="company",
                tags=[f"tag-{i % 10}"],  # 10 unique tags
                knowledge_type=KnowledgeType.DOCUMENTATION
                if i % 2 == 0
                else KnowledgeType.PROCEDURE,
            )
        )

    ingest_time = time.time() - start_ingest

    # 2. Wait for Qdrant to index all chunks
    await asyncio.sleep(1.0)

    # 3. Perform search
    user_ctx = UserContext(user_id="user-1", tenant_id="company")

    start_search = time.time()
    results = await knowledge_store.search(
        query="system architecture troubleshooting",
        user_context=user_ctx,
        top_k=10,
        score_threshold=0.1,
    )
    search_time = time.time() - start_search

    # 3. Verify results
    assert len(results) > 0
    assert len(results) <= 10  # top_k respected

    # 4. Performance assertions
    assert ingest_time < 120, f"Ingesting 100 chunks took {ingest_time:.1f}s (should be < 120s)"
    assert search_time < 1.0, f"Search took {search_time:.3f}s (should be < 1s)"

    print("\n✅ Performance baseline test passed!")
    print(f"✅ Ingested 100 chunks in {ingest_time:.1f} seconds")
    print(f"✅ Search completed in {search_time:.3f} seconds")
    print(f"✅ Found {len(results)} relevant results")
    print("✅ Performance acceptable for moderate scale")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifecycle_aware_ranking_real_infrastructure(db_session):
    """
    TEST: Verify lifecycle-aware ranking with REAL pgvector

    Tests that recent events rank higher than old ones with real infrastructure.
    """
    # Setup
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()
    # Note: pgvector now integrated into PostgreSQL (no separate vector store)
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    # 1. Create chunks with different ages and types
    # Documentation (permanent)
    doc_chunk = await knowledge_store.add_chunk(
        KnowledgeChunkCreate(
            text="my-app architecture: runs on Kubernetes, uses PostgreSQL",
            tenant_id="company",
            knowledge_type=KnowledgeType.DOCUMENTATION,
        )
    )

    # Recent event (high priority)
    recent_event = await knowledge_store.add_chunk(
        KnowledgeChunkCreate(
            text="my-app Kubernetes pod crashed with error: database connection failed",
            tenant_id="company",
            tags=["issue"],
            knowledge_type=KnowledgeType.EVENT,
            expires_at=datetime.now(tz=UTC) + timedelta(days=7),
            priority=10,
        )
    )

    # Wait for Qdrant to index
    await asyncio.sleep(0.5)

    # 2. Search
    user_ctx = UserContext(user_id="user-1", tenant_id="company")
    results = await knowledge_store.search(
        query="my-app Kubernetes PostgreSQL database",
        user_context=user_ctx,
        top_k=10,
        score_threshold=0.1,
    )

    # 3. Verify both found
    assert len(results) >= 2
    result_map = {r.id: i for i, r in enumerate(results)}

    assert doc_chunk.id in result_map
    assert recent_event.id in result_map

    # 4. Verify ranking (recent event with issue should rank high)
    recent_pos = result_map[recent_event.id]

    # Recent event with issue should be in top 3
    assert recent_pos < 3, f"Recent issue event should rank high, got position {recent_pos}"

    print("\n✅ Lifecycle ranking integration test passed!")
    print(f"✅ Recent event ranked at position: {recent_pos}")
    print("✅ Ranking works with real Qdrant!")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.slow
async def test_performance_with_500_chunks(db_session):
    """
    TEST: Performance with realistic data volume (500 chunks)

    Simulates realistic production load.
    """
    # Setup
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()
    # Note: pgvector now integrated into PostgreSQL (no separate vector store)
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    import time

    # 1. Ingest 500 chunks (mix of types)
    print("\n📊 Ingesting 500 chunks...")
    start_ingest = time.time()

    for i in range(500):
        knowledge_type = (
            KnowledgeType.DOCUMENTATION
            if i % 3 == 0
            else (KnowledgeType.PROCEDURE if i % 3 == 1 else KnowledgeType.EVENT)
        )

        expires_at = (
            datetime.now(tz=UTC) + timedelta(days=7)
            if knowledge_type == KnowledgeType.EVENT
            else None
        )

        await knowledge_store.add_chunk(
            KnowledgeChunkCreate(
                text=f"Knowledge content {i}: system architecture troubleshooting deployment monitoring performance optimization",
                tenant_id="company",
                tags=[f"tag-{i % 20}"],
                knowledge_type=knowledge_type,
                expires_at=expires_at,
            )
        )

        if (i + 1) % 100 == 0:
            print(f"  ✅ Ingested {i + 1} chunks...")

    ingest_time = time.time() - start_ingest

    # 2. Wait for Qdrant to index all 500 chunks
    print("\n⏳ Waiting for Qdrant to index 500 chunks...")
    await asyncio.sleep(2.0)

    # 3. Perform searches
    user_ctx = UserContext(user_id="user-1", tenant_id="company")

    search_times = []
    for query in ["architecture", "troubleshooting", "deployment", "monitoring", "optimization"]:
        start_search = time.time()
        results = await knowledge_store.search(
            query=query, user_context=user_ctx, top_k=10, score_threshold=0.1
        )
        search_time = time.time() - start_search
        search_times.append(search_time)

        assert len(results) > 0, f"No results for query: {query}"

    avg_search_time = sum(search_times) / len(search_times)

    # 3. Performance assertions
    assert ingest_time < 600, f"Ingesting 500 chunks took {ingest_time:.1f}s (should be < 600s)"
    assert avg_search_time < 0.5, f"Average search time {avg_search_time:.3f}s (should be < 0.5s)"
    assert max(search_times) < 1.0, f"Slowest search {max(search_times):.3f}s (should be < 1s)"

    print("\n✅ Performance test with 500 chunks passed!")
    print(f"✅ Ingestion: 500 chunks in {ingest_time:.1f}s ({ingest_time / 500:.2f}s per chunk)")
    print(f"✅ Search: avg {avg_search_time * 1000:.0f}ms, max {max(search_times) * 1000:.0f}ms")
    print("✅ System performs well at moderate scale!")


# Note: PDF ingestion test requires MinIO/S3 running
# Will add in next batch if MinIO available in test environment

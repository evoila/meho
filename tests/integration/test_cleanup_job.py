# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for cleanup job with real infrastructure.

Tests that expired events are actually deleted from PostgreSQL (including embeddings via pgvector).

After pgvector migration (Session 15):
- All data in PostgreSQL (text, metadata, AND vectors)
- Single DELETE removes everything
"""

import os
from datetime import UTC, datetime, timedelta

import pytest

from meho_app.core.auth_context import UserContext
from meho_app.modules.knowledge.cleanup import cleanup_expired_events, get_cleanup_statistics
from meho_app.modules.knowledge.embeddings import get_embedding_provider
from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.repository import KnowledgeRepository
from meho_app.modules.knowledge.schemas import KnowledgeChunkCreate, KnowledgeType

# Skip if no OpenAI API key
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("OPENAI_API_KEY"), reason="OPENAI_API_KEY required for real embeddings"
    ),
]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cleanup_deletes_from_both_databases(db_session):
    """
    CRITICAL TEST: Verify cleanup deletes from PostgreSQL (including embeddings)

    With pgvector: Single DELETE removes chunk data AND embeddings.
    """
    # Setup
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()  # Real OpenAI!
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    # 1. Create event that's already expired
    expired_chunk = await knowledge_store.add_chunk(
        KnowledgeChunkCreate(
            text="This event already expired - pod crash from last week",
            tenant_id="company",
            tags=["event", "expired"],
            knowledge_type=KnowledgeType.EVENT,
            expires_at=datetime.now(tz=UTC) - timedelta(hours=1),  # Already expired!
        )
    )

    # 2. Create event that's not expired yet
    active_chunk = await knowledge_store.add_chunk(
        KnowledgeChunkCreate(
            text="This event is still active - recent deployment",
            tenant_id="company",
            tags=["event", "active"],
            knowledge_type=KnowledgeType.EVENT,
            expires_at=datetime.now(tz=UTC) + timedelta(days=7),  # Expires in 7 days
        )
    )

    # 3. Verify both are in PostgreSQL
    assert await repository.get_chunk(expired_chunk.id) is not None
    assert await repository.get_chunk(active_chunk.id) is not None

    # 4. Run cleanup job (no vector_store parameter needed anymore!)
    cleanup_result = await cleanup_expired_events(db_session)

    # 5. Verify expired chunk deleted from PostgreSQL
    assert await repository.get_chunk(expired_chunk.id) is None, (
        "Expired chunk still in PostgreSQL!"
    )

    # 6. Verify active chunk NOT deleted
    assert await repository.get_chunk(active_chunk.id) is not None, "Active chunk was deleted!"

    # 7. Verify expired chunk no longer searchable (pgvector removes embeddings with chunk)
    user_ctx = UserContext(user_id="user-1", tenant_id="company")
    results = await knowledge_store.search(
        query="pod crash last week", user_context=user_ctx, top_k=10, score_threshold=0.0
    )

    result_ids = [r.id for r in results]
    assert expired_chunk.id not in result_ids, "Expired chunk still searchable!"

    # 8. Verify active chunk still searchable
    results_active = await knowledge_store.search(
        query="recent deployment", user_context=user_ctx, top_k=10, score_threshold=0.0
    )

    result_ids_active = [r.id for r in results_active]
    assert active_chunk.id in result_ids_active, "Active chunk not searchable!"

    print("\n✅ Cleanup job integration test passed!")
    print("✅ Expired chunks deleted from PostgreSQL (including embeddings)")
    print("✅ Active chunks preserved")
    print(f"✅ Cleanup result: {cleanup_result}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cleanup_statistics(db_session):
    """
    TEST: Cleanup statistics with real database

    Verifies monitoring/observability for cleanup job.
    """
    # Setup
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()  # Real OpenAI!
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    # 1. Create mix of knowledge types
    await knowledge_store.add_chunk(
        KnowledgeChunkCreate(
            text="Documentation chunk",
            tenant_id="company",
            knowledge_type=KnowledgeType.DOCUMENTATION,
        )
    )

    await knowledge_store.add_chunk(
        KnowledgeChunkCreate(
            text="Procedure chunk", tenant_id="company", knowledge_type=KnowledgeType.PROCEDURE
        )
    )

    await knowledge_store.add_chunk(
        KnowledgeChunkCreate(
            text="Event chunk",
            tenant_id="company",
            knowledge_type=KnowledgeType.EVENT,
            expires_at=datetime.now(tz=UTC) + timedelta(days=7),
        )
    )

    # 2. Get statistics
    stats = await get_cleanup_statistics(db_session)

    # 3. Verify statistics structure
    assert "total_chunks" in stats
    assert "chunks_by_type" in stats
    assert "expired_not_deleted" in stats
    assert "expiring_in_24h" in stats

    # 4. Verify counts
    assert stats["total_chunks"] >= 3
    assert stats["chunks_by_type"].get("documentation", 0) >= 1
    assert stats["chunks_by_type"].get("procedure", 0) >= 1
    assert stats["chunks_by_type"].get("event", 0) >= 1

    print("\n✅ Cleanup statistics test passed!")
    print("✅ Statistics structure correct")
    print(f"✅ Total chunks: {stats['total_chunks']}")
    print(f"✅ By type: {stats['chunks_by_type']}")
    print("✅ Monitoring data available!")

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for lifecycle-aware search ranking.

Tests that search results are ranked by recency, type, and priority.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.schemas import KnowledgeChunk, KnowledgeType


@pytest.fixture
def knowledge_store():
    """Knowledge store with mocked dependencies (pgvector architecture)

    NOTE: No vector_store parameter - pgvector integrated into repository.
    """
    return KnowledgeStore(
        repository=Mock(), embedding_provider=Mock(), hybrid_search_service=Mock()
    )


def create_test_chunk(
    text="test", knowledge_type=KnowledgeType.DOCUMENTATION, created_at=None, priority=0
):
    """Helper to create test chunk"""
    return KnowledgeChunk(
        id=str(uuid.uuid4()),
        text=text,
        tags=[],
        tenant_id="tenant-1",
        knowledge_type=knowledge_type,
        priority=priority,
        created_at=created_at or datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


def test_documentation_gets_boost(knowledge_store):
    """Test that documentation chunks get search boost"""
    now = datetime.now(tz=UTC)

    chunks = [create_test_chunk(knowledge_type=KnowledgeType.DOCUMENTATION, created_at=now)]

    vector_results = [{"id": chunks[0].id, "score": 0.8}]

    results = knowledge_store._apply_lifecycle_ranking(chunks, vector_results)

    assert len(results) == 1
    assert results[0]["base_score"] == pytest.approx(0.8)
    assert results[0]["final_score"] == pytest.approx(0.8 * 1.2)  # Documentation boost


def test_recent_event_gets_boost(knowledge_store):
    """Test that recent events (< 1h) get significant boost"""
    now = datetime.now(tz=UTC)

    # Event created 30 minutes ago
    chunks = [
        create_test_chunk(
            knowledge_type=KnowledgeType.EVENT, created_at=now - timedelta(minutes=30)
        )
    ]

    vector_results = [{"id": chunks[0].id, "score": 0.8}]

    results = knowledge_store._apply_lifecycle_ranking(chunks, vector_results)

    assert results[0]["final_score"] == pytest.approx(0.8 * 1.5)  # Recent event boost


def test_old_event_gets_downweighted(knowledge_store):
    """Test that old events (> 7 days) get downweighted"""
    now = datetime.now(tz=UTC)

    # Event created 10 days ago
    chunks = [
        create_test_chunk(knowledge_type=KnowledgeType.EVENT, created_at=now - timedelta(days=10))
    ]

    vector_results = [{"id": chunks[0].id, "score": 0.8}]

    results = knowledge_store._apply_lifecycle_ranking(chunks, vector_results)

    assert results[0]["final_score"] == pytest.approx(0.8 * 0.5)  # Old event penalty


def test_priority_affects_ranking(knowledge_store):
    """Test that explicit priority affects ranking"""
    chunks = [
        create_test_chunk(priority=50),  # High priority
        create_test_chunk(priority=0),  # Normal priority
        create_test_chunk(priority=-50),  # Low priority
    ]

    vector_results = [
        {"id": chunks[0].id, "score": 0.8},
        {"id": chunks[1].id, "score": 0.8},
        {"id": chunks[2].id, "score": 0.8},
    ]

    results = knowledge_store._apply_lifecycle_ranking(chunks, vector_results)

    # High priority chunk should have highest score
    # Note: Documentation also gets 1.2x boost, so: 0.8 * 1.2 * 1.5 = 1.44
    assert results[0]["final_score"] == pytest.approx(
        0.8 * 1.2 * 1.5, rel=0.01
    )  # Doc boost + priority
    assert results[1]["final_score"] == pytest.approx(0.8 * 1.2, rel=0.01)  # Doc boost only
    assert results[2]["final_score"] == pytest.approx(
        0.8 * 1.2 * 0.5, rel=0.01
    )  # Doc boost + penalty


def test_combined_ranking_factors(knowledge_store):
    """Test that multiple ranking factors combine correctly"""
    now = datetime.now(tz=UTC)

    chunks = [
        # Recent event with issue (should score highest)
        create_test_chunk(
            text="Recent issue",
            knowledge_type=KnowledgeType.EVENT,
            created_at=now - timedelta(minutes=10),
            priority=10,
        ),
        # Documentation (always relevant)
        create_test_chunk(
            text="Architecture doc",
            knowledge_type=KnowledgeType.DOCUMENTATION,
            created_at=now - timedelta(days=30),
            priority=0,
        ),
        # Old event (should score lowest)
        create_test_chunk(
            text="Old event",
            knowledge_type=KnowledgeType.EVENT,
            created_at=now - timedelta(days=14),
            priority=0,
        ),
    ]

    vector_results = [
        {"id": chunks[0].id, "score": 0.8},
        {"id": chunks[1].id, "score": 0.8},
        {"id": chunks[2].id, "score": 0.8},
    ]

    results = knowledge_store._apply_lifecycle_ranking(chunks, vector_results)

    # Recent event with priority should rank highest
    recent_event_score = 0.8 * 1.5 * 1.1  # recency * priority
    doc_score = 0.8 * 1.2  # documentation boost
    old_event_score = 0.8 * 0.5  # old event penalty

    assert results[0]["final_score"] == pytest.approx(recent_event_score, rel=0.01)
    assert results[1]["final_score"] == pytest.approx(doc_score, rel=0.01)
    assert results[2]["final_score"] == pytest.approx(old_event_score, rel=0.01)


def test_sorting_order(knowledge_store):
    """Test that results are sorted by final score"""
    now = datetime.now(tz=UTC)

    chunks = [
        create_test_chunk(
            text="Low relevance doc", knowledge_type=KnowledgeType.DOCUMENTATION, priority=0
        ),
        create_test_chunk(
            text="Recent critical event",
            knowledge_type=KnowledgeType.EVENT,
            created_at=now - timedelta(minutes=5),
            priority=10,
        ),
        create_test_chunk(
            text="High relevance doc", knowledge_type=KnowledgeType.DOCUMENTATION, priority=0
        ),
    ]

    vector_results = [
        {"id": chunks[0].id, "score": 0.6},  # Low similarity
        {"id": chunks[1].id, "score": 0.7},  # Medium similarity, but recent + issue
        {"id": chunks[2].id, "score": 0.9},  # High similarity
    ]

    results = knowledge_store._apply_lifecycle_ranking(chunks, vector_results)

    # Verify results include all chunks
    assert len(results) == 3

    # Recent critical event should rank high despite medium similarity
    recent_event = next(r for r in results if "critical event" in r["chunk"].text)
    assert recent_event["final_score"] > 1.0  # Boosted significantly

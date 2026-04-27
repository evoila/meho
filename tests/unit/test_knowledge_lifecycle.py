# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for knowledge lifecycle management.

Tests time-based expiration, knowledge types, and cleanup logic.
"""

from datetime import UTC, datetime, timedelta

import pytest

from meho_app.modules.knowledge.schemas import KnowledgeChunkCreate, KnowledgeType


def test_event_chunk_has_expiration():
    """Test that event chunks include expiration"""
    expires_at = datetime.now(tz=UTC) + timedelta(days=7)

    chunk = KnowledgeChunkCreate(
        text="Pod crashed in production",
        tags=["kubernetes", "pod:my-app", "event:crash", "issue"],
        knowledge_type=KnowledgeType.EVENT,
        expires_at=expires_at,
        priority=10,
        tenant_id="tenant-1",
    )

    assert chunk.expires_at is not None
    assert chunk.knowledge_type == KnowledgeType.EVENT
    assert chunk.priority == 10

    # Check expiration is approximately 7 days (allow for timing)
    days_until_expiry = (chunk.expires_at - datetime.now(tz=UTC)).days
    assert 6 <= days_until_expiry <= 7


def test_documentation_chunk_no_expiration():
    """Test that documentation chunks don't expire by default"""
    chunk = KnowledgeChunkCreate(
        text="my-app architecture documentation",
        tags=["documentation", "architecture"],
        knowledge_type=KnowledgeType.DOCUMENTATION,
        expires_at=None,  # Never expires
        priority=0,
        tenant_id="tenant-1",
    )

    assert chunk.expires_at is None
    assert chunk.knowledge_type == KnowledgeType.DOCUMENTATION
    assert chunk.priority == 0


def test_procedure_chunk_permanent():
    """Test that procedure chunks are permanent"""
    chunk = KnowledgeChunkCreate(
        text="my-app troubleshooting runbook: Step 1. Check GitHub...",
        tags=["procedure", "troubleshooting", "my-app"],
        knowledge_type=KnowledgeType.PROCEDURE,
        expires_at=None,
        tenant_id="tenant-1",
    )

    assert chunk.expires_at is None
    assert chunk.knowledge_type == KnowledgeType.PROCEDURE


def test_different_knowledge_types():
    """Test all knowledge types"""
    types_to_test = [
        KnowledgeType.DOCUMENTATION,
        KnowledgeType.PROCEDURE,
        KnowledgeType.EVENT,
        KnowledgeType.EVENT_SUMMARY,
        KnowledgeType.TREND,
    ]

    for knowledge_type in types_to_test:
        chunk = KnowledgeChunkCreate(
            text="test", knowledge_type=knowledge_type, tenant_id="tenant-1"
        )
        assert chunk.knowledge_type == knowledge_type


def test_priority_range_validation():
    """Test that priority is validated in range"""
    # Valid priorities
    for priority in [-100, -50, 0, 50, 100]:
        chunk = KnowledgeChunkCreate(text="test", priority=priority, tenant_id="tenant-1")
        assert chunk.priority == priority

    # Invalid priorities should raise validation error
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        KnowledgeChunkCreate(
            text="test",
            priority=101,  # Too high
            tenant_id="tenant-1",
        )

    with pytest.raises(ValidationError):
        KnowledgeChunkCreate(
            text="test",
            priority=-101,  # Too low
            tenant_id="tenant-1",
        )


def test_default_values():
    """Test default values for lifecycle fields"""
    chunk = KnowledgeChunkCreate(text="test chunk", tenant_id="tenant-1")

    # Defaults
    assert chunk.expires_at is None  # Don't expire by default
    assert chunk.knowledge_type == KnowledgeType.DOCUMENTATION  # Default type
    assert chunk.priority == 0  # Default priority

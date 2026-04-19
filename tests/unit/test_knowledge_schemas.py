# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_app.modules.knowledge.schemas
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from meho_app.modules.knowledge.schemas import (
    KnowledgeChunk,
    KnowledgeChunkCreate,
    KnowledgeChunkFilter,
)


@pytest.mark.unit
def test_knowledge_chunk_create_minimal():
    """Test creating chunk with only required fields"""
    chunk = KnowledgeChunkCreate(text="Test knowledge")

    assert chunk.text == "Test knowledge"
    assert chunk.tenant_id is None
    assert chunk.connector_id is None
    assert chunk.user_id is None
    assert chunk.roles == []
    assert chunk.groups == []
    assert chunk.tags == []
    assert chunk.source_uri is None


@pytest.mark.unit
def test_knowledge_chunk_create_all_fields():
    """Test creating chunk with all fields"""
    chunk = KnowledgeChunkCreate(
        text="Test knowledge",
        tenant_id="tenant-1",
        connector_id="system-1",
        user_id="user-1",
        roles=["admin"],
        groups=["team-a"],
        tags=["test", "example"],
        source_uri="s3://bucket/doc.pdf#page=1",
    )

    assert chunk.text == "Test knowledge"
    assert chunk.tenant_id == "tenant-1"
    assert chunk.connector_id == "system-1"
    assert chunk.user_id == "user-1"
    assert chunk.roles == ["admin"]
    assert chunk.groups == ["team-a"]
    assert chunk.tags == ["test", "example"]
    assert chunk.source_uri == "s3://bucket/doc.pdf#page=1"


@pytest.mark.unit
def test_knowledge_chunk_create_empty_text_fails():
    """Test that empty text is rejected"""
    with pytest.raises(ValidationError):
        KnowledgeChunkCreate(text="")


@pytest.mark.unit
def test_knowledge_chunk_create_text_too_long_fails():
    """Test that extremely long text is rejected"""
    with pytest.raises(ValidationError):
        KnowledgeChunkCreate(text="x" * 100001)  # Max is 100000


@pytest.mark.unit
def test_knowledge_chunk_with_id():
    """Test KnowledgeChunk model with ID"""
    chunk = KnowledgeChunk(
        id="123e4567-e89b-12d3-a456-426614174000",
        text="Test",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    assert chunk.id == "123e4567-e89b-12d3-a456-426614174000"
    assert chunk.text == "Test"


@pytest.mark.unit
def test_knowledge_chunk_json_serialization():
    """Test chunk can be serialized to/from JSON"""
    chunk = KnowledgeChunk(
        id="123e4567-e89b-12d3-a456-426614174000",
        text="Test knowledge",
        tenant_id="tenant-1",
        tags=["test"],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    # Serialize
    json_str = chunk.model_dump_json()
    assert isinstance(json_str, str)

    # Deserialize
    chunk2 = KnowledgeChunk.model_validate_json(json_str)
    assert chunk2.id == chunk.id
    assert chunk2.text == chunk.text


@pytest.mark.unit
def test_knowledge_chunk_filter_defaults():
    """Test filter with default values"""
    filter_params = KnowledgeChunkFilter()

    assert filter_params.tenant_id is None
    assert filter_params.connector_id is None
    assert filter_params.user_id is None
    assert filter_params.tags is None
    assert filter_params.limit == 100
    assert filter_params.offset == 0


@pytest.mark.unit
def test_knowledge_chunk_filter_custom():
    """Test filter with custom values"""
    filter_params = KnowledgeChunkFilter(
        tenant_id="tenant-1", connector_id="system-1", tags=["tag1", "tag2"], limit=50, offset=10
    )

    assert filter_params.tenant_id == "tenant-1"
    assert filter_params.connector_id == "system-1"
    assert filter_params.tags == ["tag1", "tag2"]
    assert filter_params.limit == 50
    assert filter_params.offset == 10


@pytest.mark.unit
def test_knowledge_chunk_filter_limit_validation():
    """Test filter limit validation"""
    # Too low
    with pytest.raises(ValidationError):
        KnowledgeChunkFilter(limit=0)

    # Too high
    with pytest.raises(ValidationError):
        KnowledgeChunkFilter(limit=1001)

    # Just right
    filter_params = KnowledgeChunkFilter(limit=1)
    assert filter_params.limit == 1

    filter_params = KnowledgeChunkFilter(limit=1000)
    assert filter_params.limit == 1000


@pytest.mark.unit
def test_knowledge_chunk_filter_offset_validation():
    """Test filter offset validation"""
    # Negative not allowed
    with pytest.raises(ValidationError):
        KnowledgeChunkFilter(offset=-1)

    # Zero is fine
    filter_params = KnowledgeChunkFilter(offset=0)
    assert filter_params.offset == 0

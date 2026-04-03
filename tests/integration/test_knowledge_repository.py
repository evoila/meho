# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for KnowledgeRepository with real database.
"""

import pytest

from meho_app.modules.knowledge.repository import KnowledgeRepository
from meho_app.modules.knowledge.schemas import KnowledgeChunkCreate, KnowledgeChunkFilter
from tests.support.assertions import assert_datetime_recent, assert_valid_uuid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_chunk(db_session):
    """Test creating a knowledge chunk"""
    repo = KnowledgeRepository(db_session)

    chunk_create = KnowledgeChunkCreate(
        text="Test knowledge content", tenant_id="tenant-1", tags=["test"]
    )

    chunk = await repo.create_chunk(chunk_create)

    # Verify chunk was created
    assert_valid_uuid(chunk.id, "Chunk ID should be valid UUID")
    assert chunk.text == "Test knowledge content"
    assert chunk.tenant_id == "tenant-1"
    assert chunk.tags == ["test"]
    assert_datetime_recent(chunk.created_at, seconds=5)
    assert_datetime_recent(chunk.updated_at, seconds=5)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_chunk(db_session):
    """Test retrieving a chunk by ID"""
    repo = KnowledgeRepository(db_session)

    # Create a chunk
    chunk_create = KnowledgeChunkCreate(text="Test knowledge")
    created = await repo.create_chunk(chunk_create)

    # Retrieve it
    retrieved = await repo.get_chunk(created.id)

    assert retrieved is not None
    assert retrieved.id == created.id
    assert retrieved.text == created.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_chunk_not_found(db_session):
    """Test getting non-existent chunk returns None"""
    repo = KnowledgeRepository(db_session)

    result = await repo.get_chunk("00000000-0000-0000-0000-000000000000")

    assert result is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_chunk_invalid_uuid(db_session):
    """Test getting chunk with invalid UUID returns None"""
    repo = KnowledgeRepository(db_session)

    result = await repo.get_chunk("not-a-uuid")

    assert result is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_chunks_empty(db_session):
    """Test listing chunks when none exist"""
    repo = KnowledgeRepository(db_session)

    filter_params = KnowledgeChunkFilter()
    chunks = await repo.list_chunks(filter_params)

    assert chunks == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_chunks_all(db_session):
    """Test listing all chunks"""
    repo = KnowledgeRepository(db_session)

    # Create multiple chunks
    for i in range(3):
        await repo.create_chunk(KnowledgeChunkCreate(text=f"Chunk {i}"))

    filter_params = KnowledgeChunkFilter()
    chunks = await repo.list_chunks(filter_params)

    assert len(chunks) == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_chunks_filter_by_tenant(db_session):
    """Test filtering chunks by tenant_id"""
    repo = KnowledgeRepository(db_session)

    # Create chunks for different tenants
    await repo.create_chunk(KnowledgeChunkCreate(text="Tenant 1 chunk", tenant_id="tenant-1"))
    await repo.create_chunk(KnowledgeChunkCreate(text="Tenant 2 chunk", tenant_id="tenant-2"))
    await repo.create_chunk(KnowledgeChunkCreate(text="Tenant 1 chunk 2", tenant_id="tenant-1"))

    # Filter by tenant-1
    filter_params = KnowledgeChunkFilter(tenant_id="tenant-1")
    chunks = await repo.list_chunks(filter_params)

    assert len(chunks) == 2
    assert all(c.tenant_id == "tenant-1" for c in chunks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_chunks_filter_by_system(db_session):
    """Test filtering chunks by system_id"""
    repo = KnowledgeRepository(db_session)

    await repo.create_chunk(
        KnowledgeChunkCreate(text="System A", tenant_id="tenant-1", system_id="system-a")
    )
    await repo.create_chunk(
        KnowledgeChunkCreate(text="System B", tenant_id="tenant-1", system_id="system-b")
    )

    filter_params = KnowledgeChunkFilter(system_id="system-a")
    chunks = await repo.list_chunks(filter_params)

    assert len(chunks) == 1
    assert chunks[0].system_id == "system-a"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_chunks_filter_by_tags(db_session):
    """Test filtering chunks by tags (AND logic)"""
    repo = KnowledgeRepository(db_session)

    await repo.create_chunk(KnowledgeChunkCreate(text="A", tags=["tag1"]))
    await repo.create_chunk(KnowledgeChunkCreate(text="B", tags=["tag2"]))
    await repo.create_chunk(KnowledgeChunkCreate(text="C", tags=["tag1", "tag2"]))

    # Filter by tag1
    filter_params = KnowledgeChunkFilter(tags=["tag1"])
    chunks = await repo.list_chunks(filter_params)

    assert len(chunks) == 2  # A and C
    assert all("tag1" in c.tags for c in chunks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_chunks_filter_by_multiple_tags(db_session):
    """Test filtering by multiple tags (AND logic)"""
    repo = KnowledgeRepository(db_session)

    await repo.create_chunk(KnowledgeChunkCreate(text="A", tags=["tag1"]))
    await repo.create_chunk(KnowledgeChunkCreate(text="B", tags=["tag1", "tag2"]))
    await repo.create_chunk(KnowledgeChunkCreate(text="C", tags=["tag1", "tag2", "tag3"]))

    # Must have both tag1 AND tag2
    filter_params = KnowledgeChunkFilter(tags=["tag1", "tag2"])
    chunks = await repo.list_chunks(filter_params)

    assert len(chunks) == 2  # B and C
    assert all("tag1" in c.tags and "tag2" in c.tags for c in chunks)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_chunks_pagination(db_session):
    """Test chunk list pagination"""
    repo = KnowledgeRepository(db_session)

    # Create 10 chunks
    for i in range(10):
        await repo.create_chunk(KnowledgeChunkCreate(text=f"Chunk {i}"))

    # Get first page
    filter_params = KnowledgeChunkFilter(limit=5, offset=0)
    page1 = await repo.list_chunks(filter_params)
    assert len(page1) == 5

    # Get second page
    filter_params = KnowledgeChunkFilter(limit=5, offset=5)
    page2 = await repo.list_chunks(filter_params)
    assert len(page2) == 5

    # Pages should have different chunks
    page1_ids = {c.id for c in page1}
    page2_ids = {c.id for c in page2}
    assert page1_ids.isdisjoint(page2_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_chunks_ordered_by_created_at(db_session):
    """Test chunks are ordered by created_at descending"""
    repo = KnowledgeRepository(db_session)

    # Create chunks (will have slightly different created_at)
    chunk1 = await repo.create_chunk(KnowledgeChunkCreate(text="First"))
    chunk2 = await repo.create_chunk(KnowledgeChunkCreate(text="Second"))
    chunk3 = await repo.create_chunk(KnowledgeChunkCreate(text="Third"))

    filter_params = KnowledgeChunkFilter()
    chunks = await repo.list_chunks(filter_params)

    # Should be in reverse order (most recent first)
    assert chunks[0].id == chunk3.id
    assert chunks[1].id == chunk2.id
    assert chunks[2].id == chunk1.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_chunk(db_session):
    """Test deleting a chunk"""
    repo = KnowledgeRepository(db_session)

    # Create a chunk
    chunk = await repo.create_chunk(KnowledgeChunkCreate(text="To be deleted"))

    # Delete it
    deleted = await repo.delete_chunk(chunk.id)

    assert deleted is True

    # Verify it's gone
    retrieved = await repo.get_chunk(chunk.id)
    assert retrieved is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_chunk_not_found(db_session):
    """Test deleting non-existent chunk returns False"""
    repo = KnowledgeRepository(db_session)

    deleted = await repo.delete_chunk("00000000-0000-0000-0000-000000000000")

    assert deleted is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_chunk_invalid_uuid(db_session):
    """Test deleting with invalid UUID returns False"""
    repo = KnowledgeRepository(db_session)

    deleted = await repo.delete_chunk("not-a-uuid")

    assert deleted is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_count_chunks(db_session):
    """Test counting chunks"""
    repo = KnowledgeRepository(db_session)

    # Create some chunks
    for i in range(5):
        await repo.create_chunk(KnowledgeChunkCreate(text=f"Chunk {i}", tenant_id="tenant-1"))

    # Count all
    count = await repo.count_chunks()
    assert count == 5

    # Count with filter
    filter_params = KnowledgeChunkFilter(tenant_id="tenant-1")
    count = await repo.count_chunks(filter_params)
    assert count == 5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_chunks_combined_filters(db_session):
    """Test filtering by multiple criteria"""
    repo = KnowledgeRepository(db_session)

    # Create chunks with various attributes
    await repo.create_chunk(
        KnowledgeChunkCreate(
            text="Chunk 1", tenant_id="tenant-1", system_id="system-a", tags=["tag1"]
        )
    )
    await repo.create_chunk(
        KnowledgeChunkCreate(
            text="Chunk 2", tenant_id="tenant-1", system_id="system-b", tags=["tag1"]
        )
    )
    await repo.create_chunk(
        KnowledgeChunkCreate(
            text="Chunk 3", tenant_id="tenant-2", system_id="system-a", tags=["tag1"]
        )
    )

    # Filter by tenant AND system
    filter_params = KnowledgeChunkFilter(tenant_id="tenant-1", system_id="system-a")
    chunks = await repo.list_chunks(filter_params)

    assert len(chunks) == 1
    assert chunks[0].text == "Chunk 1"

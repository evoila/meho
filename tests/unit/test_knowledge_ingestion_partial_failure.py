# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for partial failure handling in ingestion service.

Tests that partial ingestion is cleaned up properly.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from meho_app.modules.knowledge.ingestion import IngestionService
from meho_app.modules.knowledge.schemas import KnowledgeChunk


@pytest.fixture
def mock_knowledge_store_with_failure():
    """Mock knowledge store that fails on 3rd chunk"""
    store = AsyncMock()
    call_count = 0

    async def mock_add_chunk(chunk_create):
        nonlocal call_count
        call_count += 1

        if call_count == 3:
            # Fail on 3rd chunk (simulating embedding API failure, etc.)
            raise Exception("Simulated chunk creation failure")

        return KnowledgeChunk(
            id=f"chunk-{call_count}",
            **chunk_create.model_dump(),
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

    store.add_chunk = mock_add_chunk
    store.delete_chunk = AsyncMock(return_value=True)
    return store


@pytest.fixture
def mock_object_storage():
    """Mock object storage"""
    storage = Mock()
    storage.upload_document.return_value = "s3://bucket/doc.pdf"
    storage.delete_document = Mock()
    return storage


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partial_ingestion_cleans_up_chunks(
    mock_knowledge_store_with_failure, mock_object_storage
):
    """Test that partial chunk creation is cleaned up on failure"""
    service = IngestionService(
        knowledge_store=mock_knowledge_store_with_failure, object_storage=mock_object_storage
    )

    # Create text that will produce multiple chunks
    file_bytes = (
        "This is chunk 1. " * 100 + "This is chunk 2. " * 100 + "This is chunk 3. " * 100
    ).encode()

    # Should fail on 3rd chunk
    with pytest.raises(ValueError, match="Failed to create chunk"):
        await service.ingest_document(
            file_bytes=file_bytes, filename="test.txt", mime_type="text/plain", tenant_id="tenant-1"
        )

    # Should have attempted to delete the 2 successfully created chunks
    assert mock_knowledge_store_with_failure.delete_chunk.call_count == 2

    # Verify it tried to delete chunk-1 and chunk-2
    delete_calls = [
        call.args[0] for call in mock_knowledge_store_with_failure.delete_chunk.call_args_list
    ]
    assert "chunk-1" in delete_calls
    assert "chunk-2" in delete_calls

    # Should also delete uploaded document
    mock_object_storage.delete_document.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partial_ingestion_error_message():
    """Test that partial failure error message is helpful"""
    store = AsyncMock()
    storage = Mock()
    storage.upload_document.return_value = "s3://bucket/doc.pdf"

    call_count = 0

    async def failing_add_chunk(chunk_create):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise Exception("Embedding API failed")
        return KnowledgeChunk(
            id=f"chunk-{call_count}",
            **chunk_create.model_dump(),
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

    store.add_chunk = failing_add_chunk
    store.delete_chunk = AsyncMock()

    service = IngestionService(knowledge_store=store, object_storage=storage)

    # Create text long enough to generate multiple chunks (>512 tokens)
    file_bytes = b"This is a sentence. " * 500  # ~1000 tokens

    with pytest.raises(ValueError) as exc_info:  # noqa: PT011 -- test validates exception type is sufficient
        await service.ingest_document(
            file_bytes=file_bytes, filename="test.txt", mime_type="text/plain"
        )

    # Error message should indicate which chunk failed
    error_msg = str(exc_info.value)
    assert "chunk" in error_msg.lower() or "failed" in error_msg.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_successful_ingestion_no_cleanup():
    """Test that successful ingestion doesn't trigger cleanup"""
    store = AsyncMock()
    storage = Mock()
    storage.upload_document.return_value = "s3://bucket/doc.pdf"

    async def mock_add_chunk(chunk_create):
        return KnowledgeChunk(
            id="chunk-123",
            **chunk_create.model_dump(),
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

    store.add_chunk = mock_add_chunk
    store.delete_chunk = AsyncMock()

    service = IngestionService(knowledge_store=store, object_storage=storage)

    file_bytes = b"Test text"

    chunk_ids = await service.ingest_document(
        file_bytes=file_bytes, filename="test.txt", mime_type="text/plain"
    )

    # Should not delete any chunks on success
    store.delete_chunk.assert_not_called()

    # Should not delete document on success
    storage.delete_document.assert_not_called()

    # Should return chunk IDs
    assert len(chunk_ids) >= 1

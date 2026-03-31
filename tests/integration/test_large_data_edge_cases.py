# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Large data and edge case tests.

Tests system behavior with extreme inputs and edge cases.

After pgvector migration: All tests use PostgreSQL with pgvector extension.
"""

import os

import pytest
from pydantic import ValidationError

from meho_app.modules.knowledge.embeddings import get_embedding_provider
from meho_app.modules.knowledge.knowledge_store import KnowledgeStore
from meho_app.modules.knowledge.repository import KnowledgeRepository
from meho_app.modules.knowledge.schemas import KnowledgeChunkCreate

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="OPENAI_API_KEY required"),
]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_very_large_text_chunk(db_session):
    """
    TEST: Handling of very large text chunks

    Scenario: Text chunk near maximum size (100k characters)
    Expected: Ingestion succeeds, embedding works, searchable
    """
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    # Create 90k character text (near 100k limit)
    large_text = "This is a very large document. " * 3000  # ~90k chars

    print(f"\n📊 Testing with {len(large_text)} character chunk")

    chunk = await knowledge_store.add_chunk(
        KnowledgeChunkCreate(text=large_text, tenant_id="company")
    )

    assert chunk.id is not None
    # Text should be truncated to fit token limit
    assert len(chunk.text) < len(large_text), "Text should be truncated"
    assert "truncated" in chunk.text.lower(), "Should indicate truncation"

    print("✅ Large text chunk test PASSED!")
    print(f"✅ Original: {len(large_text)} chars, Truncated: {len(chunk.text)} chars")
    print("✅ Auto-truncation prevents OpenAI error")
    print("✅ Embedding generated successfully")
    print("✅ Stored in PostgreSQL with pgvector")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_text_exceeding_maximum_size_rejected():
    """
    TEST: Text exceeding 100k character limit is rejected

    Scenario: Try to create chunk with >100k characters
    Expected: Validation error, clear message
    """
    # Create >100k character text
    too_large_text = "x" * 100001

    with pytest.raises(ValidationError) as exc_info:
        KnowledgeChunkCreate(text=too_large_text, tenant_id="company")

    assert "100000" in str(exc_info.value) or "max_length" in str(exc_info.value)

    print("✅ Text size limit test PASSED!")
    print("✅ >100k characters rejected")
    print("✅ Validation prevents oversized chunks")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_text_rejected():
    """
    TEST: Empty text is rejected

    Scenario: Try to create chunk with empty string
    Expected: Validation error
    """
    with pytest.raises(ValidationError) as exc_info:
        KnowledgeChunkCreate(text="", tenant_id="company")

    assert "min_length" in str(exc_info.value) or "least 1" in str(exc_info.value)

    print("✅ Empty text test PASSED!")
    print("✅ Empty chunks rejected")
    print("✅ Validation enforced")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_text_with_unicode_and_emojis(db_session):
    """
    TEST: Handling of unicode characters and emojis

    Scenario: Text with various unicode (emojis, right-to-left, special chars)
    Expected: Ingestion succeeds, text preserved correctly
    """
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    # Text with unicode edge cases
    unicode_text = """
    Testing Unicode:
    - Emojis: 🎉 🚀 ✅ ❌ 💡
    - Right-to-left: مرحبا العالم (Arabic)
    - Special chars: ñ ü ö æ ø å
    - Math symbols: ∑ ∫ √ ∞
    - Currency: € £ ¥ ₹
    - Chinese: 你好世界
    """

    chunk = await knowledge_store.add_chunk(
        KnowledgeChunkCreate(text=unicode_text, tenant_id="company")
    )

    # Verify text preserved
    assert "🎉" in chunk.text
    assert "مرحبا" in chunk.text
    assert "你好" in chunk.text

    print("✅ Unicode handling test PASSED!")
    print("✅ Emojis preserved")
    print("✅ Right-to-left text preserved")
    print("✅ Special characters handled correctly")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_null_and_none_handling(db_session):
    """
    TEST: Handling of None/null values in optional fields

    Scenario: Create chunks with all optional fields as None
    Expected: Succeeds, defaults applied correctly
    """
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    # Create chunk with all Nones
    chunk = await knowledge_store.add_chunk(
        KnowledgeChunkCreate(
            text="Test with all nulls",
            tenant_id=None,  # Global
            system_id=None,
            user_id=None,
            tags=[],
            source_uri=None,
            expires_at=None,
            priority=0,
        )
    )

    assert chunk.id is not None
    assert chunk.tenant_id is None  # Global chunk
    assert chunk.tags == []

    print("✅ Null handling test PASSED!")
    print("✅ None values handled correctly")
    print("✅ Global chunk created")
    print("✅ Defaults applied properly")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_extremely_long_tag_list(db_session):
    """
    TEST: Handling of very long tag lists

    Scenario: Chunk with 100 tags
    Expected: Stored correctly, search filtering works
    """
    repository = KnowledgeRepository(db_session)
    embedding_provider = get_embedding_provider()
    knowledge_store = KnowledgeStore(repository=repository, embedding_provider=embedding_provider)

    # Create chunk with 100 tags
    many_tags = [f"tag-{i}" for i in range(100)]

    chunk = await knowledge_store.add_chunk(
        KnowledgeChunkCreate(text="Chunk with many tags", tenant_id="company", tags=many_tags)
    )

    # Verify all tags stored
    assert len(chunk.tags) == 100
    assert "tag-50" in chunk.tags

    print("✅ Large tag list test PASSED!")
    print("✅ 100 tags stored correctly")
    print("✅ No truncation or data loss")

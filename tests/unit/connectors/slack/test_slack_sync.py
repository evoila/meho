# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for Slack operations sync.

Covers VALIDATION.md case MSG-01f:
- MSG-01f: Operations sync creates knowledge chunks
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub slack modules before importing sync
sys.modules.setdefault("slack_sdk", MagicMock())
sys.modules.setdefault("slack_sdk.web", MagicMock())
sys.modules.setdefault("slack_sdk.web.async_client", MagicMock())
sys.modules.setdefault("slack_sdk.errors", MagicMock())
sys.modules.setdefault("slack_bolt", MagicMock())
sys.modules.setdefault("slack_bolt.async_app", MagicMock())
sys.modules.setdefault("slack_bolt.adapter", MagicMock())
sys.modules.setdefault("slack_bolt.adapter.socket_mode", MagicMock())
sys.modules.setdefault("slack_bolt.adapter.socket_mode.async_handler", MagicMock())

from meho_app.modules.connectors.slack.operations import SLACK_OPERATIONS
from meho_app.modules.connectors.slack.sync import (
    _format_slack_operation_as_text,
    _generate_slack_search_keywords,
)


# =========================================================================
# MSG-01f: Operations sync creates knowledge chunks
# =========================================================================


@pytest.mark.asyncio
async def test_sync_operations():
    """MSG-01f: Verify _sync_slack_knowledge_chunks creates chunks for each operation."""
    from meho_app.modules.connectors.slack.sync import _sync_slack_knowledge_chunks

    # Mock KnowledgeStore with a properly mocked session for delete
    mock_knowledge_store = AsyncMock()
    mock_knowledge_store.add_chunk = AsyncMock()
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_knowledge_store.repository = MagicMock()
    mock_knowledge_store.repository.session = mock_session

    chunks_created = await _sync_slack_knowledge_chunks(
        knowledge_store=mock_knowledge_store,
        connector_id="test-connector-id",
        connector_name="Test Slack",
        tenant_id="test-tenant-id",
    )

    # Should create one chunk per SLACK_OPERATIONS entry
    assert chunks_created == len(SLACK_OPERATIONS)
    assert mock_knowledge_store.add_chunk.await_count == len(SLACK_OPERATIONS)

    # Each chunk should have been called with a KnowledgeChunkCreate
    for call in mock_knowledge_store.add_chunk.call_args_list:
        chunk_create = call[0][0]
        # Verify it has the expected tags including "slack" and "messaging"
        assert "slack" in chunk_create.tags
        assert "messaging" in chunk_create.tags


# =========================================================================
# Format and keyword generation tests
# =========================================================================


def test_format_slack_operation_as_text():
    """Verify _format_slack_operation_as_text produces meaningful text."""
    # Use the first operation (get_channel_history) as test case
    op = SLACK_OPERATIONS[0]
    text = _format_slack_operation_as_text(op, "Test Slack Connector")

    # Should contain operation name
    assert "Get Channel History" in text or "get_channel_history" in text

    # Should contain connector name
    assert "Test Slack Connector" in text

    # Should contain platform
    assert "Slack" in text

    # Should contain description
    assert "channel" in text.lower()

    # Should contain parameter names
    assert "channel_id" in text

    # Should contain category
    assert "messaging" in text


def test_format_slack_operation_with_example():
    """Verify operations with examples include them in text."""
    # find an operation with an example
    ops_with_examples = [op for op in SLACK_OPERATIONS if op.example]
    assert len(ops_with_examples) > 0, "Expected at least one operation with an example"

    op = ops_with_examples[0]
    text = _format_slack_operation_as_text(op)

    assert "Example" in text


def test_generate_slack_search_keywords():
    """Verify _generate_slack_search_keywords produces relevant keywords."""
    # Test with get_channel_history
    op = SLACK_OPERATIONS[0]
    keywords = _generate_slack_search_keywords(op)

    # Should contain Slack-specific terms
    assert "slack" in keywords
    assert "workspace" in keywords

    # Should contain synonyms for channel-related operations
    assert (
        "channel" in keywords.lower()
        or "channels" in keywords.lower()
        or "conversation" in keywords.lower()
    )


def test_generate_slack_search_keywords_search_operation():
    """Verify search_messages operation has relevant search keywords."""
    search_op = next(op for op in SLACK_OPERATIONS if op.operation_id == "search_messages")
    keywords = _generate_slack_search_keywords(search_op)

    # Should contain search synonyms
    assert "find" in keywords or "query" in keywords or "lookup" in keywords


def test_generate_slack_search_keywords_post_operation():
    """Verify post_message operation has relevant post keywords."""
    post_op = next(op for op in SLACK_OPERATIONS if op.operation_id == "post_message")
    keywords = _generate_slack_search_keywords(post_op)

    # Should contain post/send synonyms
    assert "send" in keywords or "write" in keywords or "publish" in keywords

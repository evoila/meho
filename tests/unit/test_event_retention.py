# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for event retention policies.

Tests that webhook processor sets correct expiration based on event type.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from meho_app.modules.ingestion.processor import EVENT_RETENTION_POLICIES, GenericWebhookProcessor
from meho_app.modules.knowledge.schemas import KnowledgeType


@pytest.fixture
def mock_deps():
    """Mock dependencies for processor"""
    template_repo = Mock()
    knowledge_store = Mock()
    renderer = Mock()

    template = Mock()
    template.id = "template-1"
    template.connector_id = "test-connector"
    template.event_type = "push"
    template.text_template = "Test"
    template.tag_rules = []
    template.issue_detection_rule = None
    template.tenant_id = "tenant-1"

    template_repo.get_template = AsyncMock(return_value=template)
    renderer.render_text = Mock(return_value="Rendered text")
    renderer.render_tags = Mock(return_value=["tag1"])
    renderer.evaluate_boolean = Mock(return_value=False)

    mock_chunk = Mock()
    mock_chunk.id = "chunk-123"
    knowledge_store.add_chunk = AsyncMock(return_value=mock_chunk)

    return {
        "template_repo": template_repo,
        "knowledge_store": knowledge_store,
        "renderer": renderer,
        "template": template,
    }


@pytest.mark.asyncio
async def test_deployment_event_has_30_day_retention(mock_deps):
    """Test deployment events have 30-day retention"""
    processor = GenericWebhookProcessor(
        template_repo=mock_deps["template_repo"],
        knowledge_store=mock_deps["knowledge_store"],
        renderer=mock_deps["renderer"],
    )

    mock_deps["template"].event_type = "deployment"

    await processor.process_webhook(
        connector_id="test-connector", event_type="deployment", payload={}, tenant_id="tenant-1"
    )

    # Check that chunk was created with 30-day expiration
    call_args = mock_deps["knowledge_store"].add_chunk.call_args[0][0]

    assert call_args.knowledge_type == KnowledgeType.EVENT
    assert call_args.expires_at is not None

    days_until_expiry = (call_args.expires_at - datetime.now(tz=UTC)).days
    assert 29 <= days_until_expiry <= 30  # Allow for timing


@pytest.mark.asyncio
async def test_pod_event_has_7_day_retention(mock_deps):
    """Test pod events have 7-day retention"""
    processor = GenericWebhookProcessor(
        template_repo=mock_deps["template_repo"],
        knowledge_store=mock_deps["knowledge_store"],
        renderer=mock_deps["renderer"],
    )

    mock_deps["template"].event_type = "pod_event"

    await processor.process_webhook(
        connector_id="test-connector", event_type="pod_event", payload={}, tenant_id="tenant-1"
    )

    call_args = mock_deps["knowledge_store"].add_chunk.call_args[0][0]

    days_until_expiry = (call_args.expires_at - datetime.now(tz=UTC)).days
    assert 6 <= days_until_expiry <= 7


@pytest.mark.asyncio
async def test_issue_events_have_higher_priority(mock_deps):
    """Test that events tagged as 'issue' get higher priority"""
    processor = GenericWebhookProcessor(
        template_repo=mock_deps["template_repo"],
        knowledge_store=mock_deps["knowledge_store"],
        renderer=mock_deps["renderer"],
    )

    # Mock renderer to add "issue" tag
    mock_deps["renderer"].render_tags = Mock(return_value=["tag1", "issue"])

    await processor.process_webhook(
        connector_id="test-connector", event_type="alert", payload={}, tenant_id="tenant-1"
    )

    call_args = mock_deps["knowledge_store"].add_chunk.call_args[0][0]

    assert call_args.priority == 10  # Issue events have priority 10


@pytest.mark.asyncio
async def test_normal_events_have_lower_priority(mock_deps):
    """Test that non-issue events have normal priority"""
    processor = GenericWebhookProcessor(
        template_repo=mock_deps["template_repo"],
        knowledge_store=mock_deps["knowledge_store"],
        renderer=mock_deps["renderer"],
    )

    # Normal event (no "issue" tag)
    mock_deps["renderer"].render_tags = Mock(return_value=["tag1"])

    await processor.process_webhook(
        connector_id="test-connector", event_type="push", payload={}, tenant_id="tenant-1"
    )

    call_args = mock_deps["knowledge_store"].add_chunk.call_args[0][0]

    assert call_args.priority == 5  # Normal events have priority 5


def test_retention_policies_configured():
    """Test that retention policies are configured for common event types"""
    # Check that common event types have retention defined
    assert EVENT_RETENTION_POLICIES["deployment"] == 30
    assert EVENT_RETENTION_POLICIES["push"] == 14
    assert EVENT_RETENTION_POLICIES["pod_event"] == 7
    assert EVENT_RETENTION_POLICIES["alert"] == 7
    assert EVENT_RETENTION_POLICIES["default"] == 7


def test_get_retention_days():
    """Test _get_retention_days method"""
    processor = GenericWebhookProcessor(template_repo=Mock(), knowledge_store=Mock())

    # Known event types
    assert processor._get_retention_days("deployment") == 30
    assert processor._get_retention_days("push") == 14
    assert processor._get_retention_days("pod_event") == 7

    # Unknown event type uses default
    assert processor._get_retention_days("unknown_event") == 7

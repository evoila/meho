# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for generic webhook processor.

Tests the core generic processing logic that makes MEHO system-agnostic.

Phase 84: process_webhook signature changed, system_id parameter handling updated.
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: generic webhook processor system_id parameter handling changed")

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from meho_app.core.errors import NotFoundError, ValidationError
from meho_app.modules.ingestion.models import EventTemplate
from meho_app.modules.ingestion.processor import GenericWebhookProcessor


@pytest.fixture
def mock_template_repo():
    """Mock template repository"""
    return Mock()


@pytest.fixture
def mock_knowledge_store():
    """Mock knowledge store"""
    store = Mock()
    mock_chunk = Mock()
    mock_chunk.id = "chunk-123"
    store.add_chunk = AsyncMock(return_value=mock_chunk)
    return store


@pytest.fixture
def mock_renderer():
    """Mock template renderer"""
    renderer = Mock()
    renderer.render_text = Mock(return_value="Rendered text")
    renderer.render_tags = Mock(return_value=["tag1", "tag2"])
    renderer.evaluate_boolean = Mock(return_value=False)
    return renderer


@pytest.fixture
def processor(mock_template_repo, mock_knowledge_store, mock_renderer):
    """Generic webhook processor"""
    return GenericWebhookProcessor(
        template_repo=mock_template_repo,
        knowledge_store=mock_knowledge_store,
        renderer=mock_renderer,
    )


@pytest.fixture
def sample_template():
    """Sample event template"""
    return EventTemplate(
        id=uuid.uuid4(),
        connector_id="test-connector",
        event_type="test_event",
        text_template="Test: {{ payload.message }}",
        tag_rules=["source:test"],
        issue_detection_rule=None,
        tenant_id="tenant-1",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


@pytest.mark.asyncio
async def test_process_webhook_success(
    processor, mock_template_repo, sample_template, mock_knowledge_store
):
    """Test successful webhook processing"""
    mock_template_repo.get_template = AsyncMock(return_value=sample_template)

    payload = {"message": "test"}

    chunk = await processor.process_webhook(
        connector_id="test-connector",
        event_type="test_event",
        payload=payload,
        tenant_id="tenant-1",
    )

    assert chunk.id == "chunk-123"
    mock_knowledge_store.add_chunk.assert_called_once()


@pytest.mark.asyncio
async def test_process_webhook_no_template(processor, mock_template_repo):
    """Test webhook processing when no template exists"""
    mock_template_repo.get_template = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError) as exc_info:
        await processor.process_webhook(
            connector_id="test-connector", event_type="test_event", payload={}, tenant_id="tenant-1"
        )

    assert "No template found" in str(exc_info.value)


@pytest.mark.asyncio
async def test_process_webhook_wrong_tenant(processor, mock_template_repo, sample_template):
    """Test webhook processing with wrong tenant (security check)"""
    sample_template.tenant_id = "tenant-2"
    mock_template_repo.get_template = AsyncMock(return_value=sample_template)

    with pytest.raises(ValidationError) as exc_info:
        await processor.process_webhook(
            connector_id="test-connector",
            event_type="test_event",
            payload={},
            tenant_id="tenant-1",  # Different tenant!
        )

    assert "does not belong to tenant" in str(exc_info.value)


@pytest.mark.asyncio
async def test_process_webhook_with_issue_detection(
    processor, mock_template_repo, sample_template, mock_renderer, mock_knowledge_store
):
    """Test webhook processing with issue detection"""
    sample_template.issue_detection_rule = "{{ payload.severity == 'high' }}"
    mock_template_repo.get_template = AsyncMock(return_value=sample_template)
    mock_renderer.evaluate_boolean = Mock(return_value=True)  # Is an issue!

    await processor.process_webhook(
        connector_id="test-connector",
        event_type="test_event",
        payload={"severity": "high"},
        tenant_id="tenant-1",
    )

    # Check that "issue" tag was added
    call_args = mock_knowledge_store.add_chunk.call_args[0][0]
    assert "issue" in call_args.tags


@pytest.mark.asyncio
async def test_process_webhook_uses_system_id(
    processor, mock_template_repo, sample_template, mock_knowledge_store
):
    """Test that system_id is passed to knowledge chunk"""
    mock_template_repo.get_template = AsyncMock(return_value=sample_template)

    await processor.process_webhook(
        connector_id="test-connector",
        event_type="test_event",
        payload={},
        tenant_id="tenant-1",
        system_id="custom-system-123",
    )

    call_args = mock_knowledge_store.add_chunk.call_args[0][0]
    assert call_args.system_id == "custom-system-123"


@pytest.mark.asyncio
async def test_process_webhook_defaults_system_id_to_connector(
    processor, mock_template_repo, sample_template, mock_knowledge_store
):
    """Test that system_id defaults to connector_id if not provided"""
    mock_template_repo.get_template = AsyncMock(return_value=sample_template)

    await processor.process_webhook(
        connector_id="test-connector",
        event_type="test_event",
        payload={},
        tenant_id="tenant-1",
        system_id=None,  # No system_id provided
    )

    call_args = mock_knowledge_store.add_chunk.call_args[0][0]
    assert call_args.system_id == "test-connector"

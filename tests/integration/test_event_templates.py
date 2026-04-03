# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for event templates.

Tests template CRUD operations with real database.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.errors import NotFoundError, ValidationError
from meho_app.modules.ingestion.repository import EventTemplateRepository
from meho_app.modules.ingestion.schemas import (
    EventTemplateCreate,
    EventTemplateFilter,
    EventTemplateUpdate,
)


@pytest.fixture
async def template_repo(db_session: AsyncSession):
    """Event template repository"""
    return EventTemplateRepository(db_session)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_template(template_repo):
    """Test creating an event template"""
    template_create = EventTemplateCreate(
        connector_id="test-connector",
        event_type="test_event",
        text_template="Test: {{ payload.message }}",
        tag_rules=["source:test", "type:{{ payload.type }}"],
        issue_detection_rule="{{ payload.severity == 'high' }}",
        tenant_id="tenant-1",
    )

    template = await template_repo.create_template(template_create)

    assert template.id is not None
    assert template.connector_id == "test-connector"
    assert template.event_type == "test_event"
    assert template.text_template == "Test: {{ payload.message }}"
    assert len(template.tag_rules) == 2
    assert template.tenant_id == "tenant-1"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_duplicate_template_fails(template_repo):
    """Test that creating duplicate template fails"""
    template_create = EventTemplateCreate(
        connector_id="test-connector",
        event_type="test_event",
        text_template="Test",
        tag_rules=[],
        tenant_id="tenant-1",
    )

    # Create first template
    await template_repo.create_template(template_create)

    # Try to create duplicate
    with pytest.raises(ValidationError) as exc_info:
        await template_repo.create_template(template_create)

    assert "already exists" in str(exc_info.value)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_template(template_repo):
    """Test getting template by connector_id and event_type"""
    template_create = EventTemplateCreate(
        connector_id="get-test",
        event_type="test_event",
        text_template="Test",
        tag_rules=[],
        tenant_id="tenant-1",
    )

    created = await template_repo.create_template(template_create)

    # Get by connector_id + event_type
    retrieved = await template_repo.get_template("get-test", "test_event")

    assert retrieved is not None
    assert retrieved.id == created.id
    assert retrieved.connector_id == "get-test"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_template_by_id(template_repo):
    """Test getting template by ID"""
    template_create = EventTemplateCreate(
        connector_id="id-test",
        event_type="test_event",
        text_template="Test",
        tag_rules=[],
        tenant_id="tenant-1",
    )

    created = await template_repo.create_template(template_create)

    # Get by ID
    retrieved = await template_repo.get_template_by_id(str(created.id))

    assert retrieved is not None
    assert retrieved.id == created.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_templates(template_repo):
    """Test listing templates with filtering"""
    # Create multiple templates
    await template_repo.create_template(
        EventTemplateCreate(
            connector_id="connector-1",
            event_type="event-1",
            text_template="Test 1",
            tag_rules=[],
            tenant_id="tenant-1",
        )
    )
    await template_repo.create_template(
        EventTemplateCreate(
            connector_id="connector-1",
            event_type="event-2",
            text_template="Test 2",
            tag_rules=[],
            tenant_id="tenant-1",
        )
    )
    await template_repo.create_template(
        EventTemplateCreate(
            connector_id="connector-2",
            event_type="event-1",
            text_template="Test 3",
            tag_rules=[],
            tenant_id="tenant-2",
        )
    )

    # List all for connector-1
    filter1 = EventTemplateFilter(connector_id="connector-1")
    templates1 = await template_repo.list_templates(filter1)
    assert len(templates1) == 2

    # List all for tenant-1
    filter2 = EventTemplateFilter(tenant_id="tenant-1")
    templates2 = await template_repo.list_templates(filter2)
    assert len(templates2) >= 2

    # List specific combination
    filter3 = EventTemplateFilter(connector_id="connector-1", event_type="event-1")
    templates3 = await template_repo.list_templates(filter3)
    assert len(templates3) == 1
    assert templates3[0].event_type == "event-1"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_template(template_repo):
    """Test updating a template"""
    template_create = EventTemplateCreate(
        connector_id="update-test",
        event_type="test_event",
        text_template="Original",
        tag_rules=["old"],
        tenant_id="tenant-1",
    )

    created = await template_repo.create_template(template_create)

    # Update template
    template_update = EventTemplateUpdate(text_template="Updated", tag_rules=["new", "tags"])

    updated = await template_repo.update_template(str(created.id), template_update)

    assert updated.id == created.id
    assert updated.text_template == "Updated"
    assert updated.tag_rules == ["new", "tags"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_nonexistent_template_fails(template_repo):
    """Test updating nonexistent template fails"""
    template_update = EventTemplateUpdate(text_template="Test")

    # Use valid UUID format that doesn't exist
    nonexistent_id = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(NotFoundError):
        await template_repo.update_template(nonexistent_id, template_update)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_template(template_repo):
    """Test deleting a template"""
    template_create = EventTemplateCreate(
        connector_id="delete-test",
        event_type="test_event",
        text_template="Test",
        tag_rules=[],
        tenant_id="tenant-1",
    )

    created = await template_repo.create_template(template_create)
    template_id = str(created.id)

    # Delete template
    await template_repo.delete_template(template_id)

    # Verify it's gone
    retrieved = await template_repo.get_template_by_id(template_id)
    assert retrieved is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_nonexistent_template_fails(template_repo):
    """Test deleting nonexistent template fails"""
    # Use valid UUID format that doesn't exist
    nonexistent_id = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(NotFoundError):
        await template_repo.delete_template(nonexistent_id)

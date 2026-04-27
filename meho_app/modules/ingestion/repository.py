# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Repository for event template CRUD operations.
"""

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.errors import NotFoundError, ValidationError
from meho_app.modules.ingestion.models import EventTemplate
from meho_app.modules.ingestion.schemas import (
    EventTemplateCreate,
    EventTemplateFilter,
    EventTemplateUpdate,
)


class EventTemplateRepository:
    """Repository for managing event templates"""

    def __init__(self, session: AsyncSession) -> None:
        """
        Initialize repository.

        Args:
            session: SQLAlchemy async session
        """
        self.session = session

    async def create_template(self, template_create: EventTemplateCreate) -> EventTemplate:
        """
        Create a new event template.

        Args:
            template_create: Template creation data

        Returns:
            Created event template

        Raises:
            ValidationError: If template with same connector_id + event_type exists
        """
        # Check if template already exists
        existing = await self.get_template(
            connector_id=template_create.connector_id,
            event_type=template_create.event_type,
        )

        if existing:
            raise ValidationError(
                f"Template already exists for {template_create.connector_id}/{template_create.event_type}"
            )

        template = EventTemplate(**template_create.model_dump())
        self.session.add(template)
        await self.session.flush()  # Flush changes, don't commit (session managed externally)
        await self.session.refresh(template)

        return template

    async def get_template(self, connector_id: str, event_type: str) -> EventTemplate | None:
        """
        Get event template by connector ID and event type.

        Args:
            connector_id: Connector ID
            event_type: Event type

        Returns:
            Event template or None if not found
        """
        result = await self.session.execute(
            select(EventTemplate).where(
                and_(
                    EventTemplate.connector_id == connector_id,
                    EventTemplate.event_type == event_type,
                )
            )
        )
        return result.scalar_one_or_none()

    async def get_template_by_id(self, template_id: str) -> EventTemplate | None:
        """
        Get event template by ID.

        Args:
            template_id: Template ID

        Returns:
            Event template or None if not found
        """
        result = await self.session.execute(
            select(EventTemplate).where(EventTemplate.id == template_id)
        )
        return result.scalar_one_or_none()

    async def list_templates(self, filter: EventTemplateFilter) -> list[EventTemplate]:
        """
        List event templates with filtering.

        Args:
            filter: Filter criteria

        Returns:
            List of event templates
        """
        query = select(EventTemplate)

        # Apply filters
        if filter.connector_id:
            query = query.where(EventTemplate.connector_id == filter.connector_id)
        if filter.event_type:
            query = query.where(EventTemplate.event_type == filter.event_type)
        if filter.tenant_id:
            query = query.where(EventTemplate.tenant_id == filter.tenant_id)

        # Pagination
        query = query.limit(filter.limit).offset(filter.offset)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def update_template(
        self, template_id: str, template_update: EventTemplateUpdate
    ) -> EventTemplate:
        """
        Update an event template.

        Args:
            template_id: Template ID
            template_update: Update data

        Returns:
            Updated event template

        Raises:
            NotFoundError: If template not found
        """
        template = await self.get_template_by_id(template_id)

        if not template:
            raise NotFoundError(f"Template {template_id} not found")

        # Update fields
        update_data = template_update.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(template, field, value)

        await self.session.flush()  # Flush changes, don't commit (session managed externally)
        await self.session.refresh(template)

        return template

    async def delete_template(self, template_id: str) -> None:
        """
        Delete an event template.

        Args:
            template_id: Template ID

        Raises:
            NotFoundError: If template not found
        """
        template = await self.get_template_by_id(template_id)

        if not template:
            raise NotFoundError(f"Template {template_id} not found")

        await self.session.delete(template)
        await self.session.flush()  # Flush changes, don't commit (session managed externally)

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
CRUD service for orchestrator skills.

Provides tenant-scoped create, read, update, delete operations for
orchestrator skills. Automatically generates/regenerates LLM summaries
when skill content changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.orchestrator_skills.models import OrchestratorSkillModel
from meho_app.modules.orchestrator_skills.schemas import (
    OrchestratorSkillCreate,
    OrchestratorSkillUpdate,
)
from meho_app.modules.orchestrator_skills.summary_generator import (
    generate_skill_summary,
)

logger = get_logger(__name__)


class OrchestratorSkillService:
    """Service for managing orchestrator skills.

    Uses a request-scoped ``AsyncSession`` from FastAPI ``Depends`` for
    all database operations. Summary generation is triggered automatically
    on create and on content update.

    Args:
        session: Request-scoped async database session.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_skills(self, tenant_id: str) -> list[OrchestratorSkillModel]:
        """List all orchestrator skills for a tenant.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            List of all skills (active and inactive) for the tenant.
        """
        result = await self.session.execute(
            select(OrchestratorSkillModel)
            .where(OrchestratorSkillModel.tenant_id == tenant_id)
            .order_by(OrchestratorSkillModel.name)
        )
        return list(result.scalars().all())

    async def list_active_skills(self, tenant_id: str) -> list[OrchestratorSkillModel]:
        """List active orchestrator skills for a tenant.

        Used for prompt injection -- only active skills are included
        in the orchestrator's system prompt.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            List of active skills for the tenant.
        """
        result = await self.session.execute(
            select(OrchestratorSkillModel)
            .where(
                OrchestratorSkillModel.tenant_id == tenant_id,
                OrchestratorSkillModel.is_active == True,  # noqa: E712
            )
            .order_by(OrchestratorSkillModel.name)
        )
        return list(result.scalars().all())

    async def get_skill(self, tenant_id: str, skill_id: UUID) -> OrchestratorSkillModel | None:
        """Get a specific orchestrator skill by ID.

        Args:
            tenant_id: Tenant identifier (enforces tenant isolation).
            skill_id: UUID of the skill.

        Returns:
            The skill model if found and belongs to tenant, None otherwise.
        """
        result = await self.session.execute(
            select(OrchestratorSkillModel).where(
                OrchestratorSkillModel.id == skill_id,
                OrchestratorSkillModel.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_connector_skill(
        self, tenant_id: str, connector_type: str
    ) -> OrchestratorSkillModel | None:
        """Get an active connector skill by connector type.

        Used by the orchestrator to resolve DB-backed connector skills
        before falling back to filesystem skill files (Phase 77).

        Args:
            tenant_id: Tenant identifier.
            connector_type: The connector type (e.g., "kubernetes", "vmware").

        Returns:
            The active connector skill model if found, None otherwise.
        """
        result = await self.session.execute(
            select(OrchestratorSkillModel).where(
                OrchestratorSkillModel.tenant_id == tenant_id,
                OrchestratorSkillModel.skill_type == "connector",
                OrchestratorSkillModel.connector_type == connector_type,
                OrchestratorSkillModel.is_active == True,  # noqa: E712
            )
        )
        return result.scalar_one_or_none()

    async def get_skill_by_name(self, tenant_id: str, name: str) -> OrchestratorSkillModel | None:
        """Get an orchestrator skill by name.

        Used by the orchestrator's read_skill action to load full content
        when a skill summary matches a routing decision.

        Args:
            tenant_id: Tenant identifier.
            name: Skill name (case-sensitive).

        Returns:
            The skill model if found, None otherwise.
        """
        result = await self.session.execute(
            select(OrchestratorSkillModel).where(
                OrchestratorSkillModel.tenant_id == tenant_id,
                OrchestratorSkillModel.name == name,
            )
        )
        return result.scalar_one_or_none()

    async def create_skill(
        self, tenant_id: str, data: OrchestratorSkillCreate
    ) -> OrchestratorSkillModel:
        """Create a new orchestrator skill.

        Generates an LLM summary automatically from the skill content.

        Args:
            tenant_id: Tenant identifier.
            data: Skill creation data (name, description, content).

        Returns:
            The created skill model with generated summary.
        """
        summary = await generate_skill_summary(data.name, data.content)
        now = datetime.now(UTC)

        skill = OrchestratorSkillModel(
            tenant_id=tenant_id,
            name=data.name,
            description=data.description,
            content=data.content,
            summary=summary,
            created_at=now,
            updated_at=now,
        )
        self.session.add(skill)
        await self.session.flush()
        return skill

    async def update_skill(
        self, tenant_id: str, skill_id: UUID, data: OrchestratorSkillUpdate
    ) -> OrchestratorSkillModel | None:
        """Update an orchestrator skill.

        Regenerates the LLM summary if content changes.

        Args:
            tenant_id: Tenant identifier (enforces tenant isolation).
            skill_id: UUID of the skill to update.
            data: Update data (all fields optional).

        Returns:
            The updated skill model, or None if not found.
        """
        skill = await self.get_skill(tenant_id, skill_id)
        if skill is None:
            return None

        update_fields = data.model_dump(exclude_unset=True)
        if not update_fields:
            return skill

        content_changed = False
        for field_name, value in update_fields.items():
            if field_name == "content" and value is not None:
                content_changed = True
            setattr(skill, field_name, value)

        # Mark as customized when content is edited via API (D-04: seeder won't overwrite)
        if content_changed:
            skill.is_customized = True

        # Regenerate summary if content or name changed
        if content_changed or "name" in update_fields:
            skill.summary = await generate_skill_summary(skill.name, skill.content)

        skill.updated_at = datetime.now(UTC)
        await self.session.flush()
        return skill

    async def delete_skill(self, tenant_id: str, skill_id: UUID) -> bool:
        """Delete an orchestrator skill.

        Args:
            tenant_id: Tenant identifier (enforces tenant isolation).
            skill_id: UUID of the skill to delete.

        Returns:
            True if deleted, False if not found.
        """
        result = await self.session.execute(
            delete(OrchestratorSkillModel).where(
                OrchestratorSkillModel.id == skill_id,
                OrchestratorSkillModel.tenant_id == tenant_id,
            )
        )
        return result.rowcount > 0

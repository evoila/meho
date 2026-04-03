# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SQLAlchemy model for orchestrator skills.

OrchestratorSkillModel stores cross-system investigation skills that are
injected into the orchestrator's system prompt. Each skill has LLM-generated
summaries for routing decisions and full markdown content for on-demand loading.
"""

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    Column,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from meho_app.database import Base


class OrchestratorSkillModel(Base):
    """
    Orchestrator-level cross-system reasoning skill.

    Tenant-scoped skills that the orchestrator uses for investigation patterns.
    Summaries are always present in the routing/synthesis prompts; full content
    is loaded on-demand when the orchestrator decides a skill is relevant.

    Fields:
        id: UUID primary key (gen_random_uuid).
        tenant_id: Tenant scope (NOT NULL, indexed).
        name: Human-readable skill name (max 255 chars).
        description: Optional description of the skill.
        content: Full skill markdown (NOT NULL).
        summary: LLM-generated 3-4 sentence summary for system prompt injection.
        is_active: Whether the skill is included in prompts (default True).
        is_customized: Whether the skill has been edited by an admin (Phase 77, D-04).
            When True, the seeder will not overwrite the content on restart.
        skill_type: "orchestrator" for cross-system skills, "connector" for
            connector-specific skills (Phase 77, D-05).
        connector_type: The connector type this skill belongs to (e.g., "kubernetes"),
            or None for orchestrator skills.
        created_at: Creation timestamp.
        updated_at: Last update timestamp.
    """

    __tablename__ = "orchestrator_skill"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default="gen_random_uuid()",
    )
    tenant_id = Column(String, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    content = Column(Text, nullable=False)
    summary = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, server_default="true")
    is_customized = Column(Boolean, nullable=False, server_default="false")
    skill_type = Column(
        String(50), nullable=False, server_default="orchestrator"
    )  # "orchestrator" or "connector"
    connector_type = Column(
        String(50), nullable=True
    )  # e.g., "kubernetes", null for orchestrator skills
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default="CURRENT_TIMESTAMP",
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default="CURRENT_TIMESTAMP",
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "skill_type", name="uq_orch_skill_tenant_name_type"),
        Index("ix_orch_skill_tenant", "tenant_id"),
        Index("ix_orch_skill_connector_type", "tenant_id", "skill_type", "connector_type"),
    )


__all__ = ["OrchestratorSkillModel"]

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
REST API endpoints for orchestrator skills CRUD and LLM-assisted generation.

All endpoints require JWT authentication via ``get_current_user``. Tenant
isolation is enforced by extracting ``tenant_id`` from the user context.
Permission: CONNECTOR_READ (orchestrator skills are operational, not admin).
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission
from meho_app.database import get_db_session
from meho_app.modules.orchestrator_skills.models import OrchestratorSkillModel
from meho_app.modules.orchestrator_skills.schemas import (
    GenerateSkillRequest,
    GenerateSkillResponse,
    OrchestratorSkillCreate,
    OrchestratorSkillResponse,
    OrchestratorSkillSummary,
    OrchestratorSkillUpdate,
)
from meho_app.modules.orchestrator_skills.service import OrchestratorSkillService

logger = get_logger(__name__)

router = APIRouter(prefix="/orchestrator-skills", tags=["orchestrator-skills"])


# ============================================================================
# Helpers
# ============================================================================


def _skill_to_response(skill: OrchestratorSkillModel) -> OrchestratorSkillResponse:
    """Convert a model to its full API response."""
    return OrchestratorSkillResponse(
        id=skill.id,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        tenant_id=skill.tenant_id,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        name=skill.name,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        description=skill.description,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        content=skill.content,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        summary=skill.summary,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        is_active=skill.is_active,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        is_customized=skill.is_customized,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        skill_type=skill.skill_type,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        connector_type=skill.connector_type,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        created_at=skill.created_at,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        updated_at=skill.updated_at,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
    )


def _skill_to_summary(skill: OrchestratorSkillModel) -> OrchestratorSkillSummary:
    """Convert a model to its lightweight summary response."""
    return OrchestratorSkillSummary(
        id=skill.id,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        name=skill.name,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        description=skill.description,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        is_active=skill.is_active,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
    )


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/", response_model=list[OrchestratorSkillSummary])
async def list_skills(
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_READ)),
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """List all orchestrator skills for the current tenant."""
    service = OrchestratorSkillService(db)
    skills = await service.list_skills(user.tenant_id)  # type: ignore[arg-type]
    return [_skill_to_summary(s) for s in skills]


@router.post("/", response_model=OrchestratorSkillResponse, status_code=201)
async def create_skill(
    body: OrchestratorSkillCreate,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_READ)),
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """Create a new orchestrator skill.

    Generates an LLM summary automatically from the skill content.
    """
    service = OrchestratorSkillService(db)
    try:
        skill = await service.create_skill(user.tenant_id, body)  # type: ignore[arg-type]
        await db.commit()
        await db.refresh(skill)
        return _skill_to_response(skill)
    except Exception as e:
        logger.error(f"Failed to create orchestrator skill: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create skill") from e


@router.get("/{skill_id}", response_model=OrchestratorSkillResponse)
async def get_skill(
    skill_id: UUID,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_READ)),
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """Get an orchestrator skill by ID."""
    service = OrchestratorSkillService(db)
    skill = await service.get_skill(user.tenant_id, skill_id)  # type: ignore[arg-type]
    if skill is None:
        raise HTTPException(status_code=404, detail="Orchestrator skill not found")
    return _skill_to_response(skill)


@router.put("/{skill_id}", response_model=OrchestratorSkillResponse)
async def update_skill(
    skill_id: UUID,
    body: OrchestratorSkillUpdate,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_READ)),
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """Update an orchestrator skill.

    Regenerates the LLM summary if content changes.
    """
    service = OrchestratorSkillService(db)
    skill = await service.update_skill(user.tenant_id, skill_id, body)  # type: ignore[arg-type]
    if skill is None:
        raise HTTPException(status_code=404, detail="Orchestrator skill not found")
    await db.commit()
    await db.refresh(skill)
    return _skill_to_response(skill)


@router.delete("/{skill_id}", status_code=204, response_model=None)
async def delete_skill(
    skill_id: UUID,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_READ)),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Delete an orchestrator skill."""
    service = OrchestratorSkillService(db)
    deleted = await service.delete_skill(user.tenant_id, skill_id)  # type: ignore[arg-type]
    if not deleted:
        raise HTTPException(status_code=404, detail="Orchestrator skill not found")
    await db.commit()
    return None


@router.post("/generate", response_model=GenerateSkillResponse)
async def generate_skill(  # NOSONAR (cognitive complexity)
    body: GenerateSkillRequest,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_READ)),
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """Generate orchestrator skill content using LLM.

    Context-aware: fetches all configured connectors' operation names
    and existing orchestrator skills, then instructs the LLM to create
    a cross-system investigation skill grounded in available operations.
    """
    try:
        from pydantic_ai import Agent

        # Fetch available context: connector operations + existing skills
        context_parts: list[str] = []

        # Get connector operations for context
        try:
            from sqlalchemy import select as sa_select

            from meho_app.modules.connectors.models import ConnectorOperationModel
            from meho_app.modules.connectors.repositories import ConnectorRepository

            # Fetch active connectors
            conn_repo = ConnectorRepository(db)
            connectors = await conn_repo.list_connectors(user.tenant_id)  # type: ignore[arg-type]
            if connectors:
                connector_lines = []
                for c in connectors:
                    if c.is_active:
                        connector_lines.append(f"- {c.name} (type: {c.connector_type})")
                if connector_lines:
                    context_parts.append("## Available Connectors\n" + "\n".join(connector_lines))

            # Fetch operation names grouped by connector
            ops_result = await db.execute(
                sa_select(ConnectorOperationModel)
                .where(ConnectorOperationModel.tenant_id == user.tenant_id)
                .order_by(
                    ConnectorOperationModel.connector_id,
                    ConnectorOperationModel.category,
                )
            )
            ops = ops_result.scalars().all()
            if ops:
                op_lines = []
                for op in ops:
                    op_lines.append(
                        f"- {op.operation_id}: {op.name}"
                        + (f" ({op.category})" if op.category else "")
                    )
                context_parts.append("## Available Operations\n" + "\n".join(op_lines))
        except Exception as e:
            logger.warning(f"Failed to fetch connector context for skill generation: {e}")

        # Get existing orchestrator skills for context
        try:
            service = OrchestratorSkillService(db)
            existing = await service.list_skills(user.tenant_id)  # type: ignore[arg-type]
            if existing:
                skill_lines = [f"- {s.name}: {s.summary}" for s in existing]
                context_parts.append("## Existing Orchestrator Skills\n" + "\n".join(skill_lines))
        except Exception as e:
            logger.warning(f"Failed to fetch existing skills for generation context: {e}")

        context_block = (
            "\n\n".join(context_parts)
            if context_parts
            else "No connectors or skills configured yet."
        )

        agent = Agent(
            "anthropic:claude-sonnet-4-6",
            system_prompt=(
                "You are a skill author for MEHO, an AI operations assistant that "
                "connects to infrastructure systems. Create an orchestrator-level "
                "cross-system investigation skill in markdown format.\n\n"
                "The skill should:\n"
                "1. Define clear investigation patterns (e.g., forward trace, backward trace)\n"
                "2. Reference specific operations from the available connectors\n"
                "3. Describe when to use the skill and what systems it covers\n"
                "4. Include intent classification (what user queries map to which patterns)\n"
                "5. Handle dead ends and suggest alternatives\n\n"
                "Be specific and grounded in the actual operations available. "
                "Return ONLY the skill content in markdown. No code fences around the entire output.\n\n"
                f"## Context\n\n{context_block}"
            ),
        )

        result = await asyncio.wait_for(
            agent.run(f"Create an orchestrator skill for: {body.user_description}"),
            timeout=360.0,
        )
        content = str(result.output).strip()
        return GenerateSkillResponse(content=content)

    except TimeoutError:
        raise HTTPException(
            status_code=422,
            detail="Skill generation timed out (360s). Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Orchestrator skill generation failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=422,
            detail=f"Failed to generate skill: {e}",
        ) from e

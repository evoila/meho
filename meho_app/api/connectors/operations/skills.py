# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connector skill operations.

Dedicated endpoints for saving custom skills and regenerating skills from operations.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.connectors.schemas import (
    ConnectorResponse,
    RegenerateSkillResponse,
    SaveSkillRequest,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

router = APIRouter()


def _dedup_custom_skill(
    custom_skill: str | None,
    generated_skill: str | None,
) -> str | None:
    """D-02: Dedup custom_skill against generated_skill at save time.

    If custom_skill content is identical to generated_skill (after strip),
    return None to clear it -- preventing silent duplication in the agent prompt.

    Args:
        custom_skill: The custom skill content being saved.
        generated_skill: The existing generated skill for comparison.

    Returns:
        None if custom matches generated (dedup), otherwise the original custom_skill.
    """
    if custom_skill is None:
        return None

    custom_content = custom_skill.strip()
    generated_content = (generated_skill or "").strip()

    if custom_content and generated_content and custom_content == generated_content:
        return None

    return custom_skill


@router.put("/{connector_id}/skill", response_model=ConnectorResponse)
async def save_custom_skill(
    connector_id: str,
    request: SaveSkillRequest,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_UPDATE)),
):
    """
    Save custom skill content for a connector.

    Operators can edit the generated skill or write a completely custom one.
    The custom_skill field takes priority over generated_skill when the
    SpecialistAgent selects which skill to use.
    """
    import uuid

    from sqlalchemy import select

    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.models import ConnectorModel
    from meho_app.modules.connectors.repositories import ConnectorRepository

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            # Fetch connector with tenant isolation
            query = select(ConnectorModel).where(
                ConnectorModel.id == uuid.UUID(connector_id),
                ConnectorModel.tenant_id == user.tenant_id,
            )
            result = await session.execute(query)
            db_connector = result.scalar_one_or_none()

            if not db_connector:
                raise HTTPException(status_code=404, detail="Connector not found")

            # D-02: If custom_skill is identical to generated_skill, clear to NULL
            deduped_skill = _dedup_custom_skill(
                request.custom_skill, db_connector.generated_skill
            )
            if deduped_skill is None and request.custom_skill:
                logger.info(
                    f"Cleared duplicate custom_skill for connector {connector_id}",
                    connector_id=connector_id,
                )
            db_connector.custom_skill = deduped_skill
            db_connector.updated_at = datetime.now(tz=UTC)

            await session.commit()
            await session.refresh(db_connector)

            # Convert to response using repository helper
            repo = ConnectorRepository(session)
            connector = await repo.get_connector(connector_id, tenant_id=user.tenant_id)

            logger.info(
                f"Saved custom skill for connector {connector_id}",
                connector_id=connector_id,
                skill_length=len(request.custom_skill),
            )

            return ConnectorResponse(**connector.model_dump())
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving custom skill for connector {connector_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


@router.post("/{connector_id}/skill/regenerate", response_model=RegenerateSkillResponse)
async def regenerate_skill(
    connector_id: str,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_UPDATE)),
):
    """
    Regenerate skill from current operations (OpenAPI endpoints or typed operations).

    Calls the skill generation pipeline to produce a new generated_skill from
    the connector's current operations. Requires at least one operation to exist.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.skill_generation import SkillGenerator

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            # Fetch connector with tenant isolation
            repo = ConnectorRepository(session)
            connector = await repo.get_connector(connector_id, tenant_id=user.tenant_id)

            if not connector:
                raise HTTPException(status_code=404, detail="Connector not found")

            # Generate skill using the pipeline
            generator = SkillGenerator()
            result = await generator.generate_skill(
                session=session,
                connector_id=connector_id,
                connector_type=connector.connector_type,
                connector_name=connector.name,
            )
            await session.commit()

            # Check if any operations were found
            if result.operation_count == 0:
                raise HTTPException(
                    status_code=400,
                    detail="This connector has no operations to generate a skill from.",
                )

            logger.info(
                f"Regenerated skill for connector {connector_id}: "
                f"quality={result.quality_score}/5, ops={result.operation_count}",
                connector_id=connector_id,
            )

            return RegenerateSkillResponse(
                generated_skill=result.skill_content,
                skill_quality_score=result.quality_score,
                operation_count=result.operation_count,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error regenerating skill for connector {connector_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e

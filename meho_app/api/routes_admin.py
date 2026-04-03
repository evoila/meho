# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Admin API Routes.

REST API for admin-level configuration:
- Tenant agent configuration (installation context, model overrides)
- Audit log
- Allowed models list
- Superadmin dashboard (stats and activity)
"""
# mypy: disable-error-code="arg-type"

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.api.auth import get_current_user
from meho_app.api.database import get_agent_session
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission
from meho_app.modules.agents.models import ChatSessionModel
from meho_app.modules.agents.tenant_config_repository import TenantConfigRepository
from meho_app.modules.connectors.models import ConnectorModel
from meho_app.modules.knowledge.job_models import IngestionJob as IngestionJobModel
from meho_app.modules.knowledge.models import KnowledgeChunkModel

logger = get_logger(__name__)

MODEL_CLAUDE_OPUS = "anthropic:claude-opus-4-6"

router = APIRouter(prefix="/admin", tags=["admin"])


# =============================================================================
# Request/Response Models
# =============================================================================


class TenantConfigRequest(BaseModel):
    """Request model for creating/updating tenant configuration."""

    installation_context: str | None = Field(
        None,
        description="Custom context to add to MEHO's system prompt. "
        "Describe your environment, systems, workflows, and priorities.",
        max_length=10000,  # Reasonable limit to prevent prompt injection
    )
    model_override: str | None = Field(
        None, description="Override the default LLM model (e.g., MODEL_CLAUDE_OPUS)"
    )
    temperature_override: float | None = Field(
        None,
        ge=0.0,
        le=2.0,
        description="Override the default temperature (0.0 = deterministic, 2.0 = creative)",
    )
    features: dict[str, Any] | None = Field(None, description="Feature flags for this tenant")


class TenantConfigResponse(BaseModel):
    """Response model for tenant configuration."""

    tenant_id: str
    installation_context: str | None = None
    model_override: str | None = None
    temperature_override: float | None = None
    features: dict[str, Any] = Field(default_factory=dict)
    updated_by: str | None = None
    updated_at: datetime | None = None
    created_at: datetime | None = None


class AuditLogEntry(BaseModel):
    """Single audit log entry."""

    field_changed: str
    old_value: str | None = None
    new_value: str | None = None
    changed_by: str
    changed_at: datetime


class AuditLogResponse(BaseModel):
    """Response model for audit log."""

    tenant_id: str
    entries: list[AuditLogEntry]


# =============================================================================
# Dashboard Models (Superadmin)
# =============================================================================


class DashboardStats(BaseModel):
    """System-wide statistics for superadmin dashboard."""

    total_tenants: int = Field(description="Total number of tenants")
    active_tenants: int = Field(description="Number of active tenants")
    total_connectors: int = Field(description="Total connectors across all tenants")
    workflows_today: int = Field(description="Chat sessions/workflows started today")
    knowledge_chunks: int = Field(description="Total knowledge chunks across all tenants")
    errors_today: int = Field(description="Failed ingestion jobs today")


class ActivityItem(BaseModel):
    """Single activity item for the activity feed."""

    id: str = Field(description="Unique identifier")
    type: str = Field(
        description="Activity type: tenant_created, connector_added, workflow_run, error"
    )
    description: str = Field(description="Human-readable description")
    tenant_id: str | None = Field(None, description="Associated tenant ID")
    timestamp: datetime = Field(description="When the activity occurred")


# =============================================================================
# Dependency injection
# =============================================================================


async def get_repository(
    session: Annotated[AsyncSession, Depends(get_agent_session)],
) -> TenantConfigRepository:
    """Get a TenantConfigRepository instance with database session."""
    return TenantConfigRepository(session)


# =============================================================================
# API Endpoints
# =============================================================================


@router.get(
    "/config",
    summary="Get tenant configuration",
    description="Get the agent configuration for the current tenant.",
)
async def get_config(
    user: Annotated[UserContext, Depends(get_current_user)],
    repository: Annotated[TenantConfigRepository, Depends(get_repository)],
) -> TenantConfigResponse:
    """Get tenant agent configuration."""

    config = await repository.get_config(user.tenant_id)

    if not config:
        # Return empty config if not set
        return TenantConfigResponse(tenant_id=user.tenant_id)

    temp_value = None
    if config.temperature_override:
        temp_value = config.temperature_override.get("value")

    return TenantConfigResponse(
        tenant_id=config.tenant_id,
        installation_context=config.installation_context,
        model_override=config.model_override,
        temperature_override=temp_value,
        features=config.features or {},
        updated_by=config.updated_by,
        updated_at=config.updated_at,
        created_at=config.created_at,
    )


@router.put(
    "/config",
    summary="Update tenant configuration",
    description="Create or update the agent configuration for the current tenant.",
    responses={400: {"description": "Invalid model"}},
)
async def update_config(
    request: TenantConfigRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.ADMIN_CONFIG))],
    repository: Annotated[TenantConfigRepository, Depends(get_repository)],
    session: Annotated[AsyncSession, Depends(get_agent_session)],
) -> TenantConfigResponse:
    """Update tenant agent configuration."""

    # Validate model if provided
    if request.model_override:
        allowed_models = [
            "anthropic:claude-3-sonnet",
            "anthropic:claude-3-opus",
            "anthropic:claude-3-haiku",
            MODEL_CLAUDE_OPUS,
            "anthropic:claude-sonnet-4-6",
        ]
        if request.model_override not in allowed_models:
            raise HTTPException(
                status_code=400, detail=f"Invalid model. Allowed models: {allowed_models}"
            )

    config = await repository.create_or_update(
        tenant_id=user.tenant_id,
        installation_context=request.installation_context,
        model_override=request.model_override,
        temperature_override=request.temperature_override,
        features=request.features,
        updated_by=user.user_id,
    )

    await session.commit()

    # Audit: log config update with old/new values
    try:
        from meho_app.modules.audit.service import AuditService

        audit = AuditService(session)
        # Build details with changed fields
        changed_fields = dict(request.model_dump(exclude_unset=True).items())
        await audit.log_event(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            user_email=getattr(user, "email", None),
            event_type="config.update",
            action="update",
            resource_type="config",
            resource_name="tenant_config",
            details={"fields_changed": list(changed_fields.keys())},
            result="success",
        )
        await session.commit()
    except Exception as audit_err:
        logger.warning(f"Audit logging failed for config update: {audit_err}")

    temp_value = None
    if config.temperature_override:
        temp_value = config.temperature_override.get("value")

    logger.info(f"Updated tenant config for {user.tenant_id} by {user.user_id}")

    return TenantConfigResponse(
        tenant_id=config.tenant_id,
        installation_context=config.installation_context,
        model_override=config.model_override,
        temperature_override=temp_value,
        features=config.features or {},
        updated_by=config.updated_by,
        updated_at=config.updated_at,
        created_at=config.created_at,
    )


@router.delete(
    "/config",
    summary="Delete tenant configuration",
    description="Delete the agent configuration for the current tenant (reverts to defaults).",
    responses={404: {"description": "Configuration not found"}},
)
async def delete_config(
    user: Annotated[UserContext, Depends(RequirePermission(Permission.ADMIN_CONFIG))],
    repository: Annotated[TenantConfigRepository, Depends(get_repository)],
    session: Annotated[AsyncSession, Depends(get_agent_session)],
) -> dict[str, str]:
    """Delete tenant agent configuration."""

    deleted = await repository.delete_config(user.tenant_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Configuration not found")

    await session.commit()

    logger.info(f"Deleted tenant config for {user.tenant_id} by {user.user_id}")

    return {"status": "deleted", "tenant_id": user.tenant_id or ""}


@router.get(
    "/config/audit",
    summary="Get configuration audit log",
    description="Get the audit log of configuration changes for the current tenant.",
)
async def get_audit_log(
    user: Annotated[UserContext, Depends(get_current_user)],
    repository: Annotated[TenantConfigRepository, Depends(get_repository)],
    limit: int = 50,
) -> AuditLogResponse:
    """Get tenant configuration audit log."""

    entries = await repository.get_audit_log(user.tenant_id, limit=limit)

    return AuditLogResponse(
        tenant_id=user.tenant_id,
        entries=[
            AuditLogEntry(
                field_changed=e.field_changed,
                old_value=e.old_value,
                new_value=e.new_value,
                changed_by=e.changed_by,
                changed_at=e.changed_at,
            )
            for e in entries
        ],
    )


@router.get(
    "/models", summary="Get allowed models", description="Get the list of allowed LLM models."
)
async def get_allowed_models() -> dict[str, Any]:
    """Get allowed LLM models."""

    return {
        "allowed_models": [
            {
                "id": "anthropic:claude-3-sonnet",
                "name": "Claude 3 Sonnet",
                "provider": "Anthropic",
                "recommended": False,
            },
            {
                "id": "anthropic:claude-3-opus",
                "name": "Claude 3 Opus",
                "provider": "Anthropic",
                "recommended": False,
            },
            {
                "id": "anthropic:claude-3-haiku",
                "name": "Claude 3 Haiku",
                "provider": "Anthropic",
                "recommended": False,
            },
            {
                "id": MODEL_CLAUDE_OPUS,
                "name": "Claude Opus 4.6",
                "provider": "Anthropic",
                "recommended": True,
            },
            {
                "id": "anthropic:claude-sonnet-4-6",
                "name": "Claude Sonnet 4.6",
                "provider": "Anthropic",
                "recommended": True,
            },
        ],
        "default_model": MODEL_CLAUDE_OPUS,
        "note": "Environment variable LLM_MODEL can override the default.",
    }


# =============================================================================
# Dashboard Endpoints (Superadmin)
# =============================================================================


@router.get(
    "/dashboard/stats",
    summary="Get dashboard statistics",
    description="Get system-wide statistics for the superadmin dashboard. Requires global_admin role.",
)
async def get_dashboard_stats(
    user: Annotated[UserContext, Depends(RequirePermission(Permission.TENANT_LIST))],
    session: Annotated[AsyncSession, Depends(get_agent_session)],
    repository: Annotated[TenantConfigRepository, Depends(get_repository)],
) -> DashboardStats:
    """Get system-wide statistics for superadmin dashboard."""

    # Get tenant counts
    all_tenants = await repository.list_all_tenants(include_inactive=True)
    active_tenants = await repository.list_all_tenants(include_inactive=False)

    # Count connectors across all tenants
    connector_count_result = await session.execute(select(func.count()).select_from(ConnectorModel))
    total_connectors = connector_count_result.scalar() or 0

    # Count knowledge chunks across all tenants
    knowledge_count_result = await session.execute(
        select(func.count()).select_from(KnowledgeChunkModel)
    )
    knowledge_chunks = knowledge_count_result.scalar() or 0

    # Count chat sessions (workflows) created today
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    workflows_today_result = await session.execute(
        select(func.count())
        .select_from(ChatSessionModel)
        .where(ChatSessionModel.created_at >= today_start)
    )
    workflows_today = workflows_today_result.scalar() or 0

    # Count failed ingestion jobs today
    errors_today_result = await session.execute(
        select(func.count())
        .select_from(IngestionJobModel)
        .where(IngestionJobModel.status == "failed", IngestionJobModel.completed_at >= today_start)
    )
    errors_today = errors_today_result.scalar() or 0

    logger.info(f"Dashboard stats fetched by {user.user_id}")

    return DashboardStats(
        total_tenants=len(all_tenants),
        active_tenants=len(active_tenants),
        total_connectors=total_connectors,
        workflows_today=workflows_today,
        knowledge_chunks=knowledge_chunks,
        errors_today=errors_today,
    )


@router.get(
    "/dashboard/activity",
    summary="Get recent activity feed",
    description="Get recent system activity for the superadmin dashboard. Requires global_admin role.",
)
async def get_dashboard_activity(
    user: Annotated[UserContext, Depends(RequirePermission(Permission.TENANT_LIST))],
    session: Annotated[AsyncSession, Depends(get_agent_session)],
    repository: Annotated[TenantConfigRepository, Depends(get_repository)],
    limit: Annotated[int, Query(ge=1, le=100, description="Maximum number of activity items")] = 20,
) -> list[ActivityItem]:
    """Get recent activity feed for superadmin dashboard."""

    activities: list[ActivityItem] = []

    # Get recently created tenants
    all_tenants = await repository.list_all_tenants(include_inactive=True)
    for tenant in sorted(all_tenants, key=lambda t: t.created_at or datetime.min, reverse=True)[:5]:  # noqa: DTZ901 -- datetime boundary value
        if tenant.created_at:
            activities.append(
                ActivityItem(
                    id=f"tenant-{tenant.tenant_id}",
                    type="tenant_created",
                    description=f"Tenant '{tenant.display_name or tenant.tenant_id}' was created",
                    tenant_id=tenant.tenant_id,
                    timestamp=tenant.created_at,
                )
            )

    # Get recent chat sessions (workflows)
    recent_sessions_result = await session.execute(
        select(ChatSessionModel).order_by(ChatSessionModel.created_at.desc()).limit(10)
    )
    recent_sessions = recent_sessions_result.scalars().all()
    for chat_session in recent_sessions:
        activities.append(
            ActivityItem(
                id=f"workflow-{chat_session.id}",
                type="workflow_run",
                description=f"Workflow '{chat_session.title or 'Untitled'}' started",
                tenant_id=chat_session.tenant_id,
                timestamp=chat_session.created_at,
            )
        )

    # Get recent connector creations
    recent_connectors_result = await session.execute(
        select(ConnectorModel).order_by(ConnectorModel.created_at.desc()).limit(5)
    )
    recent_connectors = recent_connectors_result.scalars().all()
    for connector in recent_connectors:
        activities.append(
            ActivityItem(
                id=f"connector-{connector.id}",
                type="connector_added",
                description=f"Connector '{connector.name}' added ({connector.connector_type})",
                tenant_id=connector.tenant_id,
                timestamp=connector.created_at,
            )
        )

    # Get recent failed jobs
    recent_errors_result = await session.execute(
        select(IngestionJobModel)
        .where(IngestionJobModel.status == "failed")
        .order_by(IngestionJobModel.completed_at.desc())
        .limit(5)
    )
    recent_errors = recent_errors_result.scalars().all()
    for job in recent_errors:
        if job.completed_at:
            activities.append(
                ActivityItem(
                    id=f"error-{job.id}",
                    type="error",
                    description=f"Ingestion job failed: {job.status_message or 'Unknown error'}",
                    tenant_id=job.tenant_id,
                    timestamp=job.completed_at,
                )
            )

    # Sort all activities by timestamp and limit
    activities.sort(key=lambda a: a.timestamp, reverse=True)

    logger.info(f"Dashboard activity fetched by {user.user_id}")

    return activities[:limit]

"""
Admin API Routes.

REST API for admin-level configuration:
- Tenant agent configuration (installation context, model overrides)
- Prompt preview
- Audit log

TASK-77: Externalize Prompts & Models
"""
# mypy: disable-error-code="arg-type"

from __future__ import annotations

import logging
from typing import Any, Optional, Dict, List
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from meho_agent.tenant_config_repository import TenantConfigRepository
from meho_agent.agent_config import AgentConfig, PromptBuilder
from meho_api.database import get_agent_session
from meho_core.auth_context import UserContext
from meho_api.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# =============================================================================
# Request/Response Models
# =============================================================================


class TenantConfigRequest(BaseModel):
    """Request model for creating/updating tenant configuration."""
    
    installation_context: Optional[str] = Field(
        None,
        description="Custom context to add to MEHO's system prompt. "
                    "Describe your environment, systems, workflows, and priorities.",
        max_length=10000,  # Reasonable limit to prevent prompt injection
    )
    model_override: Optional[str] = Field(
        None,
        description="Override the default LLM model (e.g., 'openai:gpt-4.1')"
    )
    temperature_override: Optional[float] = Field(
        None,
        ge=0.0,
        le=2.0,
        description="Override the default temperature (0.0 = deterministic, 2.0 = creative)"
    )
    features: Optional[Dict[str, Any]] = Field(
        None,
        description="Feature flags for this tenant"
    )


class TenantConfigResponse(BaseModel):
    """Response model for tenant configuration."""
    
    tenant_id: str
    installation_context: Optional[str] = None
    model_override: Optional[str] = None
    temperature_override: Optional[float] = None
    features: Dict[str, Any] = Field(default_factory=dict)
    updated_by: Optional[str] = None
    updated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class AuditLogEntry(BaseModel):
    """Single audit log entry."""
    
    field_changed: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    changed_by: str
    changed_at: datetime


class AuditLogResponse(BaseModel):
    """Response model for audit log."""
    
    tenant_id: str
    entries: List[AuditLogEntry]


class PromptPreviewResponse(BaseModel):
    """Response model for prompt preview."""
    
    system_prompt: str
    character_count: int
    has_tenant_context: bool
    model: str
    temperature: float


# =============================================================================
# Dependency injection
# =============================================================================


async def get_repository(
    session: AsyncSession = Depends(get_agent_session)
) -> TenantConfigRepository:
    """Get a TenantConfigRepository instance with database session."""
    return TenantConfigRepository(session)


# =============================================================================
# API Endpoints
# =============================================================================


@router.get(
    "/config",
    response_model=TenantConfigResponse,
    summary="Get tenant configuration",
    description="Get the agent configuration for the current tenant."
)
async def get_config(
    user: UserContext = Depends(get_current_user),
    repository: TenantConfigRepository = Depends(get_repository),
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
    response_model=TenantConfigResponse,
    summary="Update tenant configuration",
    description="Create or update the agent configuration for the current tenant."
)
async def update_config(
    request: TenantConfigRequest,
    user: UserContext = Depends(get_current_user),
    repository: TenantConfigRepository = Depends(get_repository),
    session: AsyncSession = Depends(get_agent_session),
) -> TenantConfigResponse:
    """Update tenant agent configuration."""
    
    # Validate model if provided
    if request.model_override:
        allowed_models = [
            "openai:gpt-4.1-mini",
            "openai:gpt-4.1",
            "openai:gpt-5-mini",
            "openai:o1-mini",
            "openai:o1-preview",
            "anthropic:claude-3-sonnet",
            "anthropic:claude-3-opus",
            "anthropic:claude-3-haiku",
            "anthropic:claude-4-sonnet",
        ]
        if request.model_override not in allowed_models:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid model. Allowed models: {allowed_models}"
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
    description="Delete the agent configuration for the current tenant (reverts to defaults)."
)
async def delete_config(
    user: UserContext = Depends(get_current_user),
    repository: TenantConfigRepository = Depends(get_repository),
    session: AsyncSession = Depends(get_agent_session),
) -> Dict[str, str]:
    """Delete tenant agent configuration."""
    
    deleted = await repository.delete_config(user.tenant_id)
    
    if not deleted:
        raise HTTPException(status_code=404, detail="Configuration not found")
    
    await session.commit()
    
    logger.info(f"Deleted tenant config for {user.tenant_id} by {user.user_id}")
    
    return {"status": "deleted", "tenant_id": user.tenant_id or ""}


@router.get(
    "/config/audit",
    response_model=AuditLogResponse,
    summary="Get configuration audit log",
    description="Get the audit log of configuration changes for the current tenant."
)
async def get_audit_log(
    user: UserContext = Depends(get_current_user),
    repository: TenantConfigRepository = Depends(get_repository),
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
        ]
    )


@router.get(
    "/prompt/preview",
    response_model=PromptPreviewResponse,
    summary="Preview composed system prompt",
    description="Preview the complete system prompt that would be used for the current tenant."
)
async def preview_prompt(
    user: UserContext = Depends(get_current_user),
    repository: TenantConfigRepository = Depends(get_repository),
) -> PromptPreviewResponse:
    """Preview the composed system prompt."""
    
    # Get tenant context
    context = await repository.get_installation_context(user.tenant_id)
    
    # Load config and build prompt
    config = await AgentConfig.load(tenant_id=user.tenant_id)
    
    # If we have DB context, add it to the config
    if context:
        config.tenant_context = context
    
    builder = PromptBuilder(config)
    system_prompt = await builder.build()
    
    return PromptPreviewResponse(
        system_prompt=system_prompt,
        character_count=len(system_prompt),
        has_tenant_context=context is not None,
        model=config.model.name,
        temperature=config.model.temperature,
    )


@router.get(
    "/models",
    summary="Get allowed models",
    description="Get the list of allowed LLM models."
)
async def get_allowed_models() -> Dict[str, Any]:
    """Get allowed LLM models."""
    
    return {
        "allowed_models": [
            {"id": "openai:gpt-4.1-mini", "name": "GPT-4o Mini", "provider": "OpenAI", "recommended": True},
            {"id": "openai:gpt-4.1", "name": "GPT-4o", "provider": "OpenAI", "recommended": False},
            {"id": "openai:gpt-5-mini", "name": "GPT-5 Mini", "provider": "OpenAI", "recommended": True},
            {"id": "openai:o1-mini", "name": "o1 Mini (Reasoning)", "provider": "OpenAI", "recommended": False},
            {"id": "openai:o1-preview", "name": "o1 Preview (Reasoning)", "provider": "OpenAI", "recommended": False},
            {"id": "anthropic:claude-3-sonnet", "name": "Claude 3 Sonnet", "provider": "Anthropic", "recommended": False},
            {"id": "anthropic:claude-3-opus", "name": "Claude 3 Opus", "provider": "Anthropic", "recommended": False},
            {"id": "anthropic:claude-3-haiku", "name": "Claude 3 Haiku", "provider": "Anthropic", "recommended": False},
            {"id": "anthropic:claude-4-sonnet", "name": "Claude 4 Sonnet", "provider": "Anthropic", "recommended": False},
        ],
        "default_model": "openai:gpt-4.1-mini",
        "note": "Environment variable STREAMING_AGENT_MODEL can override the default."
    }


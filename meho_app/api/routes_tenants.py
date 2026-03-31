# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tenant Management API Routes.

TASK-139 Phase 4: Tenant Management API

REST API for global_admin tenant management:
- List all tenants
- Create new tenant (creates Keycloak realm + MEHO config)
- Update tenant settings
- Disable/enable tenant

All endpoints require global_admin role.
"""
# mypy: disable-error-code="arg-type"

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from keycloak.exceptions import KeycloakError
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.api.database import get_agent_session
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission
from meho_app.modules.agents.keycloak_admin import KeycloakTenantManager, get_keycloak_manager
from meho_app.modules.agents.tenant_config_repository import TenantConfigRepository

logger = get_logger(__name__)

router = APIRouter(prefix="/tenants", tags=["tenants"])


# =============================================================================
# Request/Response Models
# =============================================================================


class TenantResponse(BaseModel):
    """Response model for a tenant."""

    tenant_id: str = Field(description="Unique tenant identifier (realm name)")
    display_name: str | None = Field(None, description="Human-readable display name")
    is_active: bool = Field(True, description="Whether the tenant is active")
    subscription_tier: str = Field("free", description="Subscription tier")

    # Email domains for tenant discovery (TASK-139 Phase 8)
    email_domains: list[str] = Field(
        default_factory=list,
        description="Email domains for tenant discovery (e.g., ['acme.com', 'acme.org'])",
    )

    # Quotas
    max_connectors: int | None = Field(None, description="Max connectors (null=unlimited)")
    max_knowledge_chunks: int | None = Field(
        None, description="Max knowledge chunks (null=unlimited)"
    )
    max_workflows_per_day: int | None = Field(
        None, description="Max workflow executions per day (null=unlimited)"
    )

    # LLM settings
    installation_context: str | None = Field(None, description="Custom system prompt context")
    model_override: str | None = Field(None, description="LLM model override")
    temperature_override: float | None = Field(None, description="Temperature override")
    features: dict[str, Any] = Field(default_factory=dict, description="Feature flags")

    # Metadata
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None

    # Keycloak realm status (optional, only if realm management enabled)
    keycloak_realm_enabled: bool | None = Field(None, description="Keycloak realm enabled status")


class CreateTenantRequest(BaseModel):
    """Request model for creating a new tenant."""

    tenant_id: str = Field(
        ...,
        min_length=3,
        max_length=63,
        description="Unique tenant identifier (slug, e.g., 'acme-corp')",
    )
    display_name: str = Field(
        ..., min_length=1, max_length=255, description="Human-readable display name"
    )
    subscription_tier: str = Field(
        default="free", description="Subscription tier (free, pro, enterprise)"
    )

    # Email domains for tenant discovery (TASK-139 Phase 8)
    email_domains: list[str] | None = Field(
        None, description="Email domains for tenant discovery (e.g., ['acme.com', 'acme.org'])"
    )

    # Optional quotas
    max_connectors: int | None = Field(None, ge=0, description="Max connectors")
    max_knowledge_chunks: int | None = Field(None, ge=0, description="Max knowledge chunks")
    max_workflows_per_day: int | None = Field(None, ge=0, description="Max workflows per day")

    # Optional LLM settings
    installation_context: str | None = Field(
        None, max_length=10000, description="Custom context for system prompt"
    )
    model_override: str | None = Field(None, description="LLM model override")
    temperature_override: float | None = Field(
        None, ge=0.0, le=2.0, description="Temperature override"
    )
    features: dict[str, Any] | None = Field(None, description="Feature flags")

    # Keycloak realm creation
    create_keycloak_realm: bool = Field(
        default=True, description="Create Keycloak realm for this tenant"
    )

    @field_validator("tenant_id")
    @classmethod
    def validate_tenant_id(cls, v: str) -> str:
        """Validate tenant_id is a valid slug (alphanumeric, hyphens, converted to lowercase)."""
        # Convert to lowercase first for validation
        v_lower = v.lower()

        if len(v_lower) > 2 and not re.match(r"^[a-z][a-z0-9-]*[a-z0-9]$", v_lower):
            raise ValueError(
                "tenant_id must be a valid slug: letters, numbers, and hyphens only, "
                "must start with a letter and end with a letter or number"
            )
        # Reserved realm names
        reserved = {"master", "admin", "keycloak", "meho", "system"}
        if v_lower in reserved:
            raise ValueError(f"tenant_id '{v}' is reserved")
        return v_lower

    @field_validator("subscription_tier")
    @classmethod
    def validate_subscription_tier(cls, v: str) -> str:
        """Validate subscription tier is valid."""
        valid_tiers = {"free", "pro", "enterprise"}
        if v.lower() not in valid_tiers:
            raise ValueError(f"subscription_tier must be one of: {valid_tiers}")
        return v.lower()


class UpdateTenantRequest(BaseModel):
    """Request model for updating a tenant."""

    display_name: str | None = Field(None, min_length=1, max_length=255)
    subscription_tier: str | None = Field(None)
    is_active: bool | None = Field(None)

    # Email domains for tenant discovery (TASK-139 Phase 8)
    email_domains: list[str] | None = Field(
        None, description="Email domains for tenant discovery (e.g., ['acme.com', 'acme.org'])"
    )

    # Quotas
    max_connectors: int | None = Field(None, ge=0)
    max_knowledge_chunks: int | None = Field(None, ge=0)
    max_workflows_per_day: int | None = Field(None, ge=0)

    # LLM settings
    installation_context: str | None = Field(None, max_length=10000)
    model_override: str | None = Field(None)
    temperature_override: float | None = Field(None, ge=0.0, le=2.0)
    features: dict[str, Any] | None = Field(None)

    @field_validator("subscription_tier")
    @classmethod
    def validate_subscription_tier(cls, v: str | None) -> str | None:
        """Validate subscription tier is valid."""
        if v is None:
            return None
        valid_tiers = {"free", "pro", "enterprise"}
        if v.lower() not in valid_tiers:
            raise ValueError(f"subscription_tier must be one of: {valid_tiers}")
        return v.lower()


class TenantListResponse(BaseModel):
    """Response model for tenant list."""

    tenants: list[TenantResponse]
    total: int


# =============================================================================
# Dependency Injection
# =============================================================================


async def get_repository(
    session: AsyncSession = Depends(get_agent_session),
) -> TenantConfigRepository:
    """Get a TenantConfigRepository instance with database session."""
    return TenantConfigRepository(session)


def get_keycloak() -> KeycloakTenantManager | None:
    """
    Get KeycloakTenantManager instance.

    Returns None if Keycloak admin password is not configured.
    """
    try:
        manager = get_keycloak_manager()
        # Check if password is configured
        from meho_app.api.config import get_api_config

        config = get_api_config()
        if not config.keycloak_admin_password:
            logger.warning("Keycloak admin password not configured, realm management disabled")
            return None
        return manager
    except Exception as e:
        logger.warning(f"Could not initialize Keycloak manager: {e}")
        return None


# =============================================================================
# Helper Functions
# =============================================================================


def _config_to_response(
    config: Any,
    keycloak_realm_enabled: bool | None = None,
) -> TenantResponse:
    """Convert TenantAgentConfig model to TenantResponse."""
    temp_value = None
    if config.temperature_override:
        temp_value = config.temperature_override.get("value")

    return TenantResponse(
        tenant_id=config.tenant_id,
        display_name=config.display_name,
        is_active=config.is_active,
        subscription_tier=config.subscription_tier or "free",
        email_domains=config.email_domains or [],
        max_connectors=config.max_connectors,
        max_knowledge_chunks=config.max_knowledge_chunks,
        max_workflows_per_day=config.max_workflows_per_day,
        installation_context=config.installation_context,
        model_override=config.model_override,
        temperature_override=temp_value,
        features=config.features or {},
        created_at=config.created_at,
        updated_at=config.updated_at,
        updated_by=config.updated_by,
        keycloak_realm_enabled=keycloak_realm_enabled,
    )


# =============================================================================
# API Endpoints
# =============================================================================


@router.get(
    "",
    response_model=TenantListResponse,
    summary="List all tenants",
    description="List all tenants in the system. Requires global_admin role.",
)
async def list_tenants(
    include_inactive: bool = False,
    user: UserContext = Depends(RequirePermission(Permission.TENANT_LIST)),
    repository: TenantConfigRepository = Depends(get_repository),
) -> TenantListResponse:
    """List all tenants."""
    tenants = await repository.list_all_tenants(include_inactive=include_inactive)

    return TenantListResponse(
        tenants=[_config_to_response(t) for t in tenants],
        total=len(tenants),
    )


@router.post(
    "",
    response_model=TenantResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new tenant",
    description="Create a new tenant with optional Keycloak realm. Requires global_admin role.",
)
async def create_tenant(
    request: CreateTenantRequest,
    user: UserContext = Depends(RequirePermission(Permission.TENANT_CREATE)),
    repository: TenantConfigRepository = Depends(get_repository),
    session: AsyncSession = Depends(get_agent_session),
    keycloak: KeycloakTenantManager | None = Depends(get_keycloak),
) -> TenantResponse:
    """Create a new tenant."""

    # Check if tenant already exists
    existing = await repository.get_config(request.tenant_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tenant '{request.tenant_id}' already exists",
        )

    keycloak_realm_enabled: bool | None = None

    # Create Keycloak realm if requested and Keycloak is configured
    if request.create_keycloak_realm and keycloak:
        try:
            # Check if realm already exists
            if keycloak.realm_exists(request.tenant_id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Keycloak realm '{request.tenant_id}' already exists",
                )

            keycloak.create_realm(
                tenant_id=request.tenant_id,
                display_name=request.display_name,
                enabled=True,
            )
            keycloak_realm_enabled = True
            logger.info(f"Created Keycloak realm for tenant: {request.tenant_id}")

        except KeycloakError as e:
            logger.error(f"Failed to create Keycloak realm: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create Keycloak realm: {e!s}",
            ) from e

    # Create tenant config in MEHO database
    config = await repository.create_tenant(
        tenant_id=request.tenant_id,
        display_name=request.display_name,
        subscription_tier=request.subscription_tier,
        email_domains=request.email_domains,
        max_connectors=request.max_connectors,
        max_knowledge_chunks=request.max_knowledge_chunks,
        max_workflows_per_day=request.max_workflows_per_day,
        installation_context=request.installation_context,
        model_override=request.model_override,
        temperature_override=request.temperature_override,
        features=request.features,
        created_by=user.user_id,
    )

    await session.commit()

    logger.info(f"Created tenant: {request.tenant_id} by {user.user_id}")

    return _config_to_response(config, keycloak_realm_enabled)


@router.get(
    "/{tenant_id}",
    response_model=TenantResponse,
    summary="Get tenant details",
    description="Get details of a specific tenant. Requires global_admin role.",
)
async def get_tenant(
    tenant_id: str,
    user: UserContext = Depends(RequirePermission(Permission.TENANT_LIST)),
    repository: TenantConfigRepository = Depends(get_repository),
    keycloak: KeycloakTenantManager | None = Depends(get_keycloak),
) -> TenantResponse:
    """Get tenant details."""
    config = await repository.get_config(tenant_id)

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Tenant '{tenant_id}' not found"
        )

    # Get Keycloak realm status if available
    keycloak_realm_enabled: bool | None = None
    if keycloak:
        realm_info = keycloak.get_realm_info(tenant_id)
        if realm_info:
            keycloak_realm_enabled = realm_info.get("enabled", True)

    return _config_to_response(config, keycloak_realm_enabled)


@router.patch(
    "/{tenant_id}",
    response_model=TenantResponse,
    summary="Update tenant settings",
    description="Update tenant settings. Requires global_admin role.",
)
async def update_tenant(
    tenant_id: str,
    request: UpdateTenantRequest,
    user: UserContext = Depends(RequirePermission(Permission.TENANT_UPDATE)),
    repository: TenantConfigRepository = Depends(get_repository),
    session: AsyncSession = Depends(get_agent_session),
) -> TenantResponse:
    """Update tenant settings."""
    config = await repository.get_config(tenant_id)

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Tenant '{tenant_id}' not found"
        )

    # Update config with new values
    config = await repository.update_tenant(
        tenant_id=tenant_id,
        display_name=request.display_name,
        subscription_tier=request.subscription_tier,
        is_active=request.is_active,
        email_domains=request.email_domains,
        max_connectors=request.max_connectors,
        max_knowledge_chunks=request.max_knowledge_chunks,
        max_workflows_per_day=request.max_workflows_per_day,
        installation_context=request.installation_context,
        model_override=request.model_override,
        temperature_override=request.temperature_override,
        features=request.features,
        updated_by=user.user_id,
    )

    await session.commit()

    logger.info(f"Updated tenant: {tenant_id} by {user.user_id}")

    return _config_to_response(config)


@router.post(
    "/{tenant_id}/disable",
    response_model=TenantResponse,
    summary="Disable a tenant",
    description="Disable a tenant (soft delete). Also disables Keycloak realm. Requires global_admin role.",
)
async def disable_tenant(
    tenant_id: str,
    user: UserContext = Depends(RequirePermission(Permission.TENANT_UPDATE)),
    repository: TenantConfigRepository = Depends(get_repository),
    session: AsyncSession = Depends(get_agent_session),
    keycloak: KeycloakTenantManager | None = Depends(get_keycloak),
) -> TenantResponse:
    """Disable a tenant."""
    config = await repository.get_config(tenant_id)

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Tenant '{tenant_id}' not found"
        )

    # Disable Keycloak realm if available
    keycloak_realm_enabled: bool | None = None
    if keycloak:
        try:
            keycloak.disable_realm(tenant_id)
            keycloak_realm_enabled = False
            logger.info(f"Disabled Keycloak realm for tenant: {tenant_id}")
        except KeycloakError as e:
            logger.warning(f"Could not disable Keycloak realm: {e}")

    # Disable tenant in MEHO
    config = await repository.disable_tenant(tenant_id, disabled_by=user.user_id)
    await session.commit()

    logger.info(f"Disabled tenant: {tenant_id} by {user.user_id}")

    return _config_to_response(config, keycloak_realm_enabled)


@router.post(
    "/{tenant_id}/enable",
    response_model=TenantResponse,
    summary="Enable a tenant",
    description="Re-enable a disabled tenant. Also enables Keycloak realm. Requires global_admin role.",
)
async def enable_tenant(
    tenant_id: str,
    user: UserContext = Depends(RequirePermission(Permission.TENANT_UPDATE)),
    repository: TenantConfigRepository = Depends(get_repository),
    session: AsyncSession = Depends(get_agent_session),
    keycloak: KeycloakTenantManager | None = Depends(get_keycloak),
) -> TenantResponse:
    """Enable a tenant."""
    config = await repository.get_config(tenant_id)

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Tenant '{tenant_id}' not found"
        )

    # Enable Keycloak realm if available
    keycloak_realm_enabled: bool | None = None
    if keycloak:
        try:
            keycloak.enable_realm(tenant_id)
            keycloak_realm_enabled = True
            logger.info(f"Enabled Keycloak realm for tenant: {tenant_id}")
        except KeycloakError as e:
            logger.warning(f"Could not enable Keycloak realm: {e}")

    # Enable tenant in MEHO
    config = await repository.enable_tenant(tenant_id, enabled_by=user.user_id)
    await session.commit()

    logger.info(f"Enabled tenant: {tenant_id} by {user.user_id}")

    return _config_to_response(config, keycloak_realm_enabled)

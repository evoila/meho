# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Authentication routes for MEHO API.

Supports:
- Tenant discovery by email domain (TASK-139 Phase 8)
- Production uses Keycloak for authentication via OIDC

See TASK-139 for Keycloak integration details.
"""

# mypy: disable-error-code="no-untyped-def"
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.api.config import get_api_config
from meho_app.api.database import get_agent_session
from meho_app.modules.agents.tenant_config_repository import TenantConfigRepository

MSG_INVALID_EMAIL_FORMAT = "Invalid email format"

router = APIRouter(tags=["auth"])


# =============================================================================
# Tenant Discovery (TASK-139 Phase 8)
# =============================================================================


class DiscoverTenantRequest(BaseModel):
    """Request to discover tenant by email domain."""

    email: str = Field(..., min_length=5, max_length=255, description="User's email address")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """Validate email format."""
        v = v.strip().lower()
        if "@" not in v:
            raise ValueError(MSG_INVALID_EMAIL_FORMAT)
        parts = v.split("@")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(MSG_INVALID_EMAIL_FORMAT)
        if "." not in parts[1]:
            raise ValueError("Invalid email domain")
        return v


class DiscoverTenantResponse(BaseModel):
    """Response with discovered tenant information."""

    tenant_id: str = Field(description="Tenant identifier")
    realm: str = Field(description="Keycloak realm name (same as tenant_id)")
    display_name: str = Field(description="Human-readable organization name")
    keycloak_url: str = Field(description="Keycloak server URL")


async def get_repository(
    session: Annotated[AsyncSession, Depends(get_agent_session)],
) -> TenantConfigRepository:
    """Get a TenantConfigRepository instance with database session."""
    return TenantConfigRepository(session)


@router.post(
    "/discover-tenant",
    response_model=DiscoverTenantResponse,
    summary="Discover tenant by email domain",
    description="Find the tenant (Keycloak realm) for a user based on their email domain. "
    "This is a PUBLIC endpoint - no authentication required.",
)
async def discover_tenant(
    request: DiscoverTenantRequest,
    repository: Annotated[TenantConfigRepository, Depends(get_repository)],
):
    """
    Discover tenant by email domain.

    TASK-139 Phase 8: Email-based tenant discovery for SSO.

    This endpoint is PUBLIC (no authentication required) and allows
    the frontend to determine which Keycloak realm to redirect to
    based on the user's email domain.

    Example request:
    ```json
    {
        "email": "john.doe@acme.com"
    }
    ```

    Returns:
        Tenant information including Keycloak realm

    Raises:
        400: Invalid email format
        404: No tenant found for email domain
        403: Tenant is disabled
    """
    config = get_api_config()

    # Extract domain from email
    try:
        domain = request.email.split("@")[1].lower()
    except IndexError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=MSG_INVALID_EMAIL_FORMAT
        ) from None

    # Look up tenant by email domain (active tenants only by default)
    tenant = await repository.find_by_email_domain(domain, active_only=False)

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No organization found for this email domain. Contact your administrator.",
        )

    if not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This organization is currently disabled. Contact your administrator.",
        )

    return DiscoverTenantResponse(
        tenant_id=tenant.tenant_id,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        realm=tenant.tenant_id,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        display_name=tenant.display_name or tenant.tenant_id,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        keycloak_url=config.keycloak_url,
    )

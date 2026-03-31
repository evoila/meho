# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Credential management operations.

Handles user credential storage and retrieval for connectors,
plus admin-only service credential management (Phase 74).
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from meho_app.api.auth import get_current_user
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission
from meho_app.modules.connectors.credential_resolver import CredentialResolver

logger = get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic schemas for service credential endpoints (Phase 74)
# ---------------------------------------------------------------------------


class ServiceCredentialRequest(BaseModel):
    """Request body for setting/updating a service credential."""

    credential_type: str = Field(..., description="BASIC, API_KEY, OAUTH2_TOKEN, etc.")
    credentials: dict[str, str] = Field(..., description="Credential key-value pairs")


class ServiceCredentialStatusResponse(BaseModel):
    """Response for service credential status check."""

    has_service_credential: bool
    credential_type: str | None = None
    updated_at: str | None = None


@router.post("/{connector_id}/credentials")
async def set_user_credentials(
    connector_id: str, credentials: dict, user: UserContext = Depends(get_current_user)
):
    """
    Set user-specific credentials for a connector.

    For connectors with USER_PROVIDED credential strategy,
    each user provides their own credentials (e.g., vSphere, K8s).

    This ensures audit trails show actual users, not "MEHO system".
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )
    from meho_app.modules.connectors.schemas import UserCredentialProvide

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id)

        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")

        if connector.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")

        cred_repo = UserCredentialRepository(session)

        # Phase 74: Guard against overwriting service credentials via user endpoint
        if user.user_id == CredentialResolver.SENTINEL_SERVICE_USER:
            raise HTTPException(
                status_code=403,
                detail="Cannot store credentials with reserved service user ID",
            )

        # Determine credential type based on auth_type
        if connector.auth_type == "BASIC":
            credential_type = "PASSWORD"
        elif connector.auth_type == "SESSION":
            credential_type = "SESSION"
        else:
            credential_type = "API_KEY"

        credential = UserCredentialProvide(
            connector_id=connector_id, credential_type=credential_type, credentials=credentials
        )

        await cred_repo.store_credentials(user_id=user.user_id, credential=credential)

        await session.commit()

        return {"status": "success", "message": "Credentials saved"}


@router.get("/{connector_id}/credentials/status")
async def get_credential_status(connector_id: str, user: UserContext = Depends(get_current_user)):
    """
    Check if user has credentials configured for a connector.

    Returns credential status WITHOUT exposing actual credentials.
    Security: Never returns actual passwords or keys.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id)

        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")

        if connector.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")

        cred_repo = UserCredentialRepository(session)
        record = await cred_repo._get_credential_record(user.user_id, connector_id)

        if not record or not record.is_active:
            return {
                "has_credentials": False,
                "credential_type": None,
                "last_used_at": None,
                "credential_health": None,
                "credential_health_message": None,
                "credential_health_checked_at": None,
            }

        return {
            "has_credentials": True,
            "credential_type": record.credential_type,
            "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None,
            "credential_health": record.credential_health,
            "credential_health_message": record.credential_health_message,
            "credential_health_checked_at": record.credential_health_checked_at.isoformat() if record.credential_health_checked_at else None,
        }


@router.delete("/{connector_id}/credentials")
async def delete_user_credentials(connector_id: str, user: UserContext = Depends(get_current_user)):
    """
    Delete user's credentials for a connector.

    Security: Users can only delete their own credentials.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id)

        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")

        if connector.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")

        cred_repo = UserCredentialRepository(session)
        deleted = await cred_repo.delete_credentials(user.user_id, connector_id)

        if not deleted:
            raise HTTPException(status_code=404, detail="No credentials found")

        await session.commit()

        return {"status": "success", "message": "Credentials deleted"}


# ---------------------------------------------------------------------------
# Service credential endpoints (Phase 74 -- admin only)
# ---------------------------------------------------------------------------


async def _verify_connector_tenant(session, connector_id: str, tenant_id: str):
    """Verify connector exists and belongs to tenant. Returns connector or raises 404."""
    import uuid

    from sqlalchemy import select

    from meho_app.modules.connectors.models import ConnectorModel

    query = select(ConnectorModel).where(
        ConnectorModel.id == uuid.UUID(connector_id),
        ConnectorModel.tenant_id == tenant_id,
    )
    result = await session.execute(query)
    connector = result.scalar_one_or_none()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    return connector


@router.get(
    "/{connector_id}/service-credential",
    response_model=ServiceCredentialStatusResponse,
)
async def get_service_credential_status(
    connector_id: str,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_UPDATE)),
):
    """
    Check if a service credential is configured for a connector.

    Returns metadata only -- never actual credential values.
    Requires CONNECTOR_UPDATE (admin) permission.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories.credential_repository import (
        CredentialRepository,
    )

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        await _verify_connector_tenant(session, connector_id, user.tenant_id)

        cred_repo = CredentialRepository(session)
        record = await cred_repo._get_credential_record(
            CredentialResolver.SENTINEL_SERVICE_USER, connector_id
        )

        if not record or not record.is_active:
            return ServiceCredentialStatusResponse(
                has_service_credential=False,
                credential_type=None,
                updated_at=None,
            )

        return ServiceCredentialStatusResponse(
            has_service_credential=True,
            credential_type=record.credential_type,
            updated_at=record.updated_at.isoformat() if record.updated_at else None,
        )


@router.put("/{connector_id}/service-credential", status_code=200)
async def set_service_credential(
    connector_id: str,
    request: ServiceCredentialRequest,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_UPDATE)),
):
    """
    Create or update the service credential for a connector.

    Service credentials use the sentinel user_id '__service__' and are
    stored in the same encrypted credential table as user credentials.
    Requires CONNECTOR_UPDATE (admin) permission.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.audit.service import AuditService
    from meho_app.modules.connectors.repositories.credential_repository import (
        CredentialRepository,
    )
    from meho_app.modules.connectors.schemas import UserCredentialProvide

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        await _verify_connector_tenant(session, connector_id, user.tenant_id)

        cred_repo = CredentialRepository(session)
        await cred_repo.store_credentials(
            user_id=CredentialResolver.SENTINEL_SERVICE_USER,
            credential=UserCredentialProvide(
                connector_id=connector_id,
                credential_type=request.credential_type,
                credentials=request.credentials,
            ),
        )

        # Audit log
        audit = AuditService(session)
        await audit.log_event(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            event_type="connector.service_credential_set",
            action="create",
            resource_type="connector",
            resource_id=connector_id,
            details={"credential_type": request.credential_type},
            result="success",
        )

        await session.commit()

    return {"status": "ok", "message": "Service credential saved"}


@router.delete("/{connector_id}/service-credential")
async def delete_service_credential(
    connector_id: str,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_UPDATE)),
):
    """
    Remove the service credential for a connector.

    After removal, automated sessions fall back to creator delegation.
    Requires CONNECTOR_UPDATE (admin) permission.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.audit.service import AuditService
    from meho_app.modules.connectors.repositories.credential_repository import (
        CredentialRepository,
    )

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        await _verify_connector_tenant(session, connector_id, user.tenant_id)

        cred_repo = CredentialRepository(session)
        deleted = await cred_repo.delete_credentials(
            CredentialResolver.SENTINEL_SERVICE_USER, connector_id
        )

        if not deleted:
            raise HTTPException(
                status_code=404, detail="No service credential found for this connector"
            )

        # Audit log
        audit = AuditService(session)
        await audit.log_event(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            event_type="connector.service_credential_removed",
            action="delete",
            resource_type="connector",
            resource_id=connector_id,
            details={},
            result="success",
        )

        await session.commit()

    return {"status": "ok", "message": "Service credential removed"}

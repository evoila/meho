# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Audit BFF API routes.

Provides endpoints for querying audit events:
- GET /api/audit/events   -- Admin view (filtered by tenant)
- GET /api/audit/my-activity -- User's own activity log
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.api.auth import get_current_user
from meho_app.api.database import get_agent_session
from meho_app.core.auth_context import UserContext
from meho_app.core.permissions import Permission, RequirePermission
from meho_app.modules.audit.service import AuditService

router = APIRouter()


def _require_tenant_id(user: UserContext) -> str:
    """Extract tenant_id with runtime validation, narrowing str | None to str."""
    if user.tenant_id is None:
        raise HTTPException(status_code=403, detail="Tenant context required")
    return user.tenant_id


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class AuditEventResponse(BaseModel):
    """Serialised audit event for the frontend."""

    id: str
    tenant_id: str
    user_id: str
    user_email: str | None = None
    event_type: str
    action: str
    resource_type: str
    resource_id: str | None = None
    resource_name: str | None = None
    details: dict[str, Any] | None = None
    result: str
    ip_address: str | None = None
    user_agent: str | None = None
    created_at: datetime


class AuditEventsListResponse(BaseModel):
    """Paginated list of audit events."""

    events: list[AuditEventResponse]
    total: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_to_response(event: Any) -> AuditEventResponse:
    """Convert an AuditEvent ORM instance to a Pydantic response."""
    return AuditEventResponse(
        id=str(event.id),
        tenant_id=event.tenant_id,
        user_id=event.user_id,
        user_email=event.user_email,
        event_type=event.event_type,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        resource_name=event.resource_name,
        details=event.details,
        result=event.result,
        ip_address=event.ip_address,
        user_agent=event.user_agent,
        created_at=event.created_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/events",
    summary="List audit events (admin)",
    description="Query audit events for the current tenant. Requires admin role.",
)
async def list_audit_events(
    user: Annotated[UserContext, Depends(RequirePermission(Permission.ADMIN_CONFIG))],
    session: Annotated[AsyncSession, Depends(get_agent_session)],
    event_type: Annotated[str | None, Query(description="Filter by event type")] = None,
    resource_type: Annotated[str | None, Query(description="Filter by resource type")] = None,
    user_id: Annotated[str | None, Query(description="Filter by user ID")] = None,
    offset: Annotated[int, Query(ge=0, description="Pagination offset")] = 0,
    limit: Annotated[int, Query(ge=1, le=200, description="Page size")] = 50,
) -> AuditEventsListResponse:
    """Return paginated audit events for the caller's tenant."""
    tenant_id = _require_tenant_id(user)
    svc = AuditService(session)
    events, total = await svc.query_events(
        tenant_id=tenant_id,
        event_type=event_type,
        resource_type=resource_type,
        user_id=user_id,
        offset=offset,
        limit=limit,
    )
    return AuditEventsListResponse(
        events=[_event_to_response(e) for e in events],
        total=total,
    )


@router.get(
    "/my-activity",
    summary="My activity log",
    description="Get the current user's own activity log.",
)
async def my_activity(
    user: Annotated[UserContext, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_agent_session)],
    offset: Annotated[int, Query(ge=0, description="Pagination offset")] = 0,
    limit: Annotated[int, Query(ge=1, le=200, description="Page size")] = 50,
) -> AuditEventsListResponse:
    """Return paginated activity for the authenticated user."""
    tenant_id = _require_tenant_id(user)
    svc = AuditService(session)
    events, total = await svc.get_user_activity(
        tenant_id=tenant_id,
        user_id=user.user_id,
        offset=offset,
        limit=limit,
    )
    return AuditEventsListResponse(
        events=[_event_to_response(e) for e in events],
        total=total,
    )

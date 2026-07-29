# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/checks/dashboards*`` -- REST surface for the Dashboard admin CRUD.

Task #2506 under Initiative #2416 (parent goal #221). Fronts
:class:`~meho_backplane.checks.dashboard_service.CheckDashboardAdminService`
so an operator can compose Sensors (#2503) into a Dashboard and read one
five-state answer to "is everything OK?". Lives in its own module rather than
alongside :mod:`meho_backplane.api.v1.checks` (the #2415 gateway
assignment / result-ingest surface, which owns ``/api/v1/checks/assignment``
+ ``/api/v1/checks/results``); the ``dashboards`` sub-path does not collide
with those, and keeping the two routers separate keeps the runner route cage
away from this operator-authenticated surface.

Route inventory
---------------

* ``POST /api/v1/checks/dashboards`` -- create a Dashboard. Role:
  ``tenant_admin``. A foreign / absent sensor id -> 422 ``sensor_not_found``;
  a duplicate name -> 409 ``dashboard_name_conflict``. Returns the rolled-up
  detail with HTTP 201.
* ``GET /api/v1/checks/dashboards`` -- list Dashboards for the operator's
  tenant, newest-first; each row carries its rolled-up ``state`` +
  ``member_count``. Role: ``operator``.
* ``GET /api/v1/checks/dashboards/{dashboard_id}`` -- one Dashboard's rollup
  plus the per-member breakdown (raw / effective state, pending, severity,
  ``for_seconds``, last value / evidence). Role: ``operator``.
* ``DELETE /api/v1/checks/dashboards/{dashboard_id}`` -- hard-delete a
  Dashboard (memberships go with it). Returns 204. Role: ``tenant_admin``.

Tenant scoping + cross-tenant admin
-----------------------------------

Callers are scoped to their JWT's ``tenant_id`` claim; passing a *different*
``tenant_id`` in the create body or ``tenant_filter`` in the query is the
cross-tenant case, authorized through the shared
:func:`~meho_backplane.auth.rbac.authorize_tenant_scope` seam (the #1638
platform-admin primitive) -- a cross-tenant write requires ``platform_admin``,
never merely ``tenant_admin`` (the #2503 IDOR lesson). A cross-tenant probe
by id surfaces as 404 ``dashboard_not_found`` (never 403) so the existence of
a Dashboard is not leaked across the tenant boundary.

Audit contract
--------------

Every route binds ``audit_op_id`` + ``audit_op_class`` before the service
call so the chassis audit middleware classifies the row (``read`` for list /
get, ``write`` for create / delete); ``audit_tenant_scope`` records ``self``
vs ``other``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi import status as http_status
from fastapi.responses import Response

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import authorize_tenant_scope, require_role
from meho_backplane.checks.dashboard_schemas import (
    DashboardCreate,
    DashboardDetail,
    DashboardListResponse,
)
from meho_backplane.checks.dashboard_service import (
    CheckDashboardAdminService,
    DashboardNameConflictError,
    SensorNotFoundError,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/checks/dashboards", tags=["checks-dashboards"])

#: Module-level Depends closures -- required to satisfy ruff B008 (calls in
#: default argument positions are disallowed). Same shape as
#: :mod:`meho_backplane.api.v1.sensors`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

#: Canonical operation identifiers bound into ``audit_op_id`` per route.
_DASHBOARD_OP_IDS: Final[dict[str, str]] = {
    "list": "dashboard.list",
    "create": "dashboard.create",
    "get": "dashboard.get",
    "delete": "dashboard.delete",
}


def _bind_tenant_scope_contextvar(
    *,
    operator_tenant_id: UUID,
    target_tenant_id: UUID,
) -> None:
    """Bind ``audit_tenant_scope=self|other`` for the active request."""
    structlog.contextvars.bind_contextvars(
        audit_tenant_scope=("other" if target_tenant_id != operator_tenant_id else "self"),
    )


@router.get("", response_model=DashboardListResponse)
async def list_dashboards(
    operator: Annotated[Operator, _require_operator],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    tenant_filter: UUID | None = Query(default=None),
) -> DashboardListResponse:
    """List Dashboards for the operator's tenant, newest-first.

    Each row carries its five-state rolled-up ``state`` (evaluated on read)
    and ``member_count``. A ``tenant_filter`` naming a *different* tenant is
    403 ``cross_tenant_requires_platform_admin`` unless the caller holds
    ``platform_admin``.
    """
    target_tenant = authorize_tenant_scope(operator, tenant_filter)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_DASHBOARD_OP_IDS["list"],
        audit_op_class="read",
    )
    _bind_tenant_scope_contextvar(
        operator_tenant_id=operator.tenant_id,
        target_tenant_id=target_tenant,
    )
    service = CheckDashboardAdminService()
    dashboards = await service.list_(target_tenant, limit=limit, offset=offset)
    structlog.contextvars.bind_contextvars(audit_row_count=len(dashboards))
    return DashboardListResponse(dashboards=list(dashboards))


@router.post(
    "",
    response_model=DashboardDetail,
    status_code=http_status.HTTP_201_CREATED,
)
async def create_dashboard(
    body: DashboardCreate,
    operator: Annotated[Operator, _require_admin],
) -> DashboardDetail:
    """Create one Dashboard under the operator's tenant.

    ``tenant_admin`` only. ``body.tenant_id`` is optional: when set to a
    *different* tenant the create is cross-tenant and requires
    ``platform_admin`` (via ``authorize_tenant_scope``). A foreign / absent
    sensor id is 422 ``sensor_not_found``; a duplicate name is 409.
    """
    target_tenant = authorize_tenant_scope(operator, body.tenant_id)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_DASHBOARD_OP_IDS["create"],
        audit_op_class="write",
        audit_dashboard_member_count=len(set(body.sensor_ids)),
    )
    _bind_tenant_scope_contextvar(
        operator_tenant_id=operator.tenant_id,
        target_tenant_id=target_tenant,
    )
    service = CheckDashboardAdminService()
    try:
        detail = await service.create(
            tenant_id=target_tenant,
            created_by_sub=operator.sub,
            payload=body,
        )
    except SensorNotFoundError as exc:
        # 422 -- a referenced sensor id is not visible to this tenant.
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.error_code,
        ) from exc
    except DashboardNameConflictError as exc:
        # 409 -- the (tenant_id, name) pair is already taken.
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=exc.error_code,
        ) from exc
    structlog.contextvars.bind_contextvars(audit_dashboard_id=str(detail.id))
    return detail


@router.get("/{dashboard_id}", response_model=DashboardDetail)
async def get_dashboard(
    dashboard_id: Annotated[uuid.UUID, Path()],
    operator: Annotated[Operator, _require_operator],
    tenant_filter: UUID | None = Query(default=None),
) -> DashboardDetail:
    """Return one Dashboard's rollup + per-member breakdown by id.

    A cross-tenant / absent id returns 404 ``dashboard_not_found`` -- never
    403 -- so the existence of a Dashboard is not leaked across the tenant
    boundary.
    """
    target_tenant = authorize_tenant_scope(operator, tenant_filter)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_DASHBOARD_OP_IDS["get"],
        audit_op_class="read",
        audit_dashboard_id=str(dashboard_id),
    )
    _bind_tenant_scope_contextvar(
        operator_tenant_id=operator.tenant_id,
        target_tenant_id=target_tenant,
    )
    service = CheckDashboardAdminService()
    detail = await service.get(target_tenant, dashboard_id)
    if detail is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="dashboard_not_found",
        )
    return detail


@router.delete(
    "/{dashboard_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_dashboard(
    dashboard_id: Annotated[uuid.UUID, Path()],
    operator: Annotated[Operator, _require_admin],
    tenant_filter: UUID | None = Query(default=None),
) -> Response:
    """Hard-delete one Dashboard by id (memberships go with it).

    ``tenant_admin`` only. A cross-tenant / absent id returns 404
    ``dashboard_not_found`` -- never 403 -- so existence is not leaked.
    """
    target_tenant = authorize_tenant_scope(operator, tenant_filter)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_DASHBOARD_OP_IDS["delete"],
        audit_op_class="write",
        audit_dashboard_id=str(dashboard_id),
    )
    _bind_tenant_scope_contextvar(
        operator_tenant_id=operator.tenant_id,
        target_tenant_id=target_tenant,
    )
    service = CheckDashboardAdminService()
    deleted = await service.delete(target_tenant, dashboard_id)
    if not deleted:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="dashboard_not_found",
        )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)

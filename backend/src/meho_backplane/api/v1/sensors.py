# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/sensors*`` -- REST surface for the Sensor admin CRUD.

Task #2503 under Initiative #2416 (parent goal #221). Three routes that
expose :class:`~meho_backplane.checks.service.SensorAdminService` to
operators. The MCP verbs (:mod:`meho_backplane.mcp.tools.sensors`) and the
Go CLI verbs (``cli/internal/cmd/sensor``) call into the same service from
their own transports; this module is the HTTP front of the check-layer
registration substrate.

Route inventory
---------------

* ``GET /api/v1/sensors`` -- paginated list of sensors for the operator's
  tenant, newest-first. Query params: ``limit``, ``offset``, ``status``,
  ``cadence_kind``, ``tenant_filter`` (platform_admin only). Role:
  ``operator``. The list response carries the latest-result projection, so
  it is also the status view (there is no REST GET-by-id -- the mould
  exposes none).
* ``POST /api/v1/sensors`` -- create a sensor. Body:
  :class:`~meho_backplane.checks.schemas.SensorCreate`. Returns the row
  with HTTP 201. Role: ``tenant_admin``. The safe-only guard refuses a
  non-safe / unknown op with 422; there is no update / pause / resume path
  (status transitions only via #2505's parking).
* ``DELETE /api/v1/sensors/{sensor_id}`` -- hard-delete a sensor. Returns
  204. Role: ``tenant_admin``.

Tenant scoping + cross-tenant admin
-----------------------------------

Callers are scoped to their JWT's ``tenant_id`` claim; passing a
*different* ``tenant_id`` in the create body or ``tenant_filter`` in the
query surfaces as 403 ``cross_tenant_requires_platform_admin`` unless the
caller holds the ``platform_admin`` cross-tenant capability. A
cross-tenant probe by id surfaces as 404 ``sensor_not_found`` (never 403)
so the existence of a sensor is not leaked across the tenant boundary.

Audit + broadcast contract
--------------------------

Every route binds ``audit_op_id`` + ``audit_op_class`` before the service
call so the chassis audit middleware and the publish-on-write broadcast
hook classify the row correctly. ``read`` for list, ``write`` for create /
delete. The ``audit_tenant_scope`` contextvar records ``self`` vs
``other``.
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
from meho_backplane.checks.schemas import (
    SensorCadenceFilter,
    SensorCreate,
    SensorListResponse,
    SensorRead,
    SensorStatusFilter,
)
from meho_backplane.checks.service import (
    SensorAdminService,
    SensorNameConflictError,
    SensorOperationNotFoundError,
    SensorRequiresSafeOperationError,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/sensors", tags=["sensors"])

#: Module-level Depends closures -- required to satisfy ruff B008 (calls in
#: default argument positions are disallowed). Same shape as
#: :mod:`meho_backplane.api.v1.scheduler`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

#: Canonical operation identifiers bound into ``audit_op_id`` per route.
_SENSOR_OP_IDS: Final[dict[str, str]] = {
    "list": "sensor.list",
    "create": "sensor.create",
    "delete": "sensor.delete",
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


@router.get("", response_model=SensorListResponse)
async def list_sensors(
    operator: Annotated[Operator, _require_operator],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: SensorStatusFilter | None = Query(default=None),
    cadence_kind: SensorCadenceFilter | None = Query(default=None),
    tenant_filter: UUID | None = Query(default=None),
) -> SensorListResponse:
    """List sensors for the operator's tenant, newest-first.

    Tenant scoping: callers are scoped to ``operator.tenant_id``; a
    ``tenant_filter`` naming a *different* tenant returns 403
    ``cross_tenant_requires_platform_admin`` unless the caller holds the
    ``platform_admin`` cross-tenant capability. The list response carries
    the latest-result projection (``last_state`` / ``last_evaluated_at`` /
    ...), which is the status view for each sensor.
    """
    target_tenant = authorize_tenant_scope(operator, tenant_filter)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_SENSOR_OP_IDS["list"],
        audit_op_class="read",
    )
    _bind_tenant_scope_contextvar(
        operator_tenant_id=operator.tenant_id,
        target_tenant_id=target_tenant,
    )
    service = SensorAdminService()
    sensors = await service.list_(
        target_tenant,
        status=status,
        cadence_kind=cadence_kind,
        limit=limit,
        offset=offset,
    )
    structlog.contextvars.bind_contextvars(audit_row_count=len(sensors))
    return SensorListResponse(sensors=list(sensors))


@router.post(
    "",
    response_model=SensorRead,
    status_code=http_status.HTTP_201_CREATED,
)
async def create_sensor(
    body: SensorCreate,
    operator: Annotated[Operator, _require_admin],
) -> SensorRead:
    """Create one sensor under the operator's tenant.

    ``tenant_admin`` only. ``body.tenant_id`` is optional: when set, the
    sensor is created under that tenant (cross-tenant admin); when null,
    under ``operator.tenant_id``. The safe-only guard refuses a non-safe or
    unknown op with 422; a duplicate name returns 409.
    """
    target_tenant = authorize_tenant_scope(operator, body.tenant_id)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_SENSOR_OP_IDS["create"],
        audit_op_class="write",
        audit_sensor_cadence_kind=body.cadence_kind.value,
    )
    _bind_tenant_scope_contextvar(
        operator_tenant_id=operator.tenant_id,
        target_tenant_id=target_tenant,
    )
    service = SensorAdminService()
    try:
        entry = await service.create(
            tenant_id=target_tenant,
            created_by_sub=operator.sub,
            payload=body,
        )
    except SensorOperationNotFoundError as exc:
        # 422 -- (connector_id, op_id) resolves to no enabled descriptor.
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.error_code,
        ) from exc
    except SensorRequiresSafeOperationError as exc:
        # 422 -- the op is not safety_level='safe'; a Sensor evaluates
        # unattended, so only safe ops may be bound to it.
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.error_code,
        ) from exc
    except SensorNameConflictError as exc:
        # 409 -- the (tenant_id, name) pair is already taken.
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=exc.error_code,
        ) from exc
    structlog.contextvars.bind_contextvars(audit_sensor_id=str(entry.id))
    return entry


@router.delete(
    "/{sensor_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_sensor(
    sensor_id: Annotated[uuid.UUID, Path()],
    operator: Annotated[Operator, _require_admin],
    tenant_filter: UUID | None = Query(default=None),
) -> Response:
    """Hard-delete one sensor by id.

    ``tenant_admin`` only. A cross-tenant / absent id returns 404
    ``sensor_not_found`` -- never 403 -- so the existence of a sensor is
    not leaked across the tenant boundary. The delete is a hard ``DELETE``:
    no tombstone row is retained.
    """
    target_tenant = authorize_tenant_scope(operator, tenant_filter)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_SENSOR_OP_IDS["delete"],
        audit_op_class="write",
        audit_sensor_id=str(sensor_id),
    )
    _bind_tenant_scope_contextvar(
        operator_tenant_id=operator.tenant_id,
        target_tenant_id=target_tenant,
    )
    service = SensorAdminService()
    deleted = await service.delete(target_tenant, sensor_id)
    if not deleted:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="sensor_not_found",
        )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/agents/grants*`` — REST surface for agent permission grants.

G11.2-T6 (#819) under Initiative #803 (the P3 agent identity + RBAC +
approval gate). Five routes that expose
:class:`~meho_backplane.agents.grants.AgentGrantService` to operators
and agents.

Route inventory
---------------

* ``GET /api/v1/agents/grants`` — list grants in the operator's tenant.
  Query params: ``principal_sub``, ``include_expired``, ``limit``,
  ``offset``. Returns :class:`~meho_backplane.agents.grant_schemas.AgentGrantListResponse`.
  Role: ``tenant_admin``.
* ``GET /api/v1/agents/grants/{grant_id}`` — fetch one grant by id.
  Returns :class:`~meho_backplane.agents.grant_schemas.AgentGrantRead`;
  404 when absent. Role: ``tenant_admin``.
* ``POST /api/v1/agents/grants`` — create a grant (permanent or
  time-bounded). Body: :class:`~meho_backplane.agents.grant_schemas.AgentGrantCreate`.
  Returns the row with HTTP 201. Role: ``tenant_admin``.
* ``POST /api/v1/agents/grants/elevate`` — shorthand for creating a
  time-bounded elevation (``expires_at`` required). Body:
  :class:`~meho_backplane.agents.grant_schemas.AgentElevationCreate`.
  Returns the row with HTTP 201. Role: ``tenant_admin``.
* ``DELETE /api/v1/agents/grants/{grant_id}`` — revoke a grant. Returns
  204; 404 when absent / cross-tenant. Role: ``tenant_admin``.

Why ``tenant_admin``-only for reads
-------------------------------------

Grant listings reveal which agent principals have which permissions —
sensitive information under the least-privilege model. Unlike agent
definitions (where ``operator`` can read), grants are governance data
that only tenant admins need to inspect. The MCP surface follows the
same gate.

Tenant scoping
--------------

Every route derives ``tenant_id`` from the JWT-validated
:class:`~meho_backplane.auth.operator.Operator`; no surface accepts a
tenant id from the body or query string.

Audit + broadcast
-----------------

Every route binds ``audit_op_id`` + ``audit_op_class`` before the
service call. All writes bind ``audit_agent_name`` (the principal_sub)
so the audit payload carries which principal was affected.
"""

from __future__ import annotations

from typing import Annotated, Final
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi import status as http_status
from fastapi.responses import Response

from meho_backplane.agents.grant_schemas import (
    AgentElevationCreate,
    AgentGrantCreate,
    AgentGrantListResponse,
    AgentGrantRead,
)
from meho_backplane.agents.grants import AgentGrantService, GrantValidationError
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/agents/grants", tags=["agent-grants"])

_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

_GRANT_OP_IDS: Final[dict[str, str]] = {
    "list": "agent.grant.list",
    "show": "agent.grant.show",
    "create": "agent.grant.create",
    "elevate": "agent.grant.elevate",
    "revoke": "agent.grant.revoke",
}


@router.get("", response_model=AgentGrantListResponse)
async def list_grants(
    operator: Operator = _require_admin,
    principal_sub: str | None = Query(default=None),
    include_expired: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> AgentGrantListResponse:
    """List permission grants for the operator's tenant.

    ``principal_sub`` filters to one agent's grants. ``include_expired``
    includes past elevations (useful for audit inspection). Tenant-scoped
    to the operator's JWT — no cross-tenant access.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_GRANT_OP_IDS["list"],
        audit_op_class="read",
    )
    service = AgentGrantService()
    grants = await service.list_(
        operator.tenant_id,
        principal_sub=principal_sub,
        include_expired=include_expired,
        limit=limit,
        offset=offset,
    )
    return AgentGrantListResponse(grants=grants)


@router.get("/{grant_id}", response_model=AgentGrantRead)
async def show_grant(
    grant_id: Annotated[UUID, Path()],
    operator: Operator = _require_admin,
) -> AgentGrantRead:
    """Return one grant by id.

    Cross-tenant probes return 404 ``grant_not_found`` (never 403).
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_GRANT_OP_IDS["show"],
        audit_op_class="read",
    )
    service = AgentGrantService()
    entry = await service.get(operator.tenant_id, grant_id)
    if entry is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="grant_not_found",
        )
    return entry


@router.post("", response_model=AgentGrantRead, status_code=http_status.HTTP_201_CREATED)
async def create_grant(
    payload: AgentGrantCreate,
    operator: Operator = _require_admin,
) -> AgentGrantRead:
    """Create a permission grant (permanent or time-bounded).

    A grant with ``expires_at`` is a time-bounded elevation. The grant
    is audited automatically via :class:`~meho_backplane.audit.AuditMiddleware`.
    Returns HTTP 422 when ``expires_at`` is in the past or
    ``target_scope`` is an invalid UUID.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_GRANT_OP_IDS["create"],
        audit_op_class="write",
        audit_agent_name=payload.principal_sub,
    )
    service = AgentGrantService()
    try:
        entry = await service.grant(operator.tenant_id, operator.sub, payload)
    except GrantValidationError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.message,
        ) from exc
    return entry


@router.post(
    "/elevate",
    response_model=AgentGrantRead,
    status_code=http_status.HTTP_201_CREATED,
)
async def elevate_grant(
    payload: AgentElevationCreate,
    operator: Operator = _require_admin,
) -> AgentGrantRead:
    """Create a time-bounded elevation grant (``expires_at`` required).

    Shorthand route for change windows. The grant-expiry sweeper
    removes the row automatically after ``expires_at``, reverting the
    agent to its baseline permissions.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_GRANT_OP_IDS["elevate"],
        audit_op_class="write",
        audit_agent_name=payload.principal_sub,
    )
    service = AgentGrantService()
    try:
        entry = await service.grant(operator.tenant_id, operator.sub, payload)
    except GrantValidationError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.message,
        ) from exc
    return entry


@router.delete("/{grant_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def revoke_grant(
    grant_id: Annotated[UUID, Path()],
    operator: Operator = _require_admin,
) -> Response:
    """Revoke (delete) a permission grant.

    Returns 204 on success; 404 when the grant is absent or belongs to
    another tenant. The revocation is audited via the audit middleware.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_GRANT_OP_IDS["revoke"],
        audit_op_class="write",
    )
    service = AgentGrantService()
    deleted = await service.revoke(operator.tenant_id, grant_id)
    if not deleted:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="grant_not_found",
        )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)

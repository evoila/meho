# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/service-principals/grants*`` — REST surface for standing grants (#3151).

Operator-only CRUD over
:class:`~meho_backplane.operations.service_grants.ServicePrincipalGrantService`.
Creating a grant IS the operator's upfront approval of an unattended
operation for a service principal (the review flow — ``reason`` is
required); listing and revoking manage the grant history.

Create and revoke need a person. A standing grant is a person's approval
given in advance, so only a human principal may write one: both routes
carry :func:`~meho_backplane.auth.rbac.require_human_principal` next to
the role check, the same way the approval decision routes and the
agent-grant create / elevate routes do. A token of an agent, a service
principal or a runner gets HTTP 403 ``human_principal_required`` before
the route runs. List and show keep the role check alone.

Route inventory
---------------

* ``GET /api/v1/service-principals/grants`` — list grants in the operator's
  tenant. Query: ``principal_sub``, ``include_revoked``, ``limit``,
  ``offset``. Role: ``operator``.
* ``GET /api/v1/service-principals/grants/{grant_id}`` — fetch one grant.
  404 when absent / cross-tenant. Role: ``operator``.
* ``POST /api/v1/service-principals/grants`` — create a grant. Body:
  :class:`~meho_backplane.operations.service_grant_schemas.ServiceGrantCreate`.
  201 on success; 422 on a wildcard, a delete-shaped op, a past expiry, or
  a duplicate active grant. Role: ``operator`` + human principal; a delete
  grant (sandbox mode with ``MEHO_SANDBOX_DELETE_GRANTS_FROM_API`` on)
  needs ``tenant_admin``.
* ``DELETE /api/v1/service-principals/grants/{grant_id}`` — revoke
  (soft-delete). Query: optional ``reason``, stored on the grant row and
  on the audit row. 204 on success; 404 when absent / already revoked /
  cross-tenant. Role: ``operator`` + human principal.

Role: ``operator`` (not ``tenant_admin``) — mirrors the approvals surface,
because a standing grant is the persistent form of the same approve
decision an operator already makes on the approval queue. The one
exception is a delete grant (#5251): like in the web console, only a
tenant admin creates one.

Tenant scoping: every route derives ``tenant_id`` from the JWT-validated
:class:`~meho_backplane.auth.operator.Operator`; no surface accepts a
tenant id from the body or query string. Every route binds ``audit_op_id``
+ ``audit_op_class`` so the write lands on the audit ledger.
"""

from __future__ import annotations

from typing import Annotated, Final
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi import status as http_status
from fastapi.responses import Response

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_human_principal, require_role
from meho_backplane.operations.service_grant_schemas import (
    ServiceGrantCreate,
    ServiceGrantListResponse,
    ServiceGrantRead,
)
from meho_backplane.operations.service_grants import (
    DeleteGrantConsoleOnlyError,
    DeleteGrantNeedsTenantAdminError,
    GrantValidationError,
    ServicePrincipalGrantService,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/service-principals/grants", tags=["service-principal-grants"])

#: Module-level Depends closure — avoids ruff B008 (mutable call in a
#: default-argument position). Same pattern as ``api.v1.approvals``.
_require_operator = Depends(require_role(TenantRole.OPERATOR))

#: People-only gate on create and revoke (#4616). A standing grant is a
#: person's approval given in advance, so a machine ``principal_kind``
#: (agent / service / runner) is refused 403 ``human_principal_required``
#: even when it holds the ``operator`` role — the same gate the approval
#: decision routes and ``api.v1.agent_grants`` carry. List and show keep
#: the plain role check.
_require_human = Depends(require_human_principal)

_GRANT_OP_IDS: Final[dict[str, str]] = {
    "list": "service_grant.list",
    "show": "service_grant.show",
    "create": "service_grant.create",
    "revoke": "service_grant.revoke",
}


@router.get("", response_model=ServiceGrantListResponse)
async def list_grants(
    operator: Operator = _require_operator,
    principal_sub: str | None = Query(default=None),
    include_revoked: bool = Query(default=False),
    include_expired: bool = Query(default=True),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ServiceGrantListResponse:
    """List standing grants for the operator's tenant.

    ``principal_sub`` filters to one service principal's grants;
    ``include_revoked`` surfaces the revoked history. Tenant-scoped to the
    operator's JWT — no cross-tenant access.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_GRANT_OP_IDS["list"],
        audit_op_class="read",
    )
    service = ServicePrincipalGrantService()
    grants = await service.list_(
        operator.tenant_id,
        principal_sub=principal_sub,
        include_revoked=include_revoked,
        include_expired=include_expired,
        limit=limit,
        offset=offset,
    )
    return ServiceGrantListResponse(grants=grants)


@router.get("/{grant_id}", response_model=ServiceGrantRead)
async def show_grant(
    grant_id: Annotated[UUID, Path()],
    operator: Operator = _require_operator,
) -> ServiceGrantRead:
    """Return one grant by id. Cross-tenant probes return 404 (never 403)."""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_GRANT_OP_IDS["show"],
        audit_op_class="read",
    )
    service = ServicePrincipalGrantService()
    entry = await service.get(operator.tenant_id, grant_id)
    if entry is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="grant_not_found",
        )
    return entry


@router.post(
    "",
    response_model=ServiceGrantRead,
    status_code=http_status.HTTP_201_CREATED,
    dependencies=[_require_human],
)
async def create_grant(
    payload: ServiceGrantCreate,
    operator: Operator = _require_operator,
) -> ServiceGrantRead:
    """Create a standing grant (the operator's upfront review).

    Only a person may create one: a machine principal gets 403
    ``human_principal_required`` before this runs.
    Returns 422 when the op is delete-shaped, a scope field carries a
    wildcard, ``expires_at`` is in the past, or an active grant for the
    same fully-scoped key already exists. On an instance in sandbox mode
    (#4622) a delete-shaped op is a delete grant. The web console creates
    it; this route refuses it with 403 ``delete_grant_console_only`` unless
    ``MEHO_SANDBOX_DELETE_GRANTS_FROM_API`` is on (#5251). With the setting
    on, a tenant admin may create it here, with the same checks as the
    console; an ``operator`` without ``tenant_admin`` gets 403
    ``grants_require_tenant_admin``. Audited via the audit middleware; the
    payload's ``created_via`` is ``api``.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_GRANT_OP_IDS["create"],
        audit_op_class="write",
        audit_agent_name=payload.principal_sub,
        audit_created_via="api",
    )
    service = ServicePrincipalGrantService()
    try:
        entry = await service.create(
            operator.tenant_id,
            operator.sub,
            payload,
            by_tenant_admin=operator.tenant_role == TenantRole.TENANT_ADMIN,
        )
    except DeleteGrantConsoleOnlyError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail=f"delete_grant_console_only: {exc.message}",
        ) from exc
    except DeleteGrantNeedsTenantAdminError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail=f"grants_require_tenant_admin: {exc.message}",
        ) from exc
    except GrantValidationError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.message,
        ) from exc
    return entry


@router.delete(
    "/{grant_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    dependencies=[_require_human],
)
async def revoke_grant(
    grant_id: Annotated[UUID, Path()],
    operator: Operator = _require_operator,
    reason: str | None = Query(
        default=None,
        min_length=1,
        max_length=2048,
        description="Optional reason for the revoke. Stored on the grant and the audit row.",
    ),
) -> Response:
    """Revoke (soft-delete) a standing grant.

    Only a person may revoke one: a machine principal gets 403
    ``human_principal_required`` before this runs.
    ``reason`` is optional, so a revoke without one stays valid. When given,
    it is stored as ``revoked_reason`` on the grant row and as ``reason`` in
    the audit row payload, the same way create records its ``reason``.
    It is a query parameter, not a body, because a DELETE body is not well
    supported by HTTP clients and proxies.

    Returns 204 on success; 404 when the grant is absent, already revoked,
    or belongs to another tenant. Audited via the audit middleware.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_GRANT_OP_IDS["revoke"],
        audit_op_class="write",
        audit_reason=reason,
    )
    service = ServicePrincipalGrantService()
    revoked = await service.revoke(operator.tenant_id, grant_id, operator.sub, reason=reason)
    if not revoked:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="grant_not_found",
        )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)

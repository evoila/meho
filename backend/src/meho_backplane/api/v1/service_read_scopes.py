# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/service-principals/read-scopes/{principal_sub}`` — the read scope (#4992).

A read scope is the list of targets a service principal may read. A
service principal without one reads every target of its tenant. Only a
person may set or clear a scope: set and clear carry
:func:`~meho_backplane.auth.rbac.require_human_principal` next to the role
check, like the grant create / revoke routes. A token of an agent, a
service principal or a runner gets HTTP 403 ``human_principal_required``
before the route runs. Show keeps the role check alone.

Route inventory
---------------

* ``PUT /api/v1/service-principals/read-scopes/{principal_sub}`` — set
  (replace) the scope. Body:
  :class:`~meho_backplane.operations.service_read_scope_schemas.ServiceReadScopeSet`.
  200 with the stored scope; 422 on a wildcard in ``principal_sub`` or a
  bad entry. Role: ``operator`` + human principal.
* ``GET /api/v1/service-principals/read-scopes/{principal_sub}`` — show
  the scope. 404 ``read_scope_not_found`` when none is set (or
  cross-tenant). Role: ``operator``.
* ``DELETE /api/v1/service-principals/read-scopes/{principal_sub}`` —
  clear the scope. 204; 404 when none is set. Role: ``operator`` + human
  principal.

Tenant scoping: every route derives ``tenant_id`` from the JWT-validated
:class:`~meho_backplane.auth.operator.Operator`. Every route binds
``audit_op_id`` + ``audit_op_class`` so the audit middleware writes one
row per call; set also records the entries it stored.
"""

from __future__ import annotations

from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi import status as http_status
from fastapi.responses import Response

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_human_principal, require_role
from meho_backplane.operations.service_read_scope_schemas import (
    ServiceReadScopeRead,
    ServiceReadScopeSet,
)
from meho_backplane.operations.service_read_scopes import (
    ReadScopeValidationError,
    ServiceReadScopeService,
    stored_entries,
)

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/service-principals/read-scopes", tags=["service-principal-read-scopes"]
)

_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_human = Depends(require_human_principal)

_READ_SCOPE_OP_IDS: Final[dict[str, str]] = {
    "set": "service_read_scope.set",
    "show": "service_read_scope.show",
    "clear": "service_read_scope.clear",
}

_PrincipalSub = Annotated[
    str,
    Path(min_length=1, max_length=512, description="JWT sub of the service principal."),
]


@router.put("/{principal_sub}", response_model=ServiceReadScopeRead, dependencies=[_require_human])
async def set_read_scope(
    principal_sub: _PrincipalSub,
    payload: ServiceReadScopeSet,
    operator: Operator = _require_operator,
) -> ServiceReadScopeRead:
    """Set (replace) the read scope of one service principal.

    Only a person may set it: a machine principal gets 403
    ``human_principal_required`` before this runs. Returns 422 when
    ``principal_sub`` carries a wildcard. Audited via the audit middleware,
    with the stored entries on the audit row.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_READ_SCOPE_OP_IDS["set"],
        audit_op_class="write",
        audit_agent_name=principal_sub,
        audit_entries=stored_entries(payload),
    )
    service = ServiceReadScopeService()
    try:
        return await service.set(operator.tenant_id, principal_sub, operator.sub, payload)
    except ReadScopeValidationError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.message,
        ) from exc


@router.get("/{principal_sub}", response_model=ServiceReadScopeRead)
async def show_read_scope(
    principal_sub: _PrincipalSub,
    operator: Operator = _require_operator,
) -> ServiceReadScopeRead:
    """Return the read scope of one service principal; 404 when none is set."""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_READ_SCOPE_OP_IDS["show"],
        audit_op_class="read",
    )
    service = ServiceReadScopeService()
    entry = await service.get(operator.tenant_id, principal_sub)
    if entry is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="read_scope_not_found",
        )
    return entry


@router.delete(
    "/{principal_sub}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    dependencies=[_require_human],
)
async def clear_read_scope(
    principal_sub: _PrincipalSub,
    operator: Operator = _require_operator,
) -> Response:
    """Clear the read scope of one service principal; it then reads every target again.

    Only a person may clear it. Returns 204; 404 when no scope is set.
    Audited via the audit middleware.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_READ_SCOPE_OP_IDS["clear"],
        audit_op_class="write",
        audit_agent_name=principal_sub,
    )
    service = ServiceReadScopeService()
    cleared = await service.clear(operator.tenant_id, principal_sub, operator.sub)
    if not cleared:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="read_scope_not_found",
        )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)

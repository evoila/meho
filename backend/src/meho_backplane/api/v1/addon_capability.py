# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/addons/pairings/{name}/capabilities`` — capability advertisement (#3026).

The REST surface over
:class:`~meho_backplane.operations.addon_capability.AddonCapabilityService`,
the capability-advertisement plane of Initiative #2900 built on the #3025
pairing. A paired add-on declares the surfaces it contributes (meta-tool
families, CLI verb families, console panels, event kinds) against its
negotiated integration-contract version; the backplane persists the
declaration and reports which surfaces are *active* (paired and
contract-healthy).

Route inventory
---------------

* ``PUT /api/v1/addons/pairings/{name}/capabilities`` — the paired add-on
  declares its complete surface set (replace-all). Requires a **service**
  principal (403 otherwise, as for heartbeat); 404 when the add-on is not
  paired; 422 on an unknown capability kind or a malformed / duplicate
  declaration. Audited (``op_id=addon.capabilities.declare``).
* ``GET /api/v1/addons/pairings/{name}/capabilities`` — an operator reads the
  add-on's declared surfaces plus their live activation state. 404 when
  absent / cross-tenant. Role: ``operator``.

Tenant scoping mirrors the pairing router: ``tenant_id`` comes from the
JWT-validated :class:`~meho_backplane.auth.operator.Operator`, never the body
or query string.
"""

from __future__ import annotations

from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi import status as http_status

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations.addon_capability import AddonCapabilityService
from meho_backplane.operations.addon_capability_schemas import (
    CapabilityDeclarationResponse,
    DeclareCapabilitiesRequest,
)
from meho_backplane.operations.addon_pairing import AddonNotPairedError

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/addons/pairings", tags=["addon-pairing"])

#: Module-level Depends closures — avoid ruff B008. Declaring is the add-on's
#: own action (it authenticates as its service principal); reading the
#: declaration is operator-visible.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_jwt = Depends(verify_jwt_and_bind)

_OP_IDS: Final[dict[str, str]] = {
    "declare": "addon.capabilities.declare",
    "show": "addon.capabilities.show",
}


@router.put("/{name}/capabilities", response_model=CapabilityDeclarationResponse)
async def declare_capabilities(
    name: Annotated[str, Path()],
    payload: DeclareCapabilitiesRequest,
    operator: Operator = _require_jwt,
) -> CapabilityDeclarationResponse:
    """Declare an add-on's advertised surfaces (paired service principal only).

    The paired add-on authenticates as its own **service** principal and
    replaces its declaration wholesale. A non-service principal is 403; an
    unpaired add-on is 404; an unknown capability kind or a malformed /
    duplicate declaration is 422 (rejected by the request schema before this
    handler runs). Audited via the audit middleware.
    """
    if operator.principal_kind is not PrincipalKind.SERVICE:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="capabilities_declare_requires_service_principal",
        )
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["declare"],
        audit_op_class="write",
        audit_addon_name=name,
    )
    service = AddonCapabilityService()
    try:
        return await service.declare(operator.tenant_id, name, payload)
    except AddonNotPairedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="addon_not_paired",
        ) from exc


@router.get("/{name}/capabilities", response_model=CapabilityDeclarationResponse)
async def show_capabilities(
    name: Annotated[str, Path()],
    operator: Operator = _require_operator,
) -> CapabilityDeclarationResponse:
    """Return one add-on's declared surfaces + activation state (operator-gated).

    Cross-tenant probes and unpaired add-ons return 404.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["show"],
        audit_op_class="read",
    )
    service = AddonCapabilityService()
    declaration = await service.list_declared(operator.tenant_id, name)
    if declaration is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="addon_not_paired",
        )
    return declaration

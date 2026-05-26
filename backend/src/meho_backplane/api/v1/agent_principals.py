# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/agent-principals*`` — REST surface for agent-identity lifecycle.

G11.2-T1 (#815) under Initiative #803 (G11.2 Agent identity + RBAC +
approval). Three routes that expose
:class:`~meho_backplane.auth.agent_principals.AgentPrincipalService`
to operators. The MCP tools
(:mod:`meho_backplane.mcp.tools.agent_principals`) and the Go CLI verbs
(``meho agent-principal``) call into the same service from their own
transports; this module is the HTTP front.

Route inventory
---------------

* ``GET /api/v1/agent-principals`` — list active agent principals for
  the operator's tenant, name-sorted. Query params: ``limit``,
  ``offset``, ``include_revoked``. Returns
  :class:`AgentPrincipalListResponse`. Role: ``operator``.
* ``GET /api/v1/agent-principals/{name}`` — show one principal by
  name. Returns :class:`~meho_backplane.auth.agent_principals.AgentPrincipalRead`;
  404 when absent. Role: ``operator``.
* ``POST /api/v1/agent-principals`` — register a new agent principal
  (creates the Keycloak client + inserts DB row). Returns the row with
  HTTP 201. 409 on duplicate ``(tenant, name)``. Role: ``tenant_admin``.
* ``DELETE /api/v1/agent-principals/{name}/revoke`` — revoke an agent
  (kill switch: disables Keycloak client + marks row revoked). Returns
  the updated row. Role: ``tenant_admin``.

Tenant scoping
--------------

Every route derives ``tenant_id`` from the JWT-validated
:class:`~meho_backplane.auth.operator.Operator`; no surface accepts a
tenant id from the body or query string. Cross-tenant name probes
surface as 404 (never 403).

Audit + broadcast contract
--------------------------

Every route binds ``audit_op_id`` + ``audit_op_class`` before the
service call so the chassis audit middleware classifies the row correctly.
"""

from __future__ import annotations

from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict

from meho_backplane.auth.agent_principals import (
    AgentPrincipalCreate,
    AgentPrincipalExistsError,
    AgentPrincipalNotFoundError,
    AgentPrincipalRead,
    AgentPrincipalService,
)
from meho_backplane.auth.keycloak_admin import (
    KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL,
    KeycloakAdminError,
    KeycloakAdminNotConfiguredError,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/agent-principals", tags=["agent-principals"])

_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

_OP_IDS: Final[dict[str, str]] = {
    "list": "agent_principal.list",
    "show": "agent_principal.show",
    "register": "agent_principal.register",
    "revoke": "agent_principal.revoke",
}

_NAME_MAX_LENGTH: Final[int] = 128


class AgentPrincipalListResponse(BaseModel):
    """Response envelope for ``GET /api/v1/agent-principals``."""

    model_config = ConfigDict(frozen=True)

    principals: list[AgentPrincipalRead]


def _handle_admin_error(exc: Exception) -> HTTPException:
    """Map Keycloak admin errors to HTTP responses.

    Two failure modes get distinct status codes and shapes:

    * :class:`KeycloakAdminNotConfiguredError` -> 503 with the
      gold-standard three-clause detail (domain code + named env
      vars + doc reference), built once as
      :data:`~meho_backplane.auth.keycloak_admin.KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL`.
      Compliant with the convention codified in
      ``docs/codebase/error-message-shape.md`` (G0.14-T11 #1141);
      symmetric with
      :data:`~meho_backplane.ui.auth.flow.MISSING_CLIENT_SECRET_DETAIL`
      on ``/ui/auth/login`` (the consumer-flagged gold-standard).
    * Any other :class:`KeycloakAdminError` -> 502 with the bare
      ``keycloak_admin_error`` code. Intentionally bare per the
      convention's *intentionally-bare* section: the remediation
      depends on the upstream's actual fault and naming a specific
      remediation would speculate. The structured log carries the
      exception class + status code so an operator with cluster
      access can resolve the underlying cause off the request path.
    """
    if isinstance(exc, KeycloakAdminNotConfiguredError):
        return HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL,
        )
    return HTTPException(
        status_code=http_status.HTTP_502_BAD_GATEWAY,
        detail="keycloak_admin_error",
    )


@router.get("", response_model=AgentPrincipalListResponse)
async def list_agent_principals(
    operator: Operator = _require_operator,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    include_revoked: bool = Query(default=False),
) -> AgentPrincipalListResponse:
    """List agent principals for the operator's tenant."""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["list"],
        audit_op_class="read",
    )
    service = AgentPrincipalService()
    principals = await service.list_(
        operator.tenant_id,
        include_revoked=include_revoked,
        limit=limit,
        offset=offset,
    )
    return AgentPrincipalListResponse(principals=principals)


@router.get("/{name}", response_model=AgentPrincipalRead)
async def show_agent_principal(
    name: Annotated[str, Path(max_length=_NAME_MAX_LENGTH)],
    operator: Operator = _require_operator,
) -> AgentPrincipalRead:
    """Return one agent principal by name."""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["show"],
        audit_op_class="read",
    )
    service = AgentPrincipalService()
    entry = await service.get(operator.tenant_id, name)
    if entry is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="agent_principal_not_found",
        )
    return entry


@router.post(
    "",
    response_model=AgentPrincipalRead,
    status_code=http_status.HTTP_201_CREATED,
)
async def register_agent_principal(
    body: AgentPrincipalCreate,
    operator: Operator = _require_admin,
) -> AgentPrincipalRead:
    """Register a new agent principal (creates Keycloak client + DB row).

    ``tenant_admin`` only. Returns 409 on duplicate ``(tenant, name)``.
    Returns 503 when Keycloak admin is not configured.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["register"],
        audit_op_class="write",
        audit_agent_principal_name=body.name,
    )
    service = AgentPrincipalService()
    try:
        return await service.register(
            tenant_id=operator.tenant_id,
            created_by_sub=operator.sub,
            payload=body,
        )
    except AgentPrincipalExistsError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="agent_principal_already_exists",
        ) from exc
    except KeycloakAdminError as exc:
        raise _handle_admin_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.delete("/{name}/revoke", response_model=AgentPrincipalRead)
async def revoke_agent_principal(
    name: Annotated[str, Path(max_length=_NAME_MAX_LENGTH)],
    operator: Operator = _require_admin,
) -> AgentPrincipalRead:
    """Revoke an agent principal (kill switch).

    Disables the Keycloak client (blocks new token grants) and marks
    the DB row ``revoked=true``. Returns the updated row.
    ``tenant_admin`` only. Returns 404 when absent or already revoked.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["revoke"],
        audit_op_class="write",
        audit_agent_principal_name=name,
    )
    service = AgentPrincipalService()
    try:
        return await service.revoke(operator.tenant_id, name)
    except AgentPrincipalNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="agent_principal_not_found",
        ) from exc
    except KeycloakAdminError as exc:
        raise _handle_admin_error(exc) from exc

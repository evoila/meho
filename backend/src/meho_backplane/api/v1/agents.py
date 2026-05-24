# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/agents*`` -- REST surface for agent-definition CRUD.

G11.1-T2 (#809) under Initiative #802 (the P1 agent runtime). Five
routes that expose :class:`~meho_backplane.agents.service.AgentDefinitionService`
to operators and agents. The MCP verbs
(:mod:`meho_backplane.mcp.tools.agents`) and the Go CLI verbs
(``cli/internal/cmd/agent``) call into the same service from their own
transports; this module is the HTTP front of the agent-definition
backplane.

Route inventory
---------------

* ``GET /api/v1/agents`` -- paginated list of definitions for the
  operator's tenant, name-sorted. Query params: ``limit``, ``offset``.
  Returns :class:`AgentDefinitionListResponse`. Role: ``operator``.
* ``GET /api/v1/agents/{name}`` -- fetch one definition by name.
  Returns :class:`~meho_backplane.agents.schemas.AgentDefinitionRead`;
  404 when absent. Role: ``operator``.
* ``POST /api/v1/agents`` -- create a definition. Body:
  :class:`~meho_backplane.agents.schemas.AgentDefinitionCreate`. Returns
  the row with HTTP 201; 409 on a duplicate ``(tenant, name)``. Role:
  ``tenant_admin``.
* ``PATCH /api/v1/agents/{name}`` -- partial update. Body:
  :class:`~meho_backplane.agents.schemas.AgentDefinitionUpdate`. Returns
  the updated row; 404 when absent. Role: ``tenant_admin``.
* ``DELETE /api/v1/agents/{name}`` -- delete a definition. Returns 204;
  404 when absent / cross-tenant. Role: ``tenant_admin``.

Tenant scoping
--------------

Every route derives ``tenant_id`` from the JWT-validated
:class:`~meho_backplane.auth.operator.Operator`; no surface accepts a
tenant id from the body or query string. A cross-tenant name probe
surfaces as 404 (never 403) -- the conflation prevents enumerating
another tenant's agents via a status-code differential.

Audit + broadcast contract
--------------------------

Every route binds ``audit_op_id`` + ``audit_op_class`` before the
service call so the chassis :class:`~meho_backplane.audit.AuditMiddleware`
and the publish-on-write broadcast hook classify the row correctly.
``read`` for list / show, ``write`` for create / edit / delete. The
op_class is bound explicitly because
:func:`~meho_backplane.broadcast.events.classify_op` would only match
some of the op ids against its suffix tables. Mutations also bind
``audit_agent_name`` so the audit payload carries which definition was
touched -- the system prompt / toolset content is never bound.
"""

from __future__ import annotations

from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi import status as http_status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from meho_backplane.agents.schemas import (
    AgentDefinitionCreate,
    AgentDefinitionRead,
    AgentDefinitionUpdate,
)
from meho_backplane.agents.service import (
    AgentDefinitionExistsError,
    AgentDefinitionService,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

#: Module-level Depends closures -- required to satisfy ruff B008 (calls
#: in default argument positions are disallowed). Same shape as
#: :mod:`meho_backplane.api.v1.kb`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

#: Canonical operation identifiers bound into ``audit_op_id`` per route.
#: Pinned as module constants so the contract is greppable and a typo
#: surfaces at first call rather than as a silent broadcast under the
#: wrong op id.
_AGENT_OP_IDS: Final[dict[str, str]] = {
    "list": "agent.list",
    "show": "agent.show",
    "create": "agent.create",
    "edit": "agent.edit",
    "delete": "agent.delete",
}

#: Maximum length of the ``name`` path parameter accepted by /{name}
#: routes. Defence-in-depth on top of the name pattern -- a pathological
#: name substring in the URL would never match a row but would still
#: cost a query; capping at the path-parameter parse stage bounds it.
_NAME_MAX_LENGTH: Final[int] = 128


class AgentDefinitionListResponse(BaseModel):
    """Response envelope for ``GET /api/v1/agents``.

    Wrapped in ``{"agents": [...]}`` so a future paging / cursor field
    can land non-breakingly -- the same shape
    :mod:`meho_backplane.api.v1.kb` adopted for its list response.
    """

    model_config = ConfigDict(frozen=True)

    agents: list[AgentDefinitionRead]


@router.get("", response_model=AgentDefinitionListResponse)
async def list_agents(
    operator: Operator = _require_operator,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> AgentDefinitionListResponse:
    """List agent definitions for the operator's tenant, name-sorted.

    Tenant-scoped to ``operator.tenant_id`` -- no surface accepts a
    tenant id from the query string. ``operator`` and ``tenant_admin``
    both pass; ``read_only`` is rejected by :func:`require_role`.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_AGENT_OP_IDS["list"],
        audit_op_class="read",
    )
    service = AgentDefinitionService()
    agents = await service.list_(operator.tenant_id, limit=limit, offset=offset)
    return AgentDefinitionListResponse(agents=agents)


@router.get("/{name}", response_model=AgentDefinitionRead)
async def show_agent(
    name: Annotated[str, Path(max_length=_NAME_MAX_LENGTH)],
    operator: Operator = _require_operator,
) -> AgentDefinitionRead:
    """Return one agent definition by name.

    Cross-tenant name probes surface as 404 ``agent_not_found`` (not
    403) -- the conflation prevents enumerating another tenant's agents
    via a status-code differential.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_AGENT_OP_IDS["show"],
        audit_op_class="read",
    )
    service = AgentDefinitionService()
    entry = await service.get(operator.tenant_id, name)
    if entry is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="agent_not_found",
        )
    return entry


@router.post(
    "",
    response_model=AgentDefinitionRead,
    status_code=http_status.HTTP_201_CREATED,
)
async def create_agent(
    body: AgentDefinitionCreate,
    operator: Operator = _require_admin,
) -> AgentDefinitionRead:
    """Create one agent definition under the operator's tenant.

    ``tenant_admin`` only. A duplicate ``(tenant, name)`` returns 409
    ``agent_already_exists`` (the per-tenant name natural-key contract
    the ``agent_definition_tenant_name_idx`` unique index enforces).
    The system prompt / toolset content is never bound to the audit row;
    only the agent name is.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_AGENT_OP_IDS["create"],
        audit_op_class="write",
        audit_agent_name=body.name,
    )
    service = AgentDefinitionService()
    try:
        return await service.create(
            tenant_id=operator.tenant_id,
            created_by_sub=operator.sub,
            payload=body,
        )
    except AgentDefinitionExistsError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="agent_already_exists",
        ) from exc


@router.patch("/{name}", response_model=AgentDefinitionRead)
async def edit_agent(
    name: Annotated[str, Path(max_length=_NAME_MAX_LENGTH)],
    body: AgentDefinitionUpdate,
    operator: Operator = _require_admin,
) -> AgentDefinitionRead:
    """Apply a partial update to one agent definition by name.

    ``tenant_admin`` only. Only the fields the caller set are applied
    (``exclude_unset``), so a PATCH can change a single field. ``name``
    is not updatable -- renaming is a delete + recreate. A cross-tenant
    / absent name returns 404 ``agent_not_found``.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_AGENT_OP_IDS["edit"],
        audit_op_class="write",
        audit_agent_name=name,
    )
    service = AgentDefinitionService()
    entry = await service.update(operator.tenant_id, name, body)
    if entry is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="agent_not_found",
        )
    return entry


@router.delete(
    "/{name}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_agent(
    name: Annotated[str, Path(max_length=_NAME_MAX_LENGTH)],
    operator: Operator = _require_admin,
) -> Response:
    """Delete one agent definition by name.

    ``tenant_admin`` only. A cross-tenant / absent name returns 404
    ``agent_not_found`` -- never 403, so the existence of a definition
    is not leaked across the tenant boundary.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_AGENT_OP_IDS["delete"],
        audit_op_class="write",
        audit_agent_name=name,
    )
    service = AgentDefinitionService()
    deleted = await service.delete(operator.tenant_id, name)
    if not deleted:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="agent_not_found",
        )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)

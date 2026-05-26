# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/scheduler/triggers*`` -- REST surface for scheduled-trigger admin.

G11.3-T5 (#826) under Initiative #804 (the P2 scheduler). Three routes
that expose :class:`~meho_backplane.scheduler.service.SchedulerAdminService`
to operators. The MCP verbs (:mod:`meho_backplane.mcp.tools.scheduler`)
and the Go CLI verbs (``cli/internal/cmd/scheduler``) call into the
same service from their own transports; this module is the HTTP front
of the scheduler-admin backplane.

Route inventory
---------------

* ``GET /api/v1/scheduler/triggers`` -- paginated list of triggers for
  the operator's tenant, newest-first. Query params: ``limit``,
  ``offset``, ``kind``, ``status``, ``tenant_filter`` (tenant_admin
  only). Role: ``operator``.
* ``POST /api/v1/scheduler/triggers`` -- create a trigger. Body:
  :class:`~meho_backplane.scheduler.schemas.ScheduledTriggerCreate`.
  Returns the row with HTTP 201. Role: ``tenant_admin``.
* ``DELETE /api/v1/scheduler/triggers/{id}`` -- cancel a trigger (the
  transition is terminal; the row is retained for audit). Returns 204.
  Role: ``tenant_admin``.

Tenant scoping + cross-tenant admin
-----------------------------------

``operator`` / ``read_only`` callers are scoped to their JWT's
``tenant_id`` claim and any attempt to pass ``tenant_id`` in the
create body or ``tenant_filter`` in the query string surfaces as 403
``tenant_filter_requires_tenant_admin`` (mirrors the
``retrieve_usage`` precedent). ``tenant_admin`` callers may target
another tenant by setting ``tenant_id`` in the body (create) or
``tenant_filter`` in the query (list). A cross-tenant probe by id
surfaces as 404 ``trigger_not_found`` (never 403) -- the conflation
prevents enumerating another tenant's triggers via a status-code
differential.

Audit + broadcast contract
--------------------------

Every route binds ``audit_op_id`` + ``audit_op_class`` before the
service call so the chassis
:class:`~meho_backplane.audit.AuditMiddleware` and the publish-on-write
broadcast hook classify the row correctly. ``read`` for list,
``write`` for create / cancel. The ``audit_tenant_scope`` contextvar
records ``self`` vs ``other`` so an audit query for cross-tenant
admin activity is trivial.
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
from meho_backplane.auth.rbac import require_role
from meho_backplane.scheduler.schemas import (
    KindFilter,
    ScheduledTriggerCreate,
    ScheduledTriggerListResponse,
    ScheduledTriggerRead,
    StatusFilter,
)
from meho_backplane.scheduler.service import (
    AgentDefinitionMissingError,
    SchedulerAdminService,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/scheduler", tags=["scheduler"])

#: Module-level Depends closures -- required to satisfy ruff B008 (calls
#: in default argument positions are disallowed). Same shape as
#: :mod:`meho_backplane.api.v1.agents`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

#: Canonical operation identifiers bound into ``audit_op_id`` per route.
#: Pinned as module constants so the contract is greppable and a typo
#: surfaces at first call rather than as a silent broadcast under the
#: wrong op id.
_SCHEDULER_OP_IDS: Final[dict[str, str]] = {
    "list": "scheduler.list",
    "create": "scheduler.create",
    "cancel": "scheduler.cancel",
}


def _bind_tenant_scope_contextvar(
    *,
    operator_tenant_id: UUID,
    target_tenant_id: UUID,
) -> None:
    """Bind ``audit_tenant_scope=self|other`` for the active request.

    Matches the :mod:`meho_backplane.api.v1.retrieve_usage` precedent so
    a cross-tenant admin action is greppable in audit (``scope=other``)
    without parsing the actor's vs the row's tenant id from the
    payload.
    """
    structlog.contextvars.bind_contextvars(
        audit_tenant_scope=("other" if target_tenant_id != operator_tenant_id else "self"),
    )


@router.get("/triggers", response_model=ScheduledTriggerListResponse)
async def list_triggers(
    operator: Annotated[Operator, _require_operator],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    kind: KindFilter | None = Query(default=None),
    status: StatusFilter | None = Query(default=None),
    tenant_filter: UUID | None = Query(default=None),
) -> ScheduledTriggerListResponse:
    """List scheduled triggers for the operator's tenant, newest-first.

    Tenant scoping: ``operator`` / ``read_only`` callers are scoped to
    ``operator.tenant_id`` and any non-null ``tenant_filter`` returns
    403. Only ``tenant_admin`` may cross tenants.
    """
    if tenant_filter is not None and operator.tenant_role != TenantRole.TENANT_ADMIN:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="tenant_filter_requires_tenant_admin",
        )
    target_tenant = tenant_filter if tenant_filter is not None else operator.tenant_id
    structlog.contextvars.bind_contextvars(
        audit_op_id=_SCHEDULER_OP_IDS["list"],
        audit_op_class="read",
    )
    _bind_tenant_scope_contextvar(
        operator_tenant_id=operator.tenant_id,
        target_tenant_id=target_tenant,
    )
    service = SchedulerAdminService()
    triggers = await service.list_(
        target_tenant,
        kind=kind,
        status=status,
        limit=limit,
        offset=offset,
    )
    structlog.contextvars.bind_contextvars(audit_row_count=len(triggers))
    return ScheduledTriggerListResponse(triggers=list(triggers))


@router.post(
    "/triggers",
    response_model=ScheduledTriggerRead,
    status_code=http_status.HTTP_201_CREATED,
)
async def create_trigger(
    body: ScheduledTriggerCreate,
    operator: Annotated[Operator, _require_admin],
) -> ScheduledTriggerRead:
    """Create one scheduled trigger under the operator's tenant.

    ``tenant_admin`` only. ``body.tenant_id`` is optional: when set, the
    trigger is created under that tenant (cross-tenant admin); when
    null, the trigger is created under ``operator.tenant_id``. The
    schema's discriminated-union validator already proved exactly one
    of ``cron_expr`` / ``fire_at`` / ``event_filter`` is set; the
    service runs the FK pre-flight.
    """
    target_tenant = body.tenant_id if body.tenant_id is not None else operator.tenant_id
    structlog.contextvars.bind_contextvars(
        audit_op_id=_SCHEDULER_OP_IDS["create"],
        audit_op_class="write",
        audit_trigger_kind=body.kind.value,
    )
    _bind_tenant_scope_contextvar(
        operator_tenant_id=operator.tenant_id,
        target_tenant_id=target_tenant,
    )
    service = SchedulerAdminService()
    try:
        entry = await service.create(
            tenant_id=target_tenant,
            created_by_sub=operator.sub,
            payload=body,
        )
    except AgentDefinitionMissingError as exc:
        # 422 -- the payload is well-formed but its
        # ``agent_definition_id`` does not resolve to a definition in
        # the target tenant.
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="agent_definition_not_found",
        ) from exc
    structlog.contextvars.bind_contextvars(audit_trigger_id=str(entry.id))
    return entry


@router.delete(
    "/triggers/{trigger_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def cancel_trigger(
    trigger_id: Annotated[uuid.UUID, Path()],
    operator: Annotated[Operator, _require_admin],
    tenant_filter: UUID | None = Query(default=None),
) -> Response:
    """Cancel one scheduled trigger by id (transitions ``status='cancelled'``).

    ``tenant_admin`` only. A cross-tenant / absent id returns 404
    ``trigger_not_found`` -- never 403 -- so the existence of a
    trigger is not leaked across the tenant boundary. A trigger
    already in terminal ``fired`` state returns 409
    ``trigger_already_fired`` (the lifecycle is ``fired`` -> end,
    not ``fired`` -> ``cancelled``).

    Cross-tenant: pass ``tenant_filter`` to cancel a trigger under
    another tenant; ``operator`` role is locked to the JWT's tenant.
    The check happens here even though the route is
    ``tenant_admin``-gated, for forward-compat in case the role gate
    relaxes in a future release.
    """
    if tenant_filter is not None and operator.tenant_role != TenantRole.TENANT_ADMIN:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="tenant_filter_requires_tenant_admin",
        )
    target_tenant = tenant_filter if tenant_filter is not None else operator.tenant_id
    structlog.contextvars.bind_contextvars(
        audit_op_id=_SCHEDULER_OP_IDS["cancel"],
        audit_op_class="write",
        audit_trigger_id=str(trigger_id),
    )
    _bind_tenant_scope_contextvar(
        operator_tenant_id=operator.tenant_id,
        target_tenant_id=target_tenant,
    )
    service = SchedulerAdminService()
    # Look up first so we can distinguish 404 (absent / cross-tenant)
    # from 409 (already terminal-fired). The service's cancel()
    # returns False for both, which would otherwise lose the
    # information.
    existing = await service.get(target_tenant, trigger_id)
    if existing is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="trigger_not_found",
        )
    cancelled = await service.cancel(target_tenant, trigger_id)
    if not cancelled:
        # The only way to reach here after the existence check passes
        # is the row being in terminal ``fired`` state (or a TOCTOU
        # window with another caller).
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="trigger_already_fired",
        )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)

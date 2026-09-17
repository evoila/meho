# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unified operator-console surface for agent and service-principal grants.

The existing ``/ui/agents/grants`` surface remains the detailed, all-admin
agent-grant console.  This page is its governance-plane sibling: operators
can inspect service grants, while tenant administrators can additionally
inspect agent grants and create, elevate, or revoke either kind.  The split is
deliberate: agent-grant reads are tenant-admin-only at their backing service.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from meho_backplane.agents.grant_schemas import (
    AgentGrantCreate,
    AgentGrantRead,
    GrantVerdict,
)
from meho_backplane.agents.grants import AgentGrantService
from meho_backplane.agents.grants import GrantValidationError as AgentGrantValidationError
from meho_backplane.auth.operator import Operator, TenantRole, is_human_principal
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.service_grant_schemas import (
    ServiceGrantCreate,
    ServiceGrantRead,
)
from meho_backplane.operations.service_grants import (
    GrantValidationError as ServiceGrantValidationError,
)
from meho_backplane.operations.service_grants import ServicePrincipalGrantService
from meho_backplane.targets.resolver import resolve_target_by_id
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.references import subject_ref
from meho_backplane.ui.routes.agents.operator import _lift_operator
from meho_backplane.ui.templating import get_templates

__all__ = ["build_grants_router"]

_session = Depends(require_ui_session)


async def _operator(session: UISessionContext = _session) -> Operator:
    operator = await _lift_operator(session)
    if operator.tenant_role not in (TenantRole.OPERATOR, TenantRole.TENANT_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="grants_require_operator",
        )
    return operator


async def _admin(session: UISessionContext = _session) -> Operator:
    operator = await _operator(session)
    if operator.tenant_role != TenantRole.TENANT_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="grants_require_tenant_admin",
        )
    return operator


def _csrf(response: HTMLResponse, session: UISessionContext) -> str:
    token = mint_csrf_token(str(session.session_id))
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
    return token


def _iso(raw: str | None) -> datetime | str | None:
    if raw is None or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip())
    except ValueError:
        return raw.strip()


async def _target_label(target_id: UUID, tenant_id: UUID) -> str:
    """Resolve a concrete target through the tenant-scoped resolver."""
    async with get_sessionmaker()() as db_session:
        target = await resolve_target_by_id(db_session, tenant_id, target_id)
    return target.name if target else str(target_id)


async def _agent_row(grant: AgentGrantRead, tenant_id: UUID) -> dict[str, object]:
    target_scope = grant.target_scope
    try:
        target = await _target_label(UUID(target_scope), tenant_id) if target_scope else None
    except ValueError:
        target = target_scope
    return {
        "id": str(grant.id),
        "kind": "agent",
        "subject": subject_ref(grant.principal_sub, grant.principal_name),
        "op": grant.op_pattern,
        "connector": None,
        "target": target or "any target",
        "effect": grant.verdict,
        "created_by": grant.created_by_sub,
        "expires_at": grant.expires_at,
    }


async def _service_row(grant: ServiceGrantRead, tenant_id: UUID) -> dict[str, object]:
    selector = ", ".join(
        value for value in (grant.target_product, grant.target_name_pattern) if value
    )
    effect = "active"
    if grant.revoked_at is not None:
        effect = "revoked"
    elif grant.expires_at is not None and grant.expires_at <= datetime.now(UTC):
        effect = "expired"
    return {
        "id": str(grant.id),
        "kind": "service",
        "subject": subject_ref(grant.principal_sub, None),
        "op": grant.op_id,
        "connector": grant.connector_id,
        "target": (
            await _target_label(grant.target_id, tenant_id)
            if grant.target_id
            else (selector or "targetless")
        ),
        "effect": effect,
        "created_by": grant.created_by_sub,
        "expires_at": grant.expires_at,
    }


async def _index(
    request: Request,
    principal: str | None = Query(default=None, max_length=512),
    include_expired: bool = Query(default=False),
    include_revoked: bool = Query(default=False),
    service_offset: int = Query(default=0, ge=0),
    agent_offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    session: UISessionContext = _session,
    operator: Operator = Depends(_operator),
) -> HTMLResponse:
    principal = principal.strip() if principal else None
    service_grants = await ServicePrincipalGrantService().list_(
        session.tenant_id,
        principal_sub=principal or None,
        include_expired=include_expired,
        include_revoked=include_revoked,
        limit=limit + 1,
        offset=service_offset,
    )
    agent_rows: list[dict[str, object]] = []
    if operator.tenant_role == TenantRole.TENANT_ADMIN:
        agents = await AgentGrantService().list_(
            session.tenant_id,
            principal_sub=principal or None,
            include_expired=include_expired,
            limit=limit + 1,
            offset=agent_offset,
        )
        agent_has_next = len(agents) > limit
        agent_rows = [await _agent_row(entry, session.tenant_id) for entry in agents[:limit]]
    else:
        agent_has_next = False
    service_has_next = len(service_grants) > limit
    csrf_token = mint_csrf_token(str(session.session_id))
    context = {
        "page_title": "Permission grants",
        "active_surface": "grants",
        "can_write": operator.tenant_role == TenantRole.TENANT_ADMIN,
        "agent_rows": agent_rows,
        "service_rows": [
            await _service_row(entry, session.tenant_id) for entry in service_grants[:limit]
        ],
        "principal": principal or "",
        "include_expired": include_expired,
        "include_revoked": include_revoked,
        "service_offset": service_offset,
        "agent_offset": agent_offset,
        "limit": limit,
        "service_has_next": service_has_next,
        "agent_has_next": agent_has_next,
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(request, "grants/index.html", context)
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
    return response


async def _create_modal(request: Request, session: UISessionContext = _session) -> HTMLResponse:
    response = get_templates().TemplateResponse(
        request,
        "grants/_create.html",
        {
            "csrf_token": mint_csrf_token(str(session.session_id)),
            "verdicts": [v.value for v in GrantVerdict],
        },
    )
    _csrf(response, session)
    return response


async def _create(
    request: Request,
    kind: str = Form(default=""),
    principal_sub: str = Form(default=""),
    op: str = Form(default=""),
    connector_id: str = Form(default=""),
    target_id: str | None = Form(default=None),
    target_scope: str | None = Form(default=None),
    target_product: str | None = Form(default=None),
    target_name_pattern: str | None = Form(default=None),
    reason: str | None = Form(default=None),
    verdict: str = Form(default="deny"),
    expires_at: str | None = Form(default=None),
    session: UISessionContext = _session,
    operator: Operator = Depends(_admin),
) -> HTMLResponse:
    try:
        if kind == "agent":
            if not is_human_principal(operator):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="human_principal_required",
                )
            agent_payload = AgentGrantCreate.model_validate(
                {
                    "principal_sub": principal_sub,
                    "op_pattern": op,
                    "target_scope": target_scope or None,
                    "verdict": verdict,
                    "expires_at": _iso(expires_at),
                }
            )
            await AgentGrantService().grant(session.tenant_id, session.operator_sub, agent_payload)
        elif kind == "service":
            service_payload = ServiceGrantCreate.model_validate(
                {
                    "principal_sub": principal_sub,
                    "op_id": op,
                    "connector_id": connector_id,
                    "target_id": target_id or None,
                    "target_product": target_product or None,
                    "target_name_pattern": target_name_pattern or None,
                    "reason": reason or "",
                    "expires_at": _iso(expires_at),
                }
            )
            structlog.contextvars.bind_contextvars(
                audit_op_id="service_grant.create",
                audit_op_class="write",
                audit_agent_name=service_payload.principal_sub,
            )
            await ServicePrincipalGrantService().create(
                session.tenant_id, session.operator_sub, service_payload
            )
        else:
            raise ValueError("choose agent or service grant")
    except (
        ValidationError,
        AgentGrantValidationError,
        ServiceGrantValidationError,
        ValueError,
    ) as exc:
        form_kind = kind if kind in {"agent", "service"} else "agent"
        errors = (
            {
                str(error.get("loc", ("__root__",))[0]): str(error.get("msg", "invalid value"))
                for error in exc.errors()
            }
            if isinstance(exc, ValidationError)
            else {"__root__": str(exc)}
        )
        response = get_templates().TemplateResponse(
            request,
            "grants/_create.html",
            {
                "csrf_token": mint_csrf_token(str(session.session_id)),
                "verdicts": [v.value for v in GrantVerdict],
                "errors": errors,
                "values": {
                    "kind": form_kind,
                    "principal_sub": principal_sub,
                    "op": op,
                    "connector_id": connector_id,
                    "target_id": target_id or "",
                    "target_scope": target_scope or "",
                    "target_product": target_product or "",
                    "target_name_pattern": target_name_pattern or "",
                    "reason": reason or "",
                    "verdict": verdict,
                    "expires_at": expires_at or "",
                },
            },
            status_code=422,
        )
        _csrf(response, session)
        return response
    return HTMLResponse(status_code=204, headers={"HX-Redirect": "/ui/grants"})


async def _revoke(
    kind: str,
    grant_id: UUID,
    session: UISessionContext = _session,
    operator: Operator = Depends(_admin),
) -> HTMLResponse:
    del operator
    if kind == "agent":
        revoked = await AgentGrantService().revoke(session.tenant_id, grant_id)
    elif kind == "service":
        structlog.contextvars.bind_contextvars(
            audit_op_id="service_grant.revoke",
            audit_op_class="write",
        )
        revoked = await ServicePrincipalGrantService().revoke(
            session.tenant_id, grant_id, session.operator_sub
        )
    else:
        raise HTTPException(status_code=404, detail="grant_not_found")
    if not revoked:
        raise HTTPException(status_code=404, detail="grant_not_found")
    return HTMLResponse(status_code=204, headers={"HX-Redirect": "/ui/grants"})


def build_grants_router() -> APIRouter:
    router = APIRouter(tags=["ui-grants"])
    router.add_api_route("/ui/grants", _index, methods=["GET"], response_class=HTMLResponse)
    router.add_api_route(
        "/ui/grants/create",
        _create_modal,
        methods=["GET"],
        response_class=HTMLResponse,
        dependencies=[Depends(_admin)],
    )
    router.add_api_route(
        "/ui/grants/create", _create, methods=["POST"], response_class=HTMLResponse
    )
    router.add_api_route(
        "/ui/grants/{kind}/{grant_id}/revoke",
        _revoke,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    return router

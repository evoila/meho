# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant picker and tenant switch for the web interface (#5240).

A person can be in several tenants. The web session works in one
current tenant (the ``web_session`` row, see
:mod:`meho_backplane.ui.auth.tenant`). This module adds two routes:

* ``GET /ui/tenant`` -- the page "Choose a tenant". A person with
  several tenants sees it right after login (the session middleware
  allows no other page until a tenant is chosen). It also opens from
  the sidebar tenant box. It lists the person's tenants with the role in
  each; a platform admin sees every tenant that exists.
* ``POST /ui/tenant/switch`` -- switch to the tenant in the ``tenant``
  form field. The token is checked again with that tenant through the
  shared tenant choice
  (:func:`~meho_backplane.auth.current_tenant.select_current_tenant`),
  so a tenant the token does not allow is refused and nothing changes.
  On success the session row moves, an audit row records "tenant
  switched from A to B", and the dashboard opens. The form uses the
  same form protection as every other web form (:mod:`meho_backplane.ui.csrf`).

Design: ``docs/decisions/2026-10-28-several-tenants-per-person.md``
section 2, "Web interface".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from meho_backplane.auth.current_tenant import bind_tenant_role_source
from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.operator import Operator, is_human_principal
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.refresh import (
    load_fresh_session,
    verify_access_token_with_refresh,
)
from meho_backplane.ui.auth.session_store import DecryptedSession
from meho_backplane.ui.auth.tenant import (
    TENANT_PICKER_PATH,
    TENANT_REMOVED_NOTICE,
    TENANT_SWITCH_PATH,
    can_switch_tenant,
    save_session_tenant,
)
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = ["TenantChoice", "build_tenant_router", "lift_session_operator", "tenant_choices"]

_log = structlog.get_logger(__name__)

_require_ui_session_dep = Depends(require_ui_session)

#: Where a switch lands: the dashboard.
_DASHBOARD_PATH: Final[str] = "/ui/"

#: The audit ``op_id`` of a tenant switch.
_SWITCH_OP_ID: Final[str] = "meho.ui.tenant.switch"

#: Role label of a tenant a platform admin may enter without a group there.
_PLATFORM_ADMIN_ROLE: Final[str] = "Admin (platform admin)"


@dataclass(frozen=True)
class TenantChoice:
    """One tenant on the picker or the account page.

    * ``role`` -- the person's role there, in words: the login group kind
      (``Readers``, ``Operators``, ...) or, for a token without a tenant
      list, the role value.
    * ``member`` -- ``False`` only for a tenant a platform admin may enter
      without a role group there.
    """

    id: str
    name: str
    role: str
    member: bool = True


async def _tenant_names() -> dict[uuid.UUID, str]:
    """Return the name of every tenant row (display name, else slug)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        rows = (await db_session.execute(select(Tenant.id, Tenant.slug, Tenant.name))).all()
    return {row.id: row.name or row.slug for row in rows}


async def tenant_choices(operator: Operator, *, include_all: bool = True) -> list[TenantChoice]:
    """Return the tenants *operator* may work in, with the role in each.

    The person's own tenants come first, in token order. With
    *include_all* and a platform admin, every other existing tenant
    follows. *operator* is the token's view; its current tenant does not
    change the list.
    """
    names = await _tenant_names()
    if operator.tenants:
        choices = [
            TenantChoice(
                id=str(entry.tenant_id),
                name=names.get(entry.tenant_id, entry.name),
                role=entry.group.value,
            )
            for entry in operator.tenants
        ]
    else:
        choices = [
            TenantChoice(
                id=str(operator.tenant_id),
                name=names.get(operator.tenant_id, str(operator.tenant_id)),
                role=operator.tenant_role.value,
            )
        ]
    if include_all and operator.platform_admin and is_human_principal(operator):
        own = {choice.id for choice in choices}
        choices.extend(
            TenantChoice(id=str(tenant_id), name=name, role=_PLATFORM_ADMIN_ROLE, member=False)
            for tenant_id, name in sorted(names.items(), key=lambda item: item[1])
            if str(tenant_id) not in own
        )
    return choices


async def _load_session(session_ctx: UISessionContext) -> DecryptedSession:
    """Load the session row, or raise ``401 ui_session_required``."""
    decrypted = await load_fresh_session(session_ctx.session_id)
    if decrypted is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ui_session_required",
        )
    return decrypted


async def lift_session_operator(session_ctx: UISessionContext) -> tuple[DecryptedSession, Operator]:
    """Check the session's token now and return the (maybe refreshed) session + Operator.

    The Operator works in the session's current tenant, or has
    ``tenant_chosen=False`` when no tenant was picked yet.
    """
    decrypted = await _load_session(session_ctx)
    return await verify_access_token_with_refresh(
        decrypted,
        expected_audience=get_settings().keycloak_audience,
    )


def _notice_text(notice: str | None, *, chosen: bool, current_name: str | None) -> str | None:
    """The plain message on top of the picker, if any."""
    if notice != TENANT_REMOVED_NOTICE:
        return None
    if chosen and current_name:
        return (
            "You are no longer in the tenant you worked in. "
            f"MEHO now works in the tenant {current_name}."
        )
    return "You are no longer in the tenant you worked in. Choose a tenant to go on."


def _set_csrf_cookie(response: Response, csrf_token: str) -> None:
    """Set the double-submit ``meho_csrf`` cookie for the picker's forms."""
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


async def _render_picker(
    request: Request,
    session_ctx: UISessionContext,
    *,
    notice: str | None,
) -> HTMLResponse:
    """Render ``GET /ui/tenant``."""
    _decrypted, operator = await lift_session_operator(session_ctx)
    choices = await tenant_choices(operator)
    chosen = operator.tenant_chosen
    current_id = str(operator.tenant_id) if chosen else None
    current_name = next((c.name for c in choices if c.id == current_id), None)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "choices": choices,
        "current_id": current_id,
        "notice_text": _notice_text(notice, chosen=chosen, current_name=current_name),
        "csrf_token": csrf_token,
        "switch_path": TENANT_SWITCH_PATH,
        "dashboard_path": _DASHBOARD_PATH if chosen else None,
    }
    response = get_templates().TemplateResponse(request, "tenant/choose.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


def _refused(request: Request, message: str) -> HTMLResponse:
    """Render the refusal page of a switch (403). Nothing changed."""
    context: dict[str, object] = {"message": message, "picker_path": TENANT_PICKER_PATH}
    return get_templates().TemplateResponse(
        request,
        "tenant/refused.html",
        context,
        status_code=status.HTTP_403_FORBIDDEN,
    )


def _bind_switch_audit(operator: Operator, *, old_tenant: str | None) -> None:
    """Bind the contextvars the audit middleware writes the switch row from.

    The row lands in the new tenant and names the old one:
    "tenant switched from A to B".
    """
    new_tenant = str(operator.tenant_id)
    structlog.contextvars.bind_contextvars(
        operator_sub=operator.sub,
        tenant_id=new_tenant,
        audit_op_id=_SWITCH_OP_ID,
        audit_op_class="write",
        audit_from_tenant_id=old_tenant,
        audit_to_tenant_id=new_tenant,
        audit_summary=f"tenant switched from {old_tenant or 'none'} to {new_tenant}",
    )
    bind_tenant_role_source(operator)


async def _switch(request: Request, session_ctx: UISessionContext) -> Response:
    """Handle ``POST /ui/tenant/switch``."""
    form = await request.form()
    wanted = str(form.get("tenant") or "").strip()
    if not wanted:
        return _refused(request, "Choose a tenant from the list.")
    # A fresh check of the token first (refreshes an expired token once).
    decrypted, current = await lift_session_operator(session_ctx)
    try:
        operator = await verify_jwt_for_audience(
            f"Bearer {decrypted.access_token}",
            expected_audience=get_settings().keycloak_audience,
            requested_tenant=wanted,
        )
    except HTTPException as exc:
        if exc.status_code not in (status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN):
            raise
        _log.info(
            "ui_tenant_switch_refused",
            session_id=str(session_ctx.session_id),
            operator_sub=session_ctx.operator_sub,
            reason=str(exc.detail).split(":", 1)[0],
        )
        return _refused(request, "You cannot work in this tenant. Nothing changed.")
    old_tenant = str(current.tenant_id) if current.tenant_chosen else None
    await save_session_tenant(
        session_ctx.session_id,
        tenant_id=operator.tenant_id,
        tenant_chosen=True,
        can_switch=can_switch_tenant(operator),
    )
    _bind_switch_audit(operator, old_tenant=old_tenant)
    _log.info(
        "ui_tenant_switched",
        session_id=str(session_ctx.session_id),
        operator_sub=operator.sub,
        from_tenant_id=old_tenant,
        to_tenant_id=str(operator.tenant_id),
        role_from_platform_admin=operator.role_from_platform_admin,
    )
    return RedirectResponse(url=_DASHBOARD_PATH, status_code=status.HTTP_303_SEE_OTHER)


def build_tenant_router() -> APIRouter:
    """Construct the tenant picker + switch :class:`APIRouter`."""
    router = APIRouter(tags=["ui-tenant"])

    async def _picker(
        request: Request,
        notice: str | None = None,
        session_ctx: UISessionContext = _require_ui_session_dep,
    ) -> HTMLResponse:
        """``GET /ui/tenant``: choose the tenant to work in."""
        return await _render_picker(request, session_ctx, notice=notice)

    async def _switch_route(
        request: Request,
        session_ctx: UISessionContext = _require_ui_session_dep,
    ) -> Response:
        """``POST /ui/tenant/switch``: work in another tenant."""
        return await _switch(request, session_ctx)

    router.add_api_route(
        TENANT_PICKER_PATH,
        _picker,
        methods=["GET"],
        name="ui_tenant_picker",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        TENANT_SWITCH_PATH,
        _switch_route,
        methods=["POST"],
        name="ui_tenant_switch",
        response_class=HTMLResponse,
        responses={
            303: {"description": "Switched. Opens the dashboard."},
            403: {
                "description": "The token does not allow this tenant. Nothing changed.",
                "content": {"text/html": {}},
            },
        },
    )
    return router

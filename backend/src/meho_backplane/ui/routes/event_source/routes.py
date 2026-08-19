# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Routes for the ``event_source`` admin UI (#2880).

Classic server-rendered forms + POST/redirect-get, CSRF via the shared
double-submit token. Every write reuses the REST handler in
:mod:`meho_backplane.api.v1.event_source` (the ``meho_targets_register``
-> ``create_target`` reuse pattern) so persistence, tenant scoping, and
Vault secret custody are shared, not duplicated. Reads run at operator
role; writes gate to ``tenant_admin`` via
:func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`
(403 otherwise).
"""

from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1.event_source import (
    create_event_source,
    delete_event_source,
    list_event_sources,
    update_event_source,
)
from meho_backplane.db.engine import get_raw_session, get_session
from meho_backplane.event_source.resolver import resolve_event_source
from meho_backplane.event_source.schemas import (
    EventSourceAuthStrategy,
    EventSourceCreate,
    EventSourceKind,
    EventSourceStatus,
    EventSourceUpdate,
)
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.connectors.operator import (
    resolve_operator_or_403,
    resolve_role_probe,
)
from meho_backplane.ui.routes.memory.operator import build_read_operator
from meho_backplane.ui.templating import get_templates

__all__ = ["build_event_source_router"]

_log = structlog.get_logger(__name__)

_LIST_LIMIT = 200
_KIND_OPTIONS = [k.value for k in EventSourceKind]
_AUTH_OPTIONS = [a.value for a in EventSourceAuthStrategy]
_STATUS_OPTIONS = [s.value for s in EventSourceStatus]

_require_session_dep = Depends(require_ui_session)
_read_session_dep = Depends(get_session)
_raw_session_dep = Depends(get_raw_session)


# --------------------------------------------------------------------------- helpers


def _set_csrf_cookie(response: HTMLResponse, token: str) -> None:
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _form_context(
    *,
    mode: str,
    csrf_token: str,
    values: dict[str, str],
    error: str | None = None,
) -> dict[str, object]:
    return {
        "page_title": "Event Sources",
        "active_surface": "event-source",
        "mode": mode,
        "csrf_token": csrf_token,
        "values": values,
        "error": error,
        "kind_options": _KIND_OPTIONS,
        "auth_options": _AUTH_OPTIONS,
        "status_options": _STATUS_OPTIONS,
    }


def _form_error(
    request: Request, *, mode: str, token: str, values: dict[str, str], error: str
) -> HTMLResponse:
    """Re-render the form with an inline error and refresh the CSRF cookie."""
    response = get_templates().TemplateResponse(
        request,
        "event_source/form.html",
        _form_context(mode=mode, csrf_token=token, values=values, error=error),
    )
    _set_csrf_cookie(response, token)
    return response


def _parse_extras(raw: str) -> dict[str, object]:
    """Parse the extras textarea; empty is ``{}``. Raises ValueError on bad JSON."""
    raw = raw.strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("extras must be a JSON object")
    return parsed


def _detail(exc: HTTPException) -> str:
    """Flatten an HTTPException detail (str or structured dict) to one line."""
    if isinstance(exc.detail, str):
        return exc.detail
    if isinstance(exc.detail, dict):
        message = exc.detail.get("message") or exc.detail.get("error")
        if message:
            return str(message)
    return f"request failed ({exc.status_code})"


# --------------------------------------------------------------------------- handlers


async def _list(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    session: AsyncSession = _read_session_dep,
) -> HTMLResponse:
    operator = build_read_operator(session_ctx)
    probe = await resolve_role_probe(request, session_ctx)
    envelope = await list_event_sources(
        operator=operator, session=session, status_filter=None, limit=_LIST_LIMIT, cursor=None
    )
    # Mint the session-stable CSRF token so the inline delete forms submit.
    token = mint_csrf_token(str(session_ctx.session_id))
    context = {
        "page_title": "Event Sources",
        "active_surface": "event-source",
        "items": envelope.items,
        "is_tenant_admin": probe.is_tenant_admin,
        "csrf_token": token,
    }
    response = get_templates().TemplateResponse(request, "event_source/list.html", context)
    _set_csrf_cookie(response, token)
    return response


async def _new_form(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
) -> HTMLResponse:
    await resolve_operator_or_403(request, session_ctx)
    token = mint_csrf_token(str(session_ctx.session_id))
    response = get_templates().TemplateResponse(
        request,
        "event_source/form.html",
        _form_context(mode="create", csrf_token=token, values={"status": "active"}),
    )
    _set_csrf_cookie(response, token)
    return response


async def _create(
    request: Request,
    name: str = Form(...),
    slug: str = Form(...),
    kind: str = Form(...),
    auth_strategy: str = Form(...),
    status_value: str = Form("active", alias="status"),
    extras: str = Form(""),
    secret: str = Form(""),
    session_ctx: UISessionContext = _require_session_dep,
    session: AsyncSession = _raw_session_dep,
) -> HTMLResponse | RedirectResponse:
    operator = await resolve_operator_or_403(request, session_ctx)
    token = mint_csrf_token(str(session_ctx.session_id))
    submitted = {
        "name": name,
        "slug": slug,
        "kind": kind,
        "auth_strategy": auth_strategy,
        "status": status_value,
        "extras": extras,
    }
    try:
        body = EventSourceCreate(
            name=name,
            slug=slug,
            kind=EventSourceKind(kind),
            auth_strategy=EventSourceAuthStrategy(auth_strategy),
            status=EventSourceStatus(status_value),
            extras=_parse_extras(extras),
            secret=SecretStr(secret) if secret else None,
        )
    except (ValidationError, ValueError) as exc:
        await session.rollback()
        return _form_error(request, mode="create", token=token, values=submitted, error=str(exc))
    try:
        await create_event_source(body=body, operator=operator, session=session)
        await session.commit()
    except HTTPException as exc:
        await session.rollback()
        return _form_error(
            request, mode="create", token=token, values=submitted, error=_detail(exc)
        )
    _log.info("ui_event_source_create", tenant_id=str(operator.tenant_id), slug=slug)
    return RedirectResponse("/ui/event-sources", status_code=303)


async def _edit_form(
    request: Request,
    slug: str,
    session_ctx: UISessionContext = _require_session_dep,
    session: AsyncSession = _read_session_dep,
) -> HTMLResponse:
    operator = await resolve_operator_or_403(request, session_ctx)
    row = await resolve_event_source(session, operator.tenant_id, slug)
    token = mint_csrf_token(str(session_ctx.session_id))
    values = {
        "name": row.name,
        "slug": row.slug,
        "kind": row.kind,
        "auth_strategy": row.auth_strategy,
        "status": row.status,
        "extras": json.dumps(row.extras, indent=2) if row.extras else "",
        "has_secret": "yes" if row.secret_ref else "",
    }
    response = get_templates().TemplateResponse(
        request,
        "event_source/form.html",
        _form_context(mode="edit", csrf_token=token, values=values),
    )
    _set_csrf_cookie(response, token)
    return response


async def _edit(
    request: Request,
    slug: str,
    kind: str = Form(...),
    auth_strategy: str = Form(...),
    status_value: str = Form(..., alias="status"),
    extras: str = Form(""),
    secret: str = Form(""),
    session_ctx: UISessionContext = _require_session_dep,
    session: AsyncSession = _raw_session_dep,
) -> HTMLResponse | RedirectResponse:
    operator = await resolve_operator_or_403(request, session_ctx)
    token = mint_csrf_token(str(session_ctx.session_id))
    submitted = {
        "slug": slug,
        "kind": kind,
        "auth_strategy": auth_strategy,
        "status": status_value,
        "extras": extras,
    }
    try:
        body = EventSourceUpdate(
            kind=EventSourceKind(kind),
            auth_strategy=EventSourceAuthStrategy(auth_strategy),
            status=EventSourceStatus(status_value),
            extras=_parse_extras(extras),
            secret=SecretStr(secret) if secret else None,
        )
    except (ValidationError, ValueError) as exc:
        await session.rollback()
        return _form_error(request, mode="edit", token=token, values=submitted, error=str(exc))
    try:
        await update_event_source(slug=slug, body=body, operator=operator, session=session)
        await session.commit()
    except HTTPException as exc:
        await session.rollback()
        return _form_error(request, mode="edit", token=token, values=submitted, error=_detail(exc))
    _log.info("ui_event_source_update", tenant_id=str(operator.tenant_id), slug=slug)
    return RedirectResponse("/ui/event-sources", status_code=303)


async def _delete(
    request: Request,
    slug: str,
    session_ctx: UISessionContext = _require_session_dep,
    session: AsyncSession = _raw_session_dep,
) -> RedirectResponse:
    operator = await resolve_operator_or_403(request, session_ctx)
    try:
        await delete_event_source(slug=slug, operator=operator, session=session)
        await session.commit()
    except HTTPException:
        # A double-fire (already gone) is benign; land back on the list.
        await session.rollback()
    _log.info("ui_event_source_delete", tenant_id=str(operator.tenant_id), slug=slug)
    return RedirectResponse("/ui/event-sources", status_code=303)


def build_event_source_router() -> APIRouter:
    """Construct the event-source admin UI router.

    Static-prefix routes (``/new``) register before the ``{slug}`` routes
    so a literal is never bound as a slug. ``response_model=None`` on the
    POST routes: they return ``HTMLResponse`` (inline error) |
    ``RedirectResponse`` (PRG success), a union FastAPI cannot model.
    """
    router = APIRouter(tags=["ui-event-source"])
    router.add_api_route("/ui/event-sources", _list, methods=["GET"], response_class=HTMLResponse)
    router.add_api_route(
        "/ui/event-sources/new", _new_form, methods=["GET"], response_class=HTMLResponse
    )
    router.add_api_route("/ui/event-sources", _create, methods=["POST"], response_model=None)
    router.add_api_route(
        "/ui/event-sources/{slug}/edit", _edit_form, methods=["GET"], response_class=HTMLResponse
    )
    router.add_api_route(
        "/ui/event-sources/{slug}/edit", _edit, methods=["POST"], response_model=None
    )
    router.add_api_route(
        "/ui/event-sources/{slug}/delete", _delete, methods=["POST"], response_model=None
    )
    return router

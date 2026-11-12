# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Account page: "Notify me about approvals in this browser".

Routes (all answer 404 while the admin setting ``WEB_PUSH_ENABLED`` is off,
or while the push key is missing):

* ``GET /ui/account/push/subscriptions`` -- the list fragment: the
  person's own subscriptions, and for a ``tenant_admin`` every
  subscription made in the current tenant.
* ``POST /ui/account/push/subscribe`` -- form fields ``endpoint``,
  ``p256dh``, ``auth`` from the browser's push subscription. Checked by
  :func:`~meho_backplane.web_push.endpoints.check_subscription`; a refused
  subscription gets 422 with one plain sentence. Stored for the signed-in
  person (from the session, never from the body), with the person's login
  facts in the current tenant (read from their login; see
  :mod:`meho_backplane.web_push.recipients`).
* ``POST /ui/account/push/unsubscribe`` -- form field ``endpoint``: deletes
  the person's own subscription of this browser.
* ``POST /ui/account/push/status`` -- form field ``endpoint``: answers
  ``{"on": true}`` when the signed-in person has a stored subscription for
  this browser, else ``{"on": false}``. The switch shows this answer, not
  only what the browser holds. It is a POST so the endpoint never lands in
  a URL or an access log. When it is on, the person's login facts in the
  current tenant are read again from their login, so a changed login group
  counts from the next page open.
* ``POST /ui/account/push/remove`` -- form field ``subscription_id``:
  deletes one subscription of the person, or (for a ``tenant_admin``) one
  made in the current tenant. Any other id is 404.

The script ``static/src/app/push-subscribe.js`` drives the switch. Logs name
the person, tenant, row id and push service, never the endpoint or keys.
When the database refuses a write, the route answers 503 with one plain
sentence and logs only the event name (:class:`PushStoreError`).
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_session
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.roles import is_ui_tenant_admin, resolve_ui_operator
from meho_backplane.ui.templating import get_templates
from meho_backplane.web_push.endpoints import (
    SubscriptionRefusedError,
    check_subscription,
    push_service_name,
)
from meho_backplane.web_push.keys import push_public_key
from meho_backplane.web_push.recipients import login_facts_for
from meho_backplane.web_push.store import (
    PushStoreError,
    delete_own_by_endpoint,
    is_own_endpoint,
    list_for_person,
    list_for_tenant,
    refresh_login_facts,
    remove_subscription,
    save_subscription,
)

__all__ = ["build_account_push_router"]

_log = structlog.get_logger(__name__)

_require_ui_session_dep = Depends(require_ui_session)
_get_session_dep = Depends(get_session)

_NOT_FOUND = "Not Found"
_STORE_FAILED = "MEHO could not store that just now. Try again."


def _store_failed(event: str, session_ctx: UISessionContext) -> PlainTextResponse:
    _log.warning(
        event,
        operator_sub=session_ctx.operator_sub,
        tenant_id=str(session_ctx.tenant_id),
    )
    return PlainTextResponse(_STORE_FAILED, status_code=503)


def _require_push_on() -> None:
    """Answer 404 unless the admin setting is on and the key is usable."""
    if push_public_key() is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)


async def _render_list(
    request: Request, *, session_ctx: UISessionContext, db_session: AsyncSession
) -> HTMLResponse:
    is_admin = await is_ui_tenant_admin(session_ctx)
    own = await list_for_person(db_session, operator_sub=session_ctx.operator_sub)
    tenant_rows = (
        await list_for_tenant(db_session, tenant_id=session_ctx.tenant_id) if is_admin else []
    )
    context: dict[str, object] = {
        "own_subscriptions": own,
        "tenant_subscriptions": tenant_rows,
        "is_tenant_admin": is_admin,
    }
    return get_templates().TemplateResponse(request, "account/_push_subscriptions.html", context)


async def _list(
    request: Request,
    session_ctx: UISessionContext = _require_ui_session_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    _require_push_on()
    return await _render_list(request, session_ctx=session_ctx, db_session=db_session)


async def _subscribe(
    request: Request,
    endpoint: str = Form(default=""),
    p256dh: str = Form(default=""),
    auth: str = Form(default=""),
    session_ctx: UISessionContext = _require_ui_session_dep,
    db_session: AsyncSession = _get_session_dep,
) -> Response:
    _require_push_on()
    try:
        checked = check_subscription(endpoint, p256dh, auth)
    except SubscriptionRefusedError as exc:
        _log.info(
            "ui_push_subscription_refused",
            operator_sub=session_ctx.operator_sub,
            tenant_id=str(session_ctx.tenant_id),
            reason=exc.message,
        )
        return PlainTextResponse(exc.message, status_code=422)
    try:
        await save_subscription(
            db_session,
            operator_sub=session_ctx.operator_sub,
            tenant_id=session_ctx.tenant_id,
            subscription=checked,
            facts=login_facts_for(await resolve_ui_operator(session_ctx)),
        )
    except PushStoreError:
        return _store_failed("ui_push_subscription_save_failed", session_ctx)
    _log.info(
        "ui_push_subscription_saved",
        operator_sub=session_ctx.operator_sub,
        tenant_id=str(session_ctx.tenant_id),
        service=push_service_name(checked.endpoint),
    )
    return await _render_list(request, session_ctx=session_ctx, db_session=db_session)


async def _unsubscribe(
    request: Request,
    endpoint: str = Form(default=""),
    session_ctx: UISessionContext = _require_ui_session_dep,
    db_session: AsyncSession = _get_session_dep,
) -> Response:
    _require_push_on()
    try:
        deleted = await delete_own_by_endpoint(
            db_session, operator_sub=session_ctx.operator_sub, endpoint=endpoint
        )
    except PushStoreError:
        return _store_failed("ui_push_subscription_delete_failed", session_ctx)
    _log.info(
        "ui_push_subscription_switched_off",
        operator_sub=session_ctx.operator_sub,
        tenant_id=str(session_ctx.tenant_id),
        deleted=deleted,
    )
    return await _render_list(request, session_ctx=session_ctx, db_session=db_session)


async def _status(
    endpoint: str = Form(default=""),
    session_ctx: UISessionContext = _require_ui_session_dep,
    db_session: AsyncSession = _get_session_dep,
) -> Response:
    _require_push_on()
    try:
        on = await is_own_endpoint(
            db_session, operator_sub=session_ctx.operator_sub, endpoint=endpoint
        )
        # No login read (a short outage) keeps the stored facts: writing
        # "none" would stop the messages until the next page open.
        operator = await resolve_ui_operator(session_ctx) if on else None
        if operator is not None:
            await refresh_login_facts(
                db_session,
                operator_sub=session_ctx.operator_sub,
                tenant_id=session_ctx.tenant_id,
                facts=login_facts_for(operator),
            )
    except PushStoreError:
        return _store_failed("ui_push_subscription_status_failed", session_ctx)
    return JSONResponse({"on": on})


async def _remove(
    request: Request,
    subscription_id: str = Form(default=""),
    session_ctx: UISessionContext = _require_ui_session_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    _require_push_on()
    try:
        wanted = uuid.UUID(subscription_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="subscription not found") from None
    is_admin = await is_ui_tenant_admin(session_ctx)
    deleted = await remove_subscription(
        db_session,
        subscription_id=wanted,
        operator_sub=session_ctx.operator_sub,
        admin_of_tenant=session_ctx.tenant_id if is_admin else None,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="subscription not found")
    _log.info(
        "ui_push_subscription_removed",
        operator_sub=session_ctx.operator_sub,
        tenant_id=str(session_ctx.tenant_id),
        subscription_id=str(wanted),
        as_tenant_admin=is_admin,
    )
    return await _render_list(request, session_ctx=session_ctx, db_session=db_session)


def build_account_push_router() -> APIRouter:
    """Construct the push-subscription routes of the account page."""
    router = APIRouter(tags=["ui-account"])
    router.add_api_route(
        "/ui/account/push/subscriptions",
        _list,
        methods=["GET"],
        name="ui_account_push_subscriptions",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/account/push/subscribe",
        _subscribe,
        methods=["POST"],
        name="ui_account_push_subscribe",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/account/push/unsubscribe",
        _unsubscribe,
        methods=["POST"],
        name="ui_account_push_unsubscribe",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/account/push/status",
        _status,
        methods=["POST"],
        name="ui_account_push_status",
        response_class=JSONResponse,
    )
    router.add_api_route(
        "/ui/account/push/remove",
        _remove,
        methods=["POST"],
        name="ui_account_push_remove",
        response_class=HTMLResponse,
    )
    return router

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Console page of the admin block list: ``/ui/blocked-people``.

An admin sees the blocked logins, blocks a login with a reason, and
unblocks a login. The page calls the same functions as the REST routes
(:mod:`meho_backplane.auth.principal_block`), so the rules are the same:
a platform admin blocks in every tenant, a tenant admin in their own
tenant, and nobody blocks their own login. Only a person with
``tenant_admin`` may open the page or send a form. Each block and unblock
writes one audit row.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from meho_backplane.api.v1.principal_blocks import BlockCreate
from meho_backplane.auth.operator import Operator
from meho_backplane.auth.principal_block import (
    BLOCK_SCOPE_ALL,
    CannotBlockSelfError,
    PrincipalNotBlockedError,
    block_principal,
    list_blocked,
    unblock_principal,
)
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.agents.operator import resolve_human_admin_or_403
from meho_backplane.ui.templating import get_templates

__all__ = ["build_principal_blocks_router"]

_session = Depends(require_ui_session)
_admin = Depends(resolve_human_admin_or_403)

_SELF_BLOCK_MESSAGE = "You cannot block your own login."
_FIELD_MESSAGES = {
    "sub": "Enter the login id of the person.",
    "reason": "Enter a reason (at most 500 characters).",
}


def _set_csrf_cookie(response: HTMLResponse, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _errors_of(exc: ValidationError) -> dict[str, str]:
    errors: dict[str, str] = {}
    for item in exc.errors():
        field = str(item["loc"][-1]) if item["loc"] else "__root__"
        errors.setdefault(field, _FIELD_MESSAGES.get(field, "The value is not valid."))
    return errors


async def _index(
    request: Request,
    session: UISessionContext = _session,
    operator: Operator = _admin,
) -> HTMLResponse:
    token = mint_csrf_token(str(session.session_id))
    context = {
        "page_title": "Blocked people",
        "active_surface": "blocked-people",
        "blocked": await list_blocked(operator),
        "scope_all": BLOCK_SCOPE_ALL,
        "csrf_token": token,
    }
    response = get_templates().TemplateResponse(request, "principal_blocks/index.html", context)
    _set_csrf_cookie(response, token)
    return response


async def _block(
    request: Request,
    sub: str = Form(default=""),
    reason: str = Form(default=""),
    session: UISessionContext = _session,
    operator: Operator = _admin,
) -> HTMLResponse:
    sub, reason = sub.strip(), reason.strip()
    structlog.contextvars.bind_contextvars(audit_op_id="principal.block", audit_op_class="write")
    errors: dict[str, str] = {}
    try:
        body = BlockCreate(sub=sub, reason=reason)
        await block_principal(operator, body.sub, body.reason)
    except ValidationError as exc:
        errors = _errors_of(exc)
    except CannotBlockSelfError:
        errors = {"sub": _SELF_BLOCK_MESSAGE}
    if not errors:
        return HTMLResponse(status_code=204, headers={"HX-Redirect": "/ui/blocked-people"})
    token = mint_csrf_token(str(session.session_id))
    response = get_templates().TemplateResponse(
        request,
        "principal_blocks/_form.html",
        {"csrf_token": token, "errors": errors, "values": {"sub": sub, "reason": reason}},
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )
    _set_csrf_cookie(response, token)
    return response


async def _unblock(
    sub: str = Form(min_length=1, max_length=255),
    operator: Operator = _admin,
) -> HTMLResponse:
    structlog.contextvars.bind_contextvars(audit_op_id="principal.unblock", audit_op_class="write")
    try:
        await unblock_principal(operator, sub)
    except PrincipalNotBlockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="principal_not_blocked"
        ) from exc
    return HTMLResponse(status_code=204, headers={"HX-Redirect": "/ui/blocked-people"})


def build_principal_blocks_router() -> APIRouter:
    """The ``/ui/blocked-people`` page and its two forms."""
    router = APIRouter(tags=["ui-principal-blocks"])
    router.add_api_route("/ui/blocked-people", _index, methods=["GET"], response_class=HTMLResponse)
    router.add_api_route(
        "/ui/blocked-people", _block, methods=["POST"], response_class=HTMLResponse
    )
    router.add_api_route(
        "/ui/blocked-people/unblock", _unblock, methods=["POST"], response_class=HTMLResponse
    )
    return router

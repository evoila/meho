# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Console page of the admin block list: ``/ui/blocked-people``.

An admin sees the blocked logins, blocks a login with a reason, and
unblocks a login. The page calls the same functions as the REST routes
(:mod:`meho_backplane.auth.principal_block`), so the rules are the same:
a platform admin blocks and unblocks in every tenant, a tenant admin in
their own tenant, and nobody blocks their own login. Only a person with
``tenant_admin`` may open the page or send a form. The admin check uses
the signed-in session only.

Both forms are plain ``POST`` forms with the CSRF token in a hidden
field, so they work without JavaScript. A change answers ``303`` back to
the page. Each block and unblock writes one audit row named
``principal.block`` or ``principal.unblock``; the request record of the
form keeps its own ``http.post:`` name.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
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

_PAGE = "/ui/blocked-people"
_session = Depends(require_ui_session)


async def _human_admin(request: Request, session: UISessionContext = _session) -> Operator:
    """The signed-in person admin; the session comes from the session cookie only."""
    return await resolve_human_admin_or_403(request, session)


_admin = Depends(_human_admin)

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


async def _page(
    request: Request,
    session: UISessionContext,
    operator: Operator,
    *,
    errors: dict[str, str] | None = None,
    values: dict[str, str] | None = None,
) -> HTMLResponse:
    token = mint_csrf_token(str(session.session_id))
    context = {
        "page_title": "Blocked people",
        "active_surface": "blocked-people",
        "blocked": await list_blocked(operator),
        "scope_all": BLOCK_SCOPE_ALL,
        "platform_admin": operator.platform_admin,
        "csrf_token": token,
        "errors": errors or {},
        "values": values or {},
    }
    response = get_templates().TemplateResponse(
        request,
        "principal_blocks/index.html",
        context,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY if errors else status.HTTP_200_OK,
    )
    _set_csrf_cookie(response, token)
    return response


def _back_to_page() -> RedirectResponse:
    return RedirectResponse(_PAGE, status_code=status.HTTP_303_SEE_OTHER)


async def _index(
    request: Request,
    session: UISessionContext = _session,
    operator: Operator = _admin,
) -> HTMLResponse:
    return await _page(request, session, operator)


async def _block(
    request: Request,
    sub: str = Form(default=""),
    reason: str = Form(default=""),
    session: UISessionContext = _session,
    operator: Operator = _admin,
) -> HTMLResponse | RedirectResponse:
    sub, reason = sub.strip(), reason.strip()
    try:
        body = BlockCreate(sub=sub, reason=reason)
        await block_principal(operator, body.sub, body.reason)
    except ValidationError as exc:
        errors = _errors_of(exc)
    except CannotBlockSelfError:
        errors = {"sub": _SELF_BLOCK_MESSAGE}
    else:
        return _back_to_page()
    return await _page(
        request, session, operator, errors=errors, values={"sub": sub, "reason": reason}
    )


async def _unblock(
    sub: str = Form(min_length=1, max_length=255),
    operator: Operator = _admin,
) -> RedirectResponse:
    try:
        await unblock_principal(operator, sub)
    except PrincipalNotBlockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="principal_not_blocked"
        ) from exc
    return _back_to_page()


_HTML = {"text/html": {"schema": {"type": "string"}}}
_JSON_DETAIL = {
    "application/json": {"schema": {"type": "object", "properties": {"detail": {"type": "string"}}}}
}
_BACK_TO_PAGE: dict[int | str, dict[str, Any]] = {
    303: {"description": "Done. The answer sends the browser back to the page."}
}


def build_principal_blocks_router() -> APIRouter:
    """The ``/ui/blocked-people`` page and its two forms."""
    router = APIRouter(tags=["ui-principal-blocks"])
    router.add_api_route(_PAGE, _index, methods=["GET"], response_class=HTMLResponse)
    router.add_api_route(
        _PAGE,
        _block,
        methods=["POST"],
        status_code=status.HTTP_303_SEE_OTHER,
        response_class=RedirectResponse,
        response_model=None,
        responses={
            **_BACK_TO_PAGE,
            422: {
                "description": "The page again, with the error next to its field.",
                "content": _HTML,
            },
        },
    )
    router.add_api_route(
        f"{_PAGE}/unblock",
        _unblock,
        methods=["POST"],
        status_code=status.HTTP_303_SEE_OTHER,
        response_class=RedirectResponse,
        response_model=None,
        responses={
            **_BACK_TO_PAGE,
            404: {"description": "The login has no block you may remove.", "content": _JSON_DETAIL},
        },
    )
    return router

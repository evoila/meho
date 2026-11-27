# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Console page of the admin block list: ``/ui/blocked-people``.

An admin sees the blocked logins, blocks a login with a reason, and
unblocks a login. The form offers the agents in the admin's reach to
pick (``agent``), so nobody types the ``agent:`` prefix; the typed field
(``sub``) takes a login id, and MEHO stores its block. A typed text that
starts with ``agent:`` follows the same rule as the REST route. The list
names the agent of an agent's login.
The page calls the same functions as the REST routes
(:mod:`meho_backplane.auth.principal_block`), so the rules are the same:
a platform admin blocks and unblocks in every tenant, a tenant admin in
their own tenant, and nobody blocks their own login. Only a person with
``tenant_admin`` may open the page or send a form. The admin check uses
the signed-in session only.

Every form is a plain ``POST`` form with the CSRF token and the page's
tenant (``meho_tenant``) in hidden fields, so it works without
JavaScript, and a form from a page of another tenant gets ``409``. When
MEHO cannot read the block list, the page and both steps of an unblock
answer ``503`` with ``principal_block_check_unavailable``. A
change answers ``303`` back to the page. The unblock button of a row
opens a confirm page first; only the form of that page (``confirm=yes``)
removes blocks. That form carries the set of blocks the page showed
(``shown``), and the confirm removes only those blocks. When the blocks of
the login changed in between, the confirm removes nothing and shows the
confirm page again with the new blocks. The page never unblocks the admin's own login: that
needs a reason and a fresh sign-in, so the admin runs
``meho principal unblock <sub> --reason <text>`` in the tenant of the
block. On the own login MEHO removes only the block of the current
tenant, and a block in every tenant stays until another platform admin
removes it. Each block and unblock
writes one audit row named ``principal.block`` or ``principal.unblock``;
the request record of the form keeps its own ``http.post:`` name.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from meho_backplane.api.v1.principal_blocks import BlockCreate
from meho_backplane.auth.operator import Operator
from meho_backplane.auth.principal_block import (
    BLOCK_SCOPE_ALL,
    BlockedPerson,
    BlockSetChangedError,
    CannotBlockSelfError,
    PrincipalBlockUnavailableError,
    PrincipalNotBlockedError,
    UnknownLoginError,
    block_principal,
    block_set_digest,
    list_blocked,
    unblock_principal,
)
from meho_backplane.auth.principal_block_agents import agents_in_reach, is_agent_name
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.agents.operator import resolve_human_admin_or_403
from meho_backplane.ui.templating import get_templates

__all__ = ["build_principal_blocks_router"]

_PAGE = "/ui/blocked-people"
_log = structlog.get_logger(__name__)
_session = Depends(require_ui_session)


async def _human_admin(request: Request, session: UISessionContext = _session) -> Operator:
    """The signed-in person admin; the session comes from the session cookie only."""
    return await resolve_human_admin_or_403(request, session)


_admin = Depends(_human_admin)

_SELF_BLOCK_MESSAGE = "You cannot block your own login."
_OWN_LOGIN_UNBLOCK = (
    "own_login: the console does not remove a block on your own login. "
    "Run `meho principal unblock <sub> --reason <text>` in the tenant of the block, "
    "with a fresh sign-in. On your own login MEHO removes only the block of the "
    "current tenant. A block in every tenant stays until another platform admin removes it."
)
_CONFIRMED = "yes"
_SET_CHANGED = "The blocks of this login changed. Check the blocks below and confirm again."
_FIELD_MESSAGES = {
    "sub": "Pick an agent, or type a login id.",
    "reason": "Enter a reason (at most 500 characters).",
}
_BOTH_GIVEN = "Pick an agent or type a login id, not both."
_NOT_AN_AGENT = "Pick an agent from the list."


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
        "blocked": await _readable_blocks(operator),
        "agents": await agents_in_reach(operator),
        "scope_all": BLOCK_SCOPE_ALL,
        "platform_admin": operator.platform_admin,
        "own_sub": operator.sub,
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


async def _readable_blocks(operator: Operator) -> list[BlockedPerson]:
    """The blocks the admin may see; ``503`` when MEHO cannot read the list."""
    try:
        return await list_blocked(operator)
    except Exception as exc:
        _log.warning("principal_block_list_read_failed", exc_info=True)
        raise _unavailable() from exc


def _back_to_page() -> RedirectResponse:
    return RedirectResponse(_PAGE, status_code=status.HTTP_303_SEE_OTHER)


async def _index(
    request: Request,
    session: UISessionContext = _session,
    operator: Operator = _admin,
) -> HTMLResponse:
    return await _page(request, session, operator)


def _picked_or_typed(agent: str, sub: str) -> tuple[str, dict[str, str]]:
    """The block text and the form errors: the picked agent, or the typed login id."""
    if agent and sub:
        return "", {"agent": _BOTH_GIVEN}
    if agent and not is_agent_name(agent):
        return "", {"agent": _NOT_AN_AGENT}
    return agent or sub, {}


async def _block(
    request: Request,
    sub: str = Form(default=""),
    agent: str = Form(default=""),
    reason: str = Form(default=""),
    session: UISessionContext = _session,
    operator: Operator = _admin,
) -> HTMLResponse | RedirectResponse:
    sub, agent, reason = sub.strip(), agent.strip(), reason.strip()
    text, errors = _picked_or_typed(agent, sub)
    field = "agent" if agent else "sub"
    if not errors:
        try:
            body = BlockCreate(sub=text, reason=reason)
            await block_principal(operator, body.sub, body.reason)
        except ValidationError as exc:
            errors = _errors_of(exc)
        except CannotBlockSelfError:
            errors = {field: _SELF_BLOCK_MESSAGE}
        except UnknownLoginError as exc:
            errors = {field: str(exc)}
        else:
            return _back_to_page()
    values = {"sub": sub, "agent": agent, "reason": reason}
    return await _page(request, session, operator, errors=errors, values=values)


async def _confirm_page(
    request: Request,
    session: UISessionContext,
    operator: Operator,
    sub: str,
    blocks: list[BlockedPerson],
    *,
    changed: bool = False,
) -> HTMLResponse:
    token = mint_csrf_token(str(session.session_id))
    context = {
        "page_title": "Unblock a person",
        "active_surface": "blocked-people",
        "sub": sub,
        "blocks": blocks,
        "shown": block_set_digest(blocks),
        "changed_message": _SET_CHANGED if changed else "",
        "scope_all": BLOCK_SCOPE_ALL,
        "platform_admin": operator.platform_admin,
        "confirmed": _CONFIRMED,
        "csrf_token": token,
    }
    response = get_templates().TemplateResponse(
        request, "principal_blocks/confirm_unblock.html", context
    )
    _set_csrf_cookie(response, token)
    return response


def _not_blocked() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="principal_not_blocked")


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="principal_block_check_unavailable",
    )


async def _unblock(
    request: Request,
    sub: str = Form(min_length=1, max_length=255),
    confirm: str = Form(default=""),
    shown: str = Form(default=""),
    session: UISessionContext = _session,
    operator: Operator = _admin,
) -> HTMLResponse | RedirectResponse:
    if sub == operator.sub:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_OWN_LOGIN_UNBLOCK)
    if confirm == _CONFIRMED:
        try:
            await unblock_principal(operator, sub, shown=shown)
        except PrincipalBlockUnavailableError as exc:
            raise _unavailable() from exc
        except PrincipalNotBlockedError as exc:
            raise _not_blocked() from exc
        except BlockSetChangedError:
            pass
        else:
            return _back_to_page()
    blocks = [b for b in await _readable_blocks(operator) if b.sub == sub]
    if not blocks:
        raise _not_blocked()
    return await _confirm_page(
        request, session, operator, sub, blocks, changed=confirm == _CONFIRMED
    )


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
            200: {
                "description": (
                    "The confirm page: the form has no confirm=yes, or the blocks "
                    "of the login are not the blocks the confirm page showed."
                ),
                "content": _HTML,
            },
            403: {"description": "The login is the admin's own login.", "content": _JSON_DETAIL},
            404: {"description": "The login has no block you may remove.", "content": _JSON_DETAIL},
            503: {"description": "MEHO cannot read the block list now.", "content": _JSON_DETAIL},
        },
    )
    return router

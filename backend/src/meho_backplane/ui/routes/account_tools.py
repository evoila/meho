# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Account page: "Your tools", and "Sign out this tool".

Shown only while the ``MCP_CLIENT_PER_TOOL_KIND`` switch is on. With it
off, the account page has no such section and the route answers 404.

* :func:`tools_section` -- the rows of the section for the account page:
  each tool kind, its login client and its last use from the audit rows.
* ``POST /ui/account/tools/sign-out`` -- form field ``tool`` (a tool kind
  key such as ``claude-desktop``, never a client id). Ends the signed-in
  person's logins for that one client
  (:func:`~meho_backplane.auth.tool_sign_out.sign_out_tool`) and answers
  the section again with one result line. A login the tool shares with
  another tool is kept, and the line says so. The person comes from the
  session, never from the form, so a person can sign out only their own
  tools. An unknown key, or a kind without a sign-out button (the web
  console), answers 404.

A Keycloak failure answers the section with an error line; the audit row
of the failed try says what ended before it, if anything. The logic is in
:mod:`meho_backplane.auth.tool_sign_out`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.keycloak_admin import (
    KeycloakAdminError,
    KeycloakAdminNotConfiguredError,
    KeycloakConsentNotFoundError,
    KeycloakForbiddenError,
)
from meho_backplane.auth.tool_sign_out import (
    ToolKind,
    ToolSignOutIncompleteError,
    last_use_by_client,
    sign_out_tool,
    tool_kinds,
)
from meho_backplane.db.engine import get_session
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.templating import get_templates

__all__ = ["ToolRow", "build_account_tools_router", "tools_section"]

_log = structlog.get_logger(__name__)

_require_ui_session_dep = Depends(require_ui_session)
_get_session_dep = Depends(get_session)

#: Result lines after a sign-out. Kept here so the tests pin the words.
SIGNED_OUT_TEXT = (
    "{label} is signed out. It stops when its last access token ends: within "
    "the access token lifetime (about five minutes on MEHO's built-in "
    "Keycloak). If it signs in again, it gets a new login."
)
SHARED_KEPT_TEXT = (
    " {label} also shares one browser login with {others}. MEHO did not end "
    "that login, so {others} keep working. While it lasts, {label} can still "
    "get new access tokens through it. To stop that too, end the login in "
    "your Keycloak account page."
)
SHARED_ONLY_TEXT = (
    "{label} has no login of its own: it shares one browser login with "
    "{others}. MEHO did not end it, so {others} keep working. Nothing "
    "changed. To stop {label}, end that login in your Keycloak account page."
)
NOTHING_TO_SIGN_OUT_TEXT = "Keycloak has no login of {label} for you. Nothing changed."
NOT_CONFIGURED_TEXT = (
    "MEHO cannot sign out tools: the Keycloak admin client is not set up. "
    "Ask your MEHO admin. Nothing changed."
)
FAILED_TEXT = (
    "Keycloak could not sign out {label}. Try again later. If it keeps "
    "failing, ask your MEHO admin."
)
FORBIDDEN_TEXT = (
    "Keycloak refused to sign out {label}: MEHO's Keycloak admin client does "
    "not have the right manage-users. Ask your MEHO admin to give it that "
    "right. Nothing changed."
)
PARTLY_SIGNED_OUT_TEXT = (
    "{label} is only partly signed out: Keycloak ended some of its logins, "
    "then a call failed. {label} may still work. Try again later. If it "
    "keeps failing, ask your MEHO admin."
)


@dataclass(frozen=True)
class ToolRow:
    """Template-facing row of the "Your tools" section."""

    key: str
    label: str
    client_id: str
    can_sign_out: bool
    last_used_at: datetime | None


@dataclass(frozen=True)
class _Result:
    kind: Literal["success", "info", "error"]
    text: str


async def tools_section(
    db_session: AsyncSession, session_ctx: UISessionContext
) -> list[ToolRow] | None:
    """Return the rows of "Your tools", or ``None`` while the switch is off."""
    settings = get_settings()
    if not settings.mcp_client_per_tool_kind:
        return None
    kinds = tool_kinds(settings)
    last_use = await last_use_by_client(
        db_session,
        operator_sub=session_ctx.operator_sub,
        client_ids=[k.client_id for k in kinds],
        now=datetime.now(UTC),
    )
    return [
        ToolRow(
            key=k.key,
            label=k.label,
            client_id=k.client_id,
            can_sign_out=k.can_sign_out,
            last_used_at=last_use.get(k.client_id),
        )
        for k in kinds
    ]


def _find_kind(key: str) -> ToolKind:
    """Return the tool kind with a sign-out button named *key*, else 404."""
    settings = get_settings()
    if settings.mcp_client_per_tool_kind:
        for kind in tool_kinds(settings):
            if kind.key == key and kind.can_sign_out:
                return kind
    raise HTTPException(status_code=404, detail="Not Found")


def _labels(client_ids: tuple[str, ...]) -> str:
    """Name the tools of *client_ids*, for a result line."""
    names = {k.client_id: k.label for k in tool_kinds(get_settings())}
    return ", ".join(names.get(c, c) for c in client_ids)


async def _do_sign_out(session_ctx: UISessionContext, kind: ToolKind) -> _Result:
    """Run the sign-out and turn its outcome into one result line."""
    try:
        ended = await sign_out_tool(
            operator_sub=session_ctx.operator_sub,
            tenant_id=session_ctx.tenant_id,
            kind=kind,
            settings=get_settings(),
        )
    except KeycloakConsentNotFoundError:
        return _Result("info", NOTHING_TO_SIGN_OUT_TEXT.format(label=kind.label))
    except KeycloakAdminNotConfiguredError:
        return _Result("error", NOT_CONFIGURED_TEXT)
    except ToolSignOutIncompleteError:
        return _Result("error", PARTLY_SIGNED_OUT_TEXT.format(label=kind.label))
    except KeycloakForbiddenError:
        return _Result("error", FORBIDDEN_TEXT.format(label=kind.label))
    except KeycloakAdminError as exc:
        _log.warning(
            "ui_account_tool_sign_out_failed",
            operator_sub=session_ctx.operator_sub,
            client_id=kind.client_id,
            reason=type(exc).__name__,
        )
        return _Result("error", FAILED_TEXT.format(label=kind.label))
    others = _labels(ended.shared_with)
    if not ended.ended_any:
        return _Result("info", SHARED_ONLY_TEXT.format(label=kind.label, others=others))
    text = SIGNED_OUT_TEXT.format(label=kind.label)
    if ended.shared_with:
        text += SHARED_KEPT_TEXT.format(label=kind.label, others=others)
    return _Result("success", text)


def build_account_tools_router() -> APIRouter:
    """Construct the router of ``POST /ui/account/tools/sign-out``."""
    router = APIRouter(tags=["ui-account"])

    async def _sign_out(
        request: Request,
        tool: str = Form(default=""),
        session_ctx: UISessionContext = _require_ui_session_dep,
        db_session: AsyncSession = _get_session_dep,
    ) -> HTMLResponse:
        """``POST /ui/account/tools/sign-out`` -- the tool kind key is a form field."""
        kind = _find_kind(tool)
        result = await _do_sign_out(session_ctx, kind)
        context: dict[str, object] = {
            "tools": await tools_section(db_session, session_ctx),
            "tools_result": result,
            "now_utc": datetime.now(UTC),
        }
        return get_templates().TemplateResponse(request, "account/_tools.html", context)

    router.add_api_route(
        "/ui/account/tools/sign-out",
        _sign_out,
        methods=["POST"],
        name="ui_account_tool_sign_out",
        response_class=HTMLResponse,
        responses={
            404: {
                "description": (
                    "The MCP_CLIENT_PER_TOOL_KIND switch is off, or the tool "
                    "form field names no tool kind that can be signed out."
                ),
                "content": {"text/html": {}},
            },
        },
    )
    return router

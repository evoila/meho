# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The console's approval sign-in: ``/ui/auth/approval-sign-in`` (#6282).

An approval in the console needs the same sign-in level as one on the
command line. The console's normal login (``meho-web``) never gives that
level. So before an approve, the console sends the person to one more
sign-in with its own approval client
(:attr:`~meho_backplane.settings.Settings.ui_keycloak_approve_client_id`,
default ``meho-web-approve``). Keycloak binds that client to the approval
sign-in flow ``meho approval sign-in level``: MEHO's password form, then
the one-time code. The console's normal login does not change.

* ``GET /ui/auth/approval-sign-in?return_to=<path>`` needs a console
  session. It sends the browser to Keycloak with the approval client,
  PKCE, ``prompt=login``, ``max_age=0`` and ``acr_values=meho-approve``.
  The pending flow keeps the session id: the session cookie is
  ``SameSite=Strict``, so a browser leaves it out when a login server on
  another site sends the person back.
* ``GET /ui/auth/approval-callback`` checks the login-binding cookie and
  the ``state`` as the normal callback does, exchanges the code with the
  approval client, checks the token through the JWT chain, and keeps it on
  the session row (:mod:`meho_backplane.ui.auth.approval_token_store`) only
  when it belongs to the session's person and comes from the approval
  client. Then it sends the person back to ``return_to``.

The approve itself checks the kept token
(:func:`~meho_backplane.auth.web_approval_sign_in.check_web_approval_sign_in`).
No token is ever logged.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final
from urllib.parse import quote

import httpx
import httpx2
import structlog
from authlib.integrations.base_client.errors import MismatchingStateError, OAuthError
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from meho_backplane.auth.approval_sign_in import APPROVAL_LEVEL_ACR
from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.web_approval_sign_in import is_web_approval_sign_in
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.approval_token_store import store_approval_token
from meho_backplane.ui.auth.flow import (
    MISSING_CLIENT_SECRET_DETAIL,
    OAuthFlowConfigurationError,
    OAuthFlowError,
    _ensure_client_configured,
    _post_to_token_endpoint,
    _verify_browser_binding,
    build_authorization_request,
    get_verifier_store,
    resolve_oidc_endpoints,
)
from meho_backplane.ui.auth.routes import (
    AUTHORIZATION_STATE_EXPIRED_DETAIL,
    _raise_idp_error,
    _safe_return_to,
    clear_login_binding_cookie,
    login_binding_cookie_name,
    login_location,
    set_login_binding_cookie,
)
from meho_backplane.ui.auth.session_cookie import read_session_cookie
from meho_backplane.ui.auth.session_ref import session_ref
from meho_backplane.ui.auth.session_store import load_session

__all__ = [
    "APPROVAL_SIGN_IN_PATH",
    "NO_APPROVAL_CLIENT_DETAIL",
    "add_approval_sign_in_routes",
    "approval_sign_in_location",
]

#: The console's approval sign-in route.
APPROVAL_SIGN_IN_PATH: Final[str] = "/ui/auth/approval-sign-in"

#: The approval sign-in's callback. The console approval client must list
#: ``<backplane_url>`` plus this path as its redirect URI.
_CALLBACK_PATH: Final[str] = "/ui/auth/approval-callback"

#: The answer when the console has no approval client.
NO_APPROVAL_CLIENT_DETAIL: Final[str] = (
    "ui_approval_sign_in_not_configured: UI_KEYCLOAK_APPROVE_CLIENT_ID is empty, "
    "so the console cannot start an approval sign-in."
)

#: The answer when the approval sign-in is another person than the session.
_OTHER_PERSON_DETAIL: Final[str] = (
    "approval_sign_in_other_person: sign in for the approval as the person "
    "who is signed in to the console."
)


def approval_sign_in_location(return_to: str) -> str:
    """Build ``/ui/auth/approval-sign-in?return_to=<path>``."""
    return f"{APPROVAL_SIGN_IN_PATH}?return_to={quote(return_to, safe='')}"


def _callback_uri() -> str:
    return f"{get_settings().backplane_url.rstrip('/')}{_CALLBACK_PATH}"


async def _session_id(request: Request) -> uuid.UUID | None:
    """The id of the request's active console session, or ``None``."""
    raw = read_session_cookie(request.scope)
    if not raw:
        return None
    try:
        cookie_id = uuid.UUID(raw)
    except ValueError:
        return None
    async with get_sessionmaker()() as db_session:
        found = await load_session(db_session, cookie_id)
        await db_session.commit()
    return found.id if found is not None else None


async def _handle_approval_sign_in(
    request: Request, return_to: Annotated[str | None, Query()] = None
) -> RedirectResponse:
    """Send the browser to the approval sign-in of the console's approval client."""
    log = structlog.get_logger(__name__)
    target = _safe_return_to(return_to)
    session_id = await _session_id(request)
    if session_id is None:
        return RedirectResponse(url=login_location(target), status_code=status.HTTP_302_FOUND)
    client_id = get_settings().ui_keycloak_approve_client_id
    if not client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=NO_APPROVAL_CLIENT_DETAIL
        )
    try:
        url, state, browser_binding = await build_authorization_request(
            redirect_uri=_callback_uri(),
            return_to=target,
            fresh_sign_in=True,
            client_id=client_id,
            acr_values=APPROVAL_LEVEL_ACR,
            session_id=session_id,
        )
    except OAuthFlowConfigurationError:
        log.warning("ui_auth_oauth_not_configured", route="approval-sign-in")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=MISSING_CLIENT_SECRET_DETAIL
        ) from None
    except httpx.HTTPError as exc:
        log.warning(
            "ui_auth_discovery_failed", route="approval-sign-in", error_class=type(exc).__name__
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="upstream_auth_provider_unreachable"
        ) from exc
    log.info("ui_approval_sign_in_redirect", state=state, session_ref=session_ref(session_id))
    response = RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
    set_login_binding_cookie(response, state, browser_binding)
    return response


async def _exchange(
    request: Request, state: str | None, browser_binding: str | None
) -> tuple[str, uuid.UUID, str]:
    """Exchange the code with the approval client: ``(access token, session id, return_to)``.

    Every failure of the flow (no or unknown ``state``, a wrong browser,
    an answer without a token) is the same recoverable ``400`` as on the
    normal callback; the cause goes to the log only.
    """
    log = structlog.get_logger(__name__)
    settings = get_settings()
    try:
        _ensure_client_configured(settings)
        endpoints = await resolve_oidc_endpoints()
        pending = await get_verifier_store().pop(state) if state else None
        if pending is None or pending.session_id is None:
            raise OAuthFlowError("unknown_or_expired_state")
        _verify_browser_binding(pending, browser_binding)
        token = await _post_to_token_endpoint(
            settings,
            endpoints,
            redirect_uri=_callback_uri(),
            authorization_response=str(request.url),
            state=state or "",
            pending=pending,
            client_id=settings.ui_keycloak_approve_client_id,
        )
    except OAuthFlowConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=MISSING_CLIENT_SECRET_DETAIL
        ) from None
    except (OAuthFlowError, MismatchingStateError, OAuthError) as exc:
        log.warning(
            "ui_approval_sign_in_callback_rejected", error_class=type(exc).__name__, state=state
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=AUTHORIZATION_STATE_EXPIRED_DETAIL
        ) from exc
    except (httpx.HTTPError, httpx2.HTTPError) as exc:
        log.warning("ui_auth_token_endpoint_unreachable", error_class=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="upstream_auth_provider_unreachable"
        ) from exc
    access = token.get("access_token")
    if not isinstance(access, str) or not access:
        log.warning("ui_approval_sign_in_callback_rejected", error_class="missing_access_token")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=AUTHORIZATION_STATE_EXPIRED_DETAIL
        )
    return access, pending.session_id, pending.return_to


async def _handle_approval_callback(
    request: Request,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """Keep the approval sign-in on the session and send the person back."""
    del code  # authlib reads it from the full callback URL.
    if error:
        _raise_idp_error(error, error_description)
    browser_binding = request.cookies.get(login_binding_cookie_name(state)) if state else None
    access, session_id, return_to = await _exchange(request, state, browser_binding)
    settings = get_settings()
    operator = await verify_jwt_for_audience(
        f"Bearer {access}", expected_audience=settings.keycloak_audience, allow_unchosen=True
    )
    if not is_web_approval_sign_in(operator, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_OTHER_PERSON_DETAIL)
    async with get_sessionmaker()() as db_session, db_session.begin():
        found = await load_session(db_session, session_id)
        if found is None or found.operator_sub != operator.sub:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_OTHER_PERSON_DETAIL)
        if not await store_approval_token(db_session, session_id, access):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_OTHER_PERSON_DETAIL)
    structlog.get_logger(__name__).info(
        "ui_approval_sign_in_kept",
        operator_sub=operator.sub,
        acr=operator.acr,
        session_ref=session_ref(session_id),
    )
    response = RedirectResponse(url=return_to, status_code=status.HTTP_302_FOUND)
    if state:
        clear_login_binding_cookie(response, state)
    return response


def add_approval_sign_in_routes(router: APIRouter) -> None:
    """Add the two approval sign-in routes to the ``/ui/auth`` router."""
    for path, handler, name in (
        ("/approval-sign-in", _handle_approval_sign_in, "ui_auth_approval_sign_in"),
        ("/approval-callback", _handle_approval_callback, "ui_auth_approval_callback"),
    ):
        router.add_api_route(
            path,
            handler,
            methods=["GET"],
            name=name,
            response_class=RedirectResponse,
            status_code=status.HTTP_302_FOUND,
        )

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Is the approval sign-in in place? The state ``/api/v1/health`` shows (#6280).

On the command line and REST, only a token of the approval client
(:attr:`~meho_backplane.settings.Settings.keycloak_cli_approve_client_id`,
default ``meho-cli-approve``) approves a request
(:mod:`meho_backplane.auth.approval_sign_in`). That client must sign in
through its own login flow ``meho approval sign-in``: the password form, then
the one-time code when the person has set one up. No other step.

:func:`read_approval_sign_in` reads the realm with the backplane's Keycloak
admin client and says one of:

* ``ok``: the flow has exactly the steps of the realm file, and the client's
  browser sign-in is bound to it;
* ``flow_missing``: the realm has no flow ``meho approval sign-in``;
* ``flow_differs``: the flow has other steps; ``detail`` names the first
  difference;
* ``not_bound``: the flow is right, but the client is missing, has no own
  browser flow, or is bound to another flow;
* ``not_checked``: the check could not run; ``detail`` is a short reason
  word: ``keycloak_admin_not_configured``, ``no_right_to_read_login_flows``,
  ``no_right_to_read_clients``, ``keycloak_timeout``,
  ``keycloak_unexpected_answer`` or ``keycloak_unreachable``.

A failed read is never ``ok``. Reading login flows needs ``view-realm`` or
``manage-realm`` on the admin client; the built-in realm gives
``meho-admin`` ``manage-realm``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal
from urllib.parse import quote

import structlog
from pydantic import BaseModel, ConfigDict

from meho_backplane.auth.keycloak_admin import (
    KeycloakAdminClient,
    KeycloakAdminError,
    KeycloakAdminNotConfiguredError,
)
from meho_backplane.connectors.keycloak.ops_authflow import (
    APPROVAL_FLOW_ALIAS,
    approval_flow_difference,
)
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    KeycloakUnexpectedResponseError,
)
from meho_backplane.settings import Settings, get_settings

__all__ = ["ApprovalSignInState", "ApprovalSignInStatus", "read_approval_sign_in"]

ApprovalSignInState = Literal["ok", "flow_missing", "flow_differs", "not_bound", "not_checked"]

#: The whole check gives up after this many seconds, so a slow Keycloak never
#: holds up the health answer for long.
_READ_TIMEOUT_S = 5.0


class ApprovalSignInStatus(BaseModel):
    """The state of the approval sign-in flow and its binding (#6280)."""

    model_config = ConfigDict(frozen=True)

    state: ApprovalSignInState
    flow: str = APPROVAL_FLOW_ALIAS
    client: str
    detail: str | None = None


class _NoRightError(Exception):
    """The admin client may not read what the check needs; the arg is the reason word."""


async def _read_list(kc: KeycloakAdminClient, path: str, **params: str) -> list[Any]:
    status, body = await kc.read(path, params or None)
    if status == 403:
        what = "login_flows" if path.startswith("/authentication/") else "clients"
        raise _NoRightError(f"no_right_to_read_{what}")
    if status != 200 or not isinstance(body, list):
        raise KeycloakUnexpectedResponseError(f"GET {path} answered HTTP {status}")
    return body


async def _state(kc: KeycloakAdminClient, client_id: str) -> tuple[ApprovalSignInState, str | None]:
    flows = await _read_list(kc, "/authentication/flows")
    flow_id = next(
        (
            f.get("id")
            for f in flows
            if isinstance(f, dict) and f.get("alias") == APPROVAL_FLOW_ALIAS
        ),
        None,
    )
    if not flow_id:
        return "flow_missing", None
    steps = await _read_list(
        kc, f"/authentication/flows/{quote(APPROVAL_FLOW_ALIAS, safe='')}/executions"
    )
    difference = approval_flow_difference(steps)
    if difference is not None:
        return "flow_differs", difference
    clients = await _read_list(kc, "/clients", clientId=client_id)
    client = next(
        (c for c in clients if isinstance(c, dict) and c.get("clientId") == client_id), None
    )
    if client is None:
        return "not_bound", f"the client {client_id} does not exist"
    bound = (client.get("authenticationFlowBindingOverrides") or {}).get("browser")
    if not bound:
        return "not_bound", f"the client {client_id} has no own browser flow"
    if bound != flow_id:
        return "not_bound", f"the client {client_id} is bound to another flow"
    return "ok", None


async def read_approval_sign_in(settings: Settings | None = None) -> ApprovalSignInStatus:
    """Read the approval sign-in flow and its binding; never raises."""
    settings = settings or get_settings()
    client_id = settings.keycloak_cli_approve_client_id

    def not_checked(reason: str) -> ApprovalSignInStatus:
        return ApprovalSignInStatus(state="not_checked", client=client_id, detail=reason)

    try:
        kc = KeycloakAdminClient.from_settings(settings)
    except KeycloakAdminNotConfiguredError:
        return not_checked("keycloak_admin_not_configured")
    try:
        async with asyncio.timeout(_READ_TIMEOUT_S), kc:
            state, detail = await _state(kc, client_id)
    except _NoRightError as exc:
        return not_checked(str(exc))
    except TimeoutError:
        return not_checked("keycloak_timeout")
    except KeycloakUnexpectedResponseError:
        return not_checked("keycloak_unexpected_answer")
    except KeycloakAdminError as exc:
        structlog.get_logger(__name__).warning(
            "approval_sign_in_check_failed", error_class=type(exc).__name__
        )
        return not_checked("keycloak_unreachable")
    return ApprovalSignInStatus(state=state, client=client_id, detail=detail)

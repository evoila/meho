# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Is the approval sign-in in place? The state ``/api/v1/health`` shows (#6280).

On the command line and REST, only a token of the approval client
(:attr:`~meho_backplane.settings.Settings.keycloak_cli_approve_client_id`,
default ``meho-cli-approve``) approves a request
(:mod:`meho_backplane.auth.approval_sign_in`). That client must sign in
through its own login flow ``meho approval sign-in level``: the level step
(approval level 2, ``meho-approve``, asked for at every sign-in), the password
form, then the one-time code when the person has set one up. No other step.

:func:`read_approval_sign_in` reads the realm with the backplane's Keycloak
admin client and says one of:

* ``ok``: the realm really gives the approval level: the flow has exactly
  the steps of the realm file, its level step has the settings of the file
  (level 2, maximum age 0), the realm's ``acr.loa.map`` names level 2
  ``meho-approve`` and no other name has level 2, and the client's browser
  sign-in is bound to the flow;
* ``flow_missing``: the realm has no flow ``meho approval sign-in level``;
* ``flow_differs``: the flow has other steps, its level step has no or other
  settings, the realm's ``acr.loa.map`` does not name level 2
  ``meho-approve``, or the client has its own ``acr.loa.map``; ``detail``
  names the first difference;
* ``not_bound``: the flow is right, but the client is missing, has no own
  browser flow, or is bound to another flow;
* ``other_grant_on``: the client also allows direct grant (password sent by
  the program) or device grant; such a sign-in skips the approval flow;
* ``not_checked``: the check could not run; ``detail`` is a short reason
  word: ``keycloak_admin_not_configured``, ``no_right_to_read_login_flows``,
  ``no_right_to_read_realm``, ``no_right_to_read_clients``,
  ``keycloak_timeout``, ``keycloak_unexpected_answer`` or
  ``keycloak_unreachable``.

``APPROVAL_SIGN_IN_LEVEL=auto`` (the default) requires the approval level
only when the state is ``ok`` (:func:`sign_in_level_in_force`).

A failed read is never ``ok``. The answer of a read is kept for
:data:`_KEEP_S` seconds, so ``/api/v1/health`` and ``meho_status``, which
are called often, do not log in to Keycloak on every call.

Reading login flows and the realm needs ``view-realm`` or ``manage-realm``
on the admin client; the built-in realm gives ``meho-admin`` ``manage-realm``.
"""

from __future__ import annotations

import asyncio
import time
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
    APPROVAL_LEVEL_CONFIG,
    APPROVAL_LEVEL_STEP,
    approval_flow_difference,
)
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    KeycloakUnexpectedResponseError,
)
from meho_backplane.core.keycloak_levels import (
    APPROVAL_LEVEL,
    APPROVAL_LEVEL_NAME,
    LEVEL_MAP,
    client_level_map,
    level_conflict,
    parse_level_map,
)
from meho_backplane.settings import Settings, get_settings

__all__ = [
    "ApprovalSignInState",
    "ApprovalSignInStatus",
    "SignInLevelInForce",
    "read_approval_sign_in",
    "sign_in_level_for_state",
    "sign_in_level_in_force",
    "sign_in_level_warning",
]

ApprovalSignInState = Literal[
    "ok", "flow_missing", "flow_differs", "not_bound", "other_grant_on", "not_checked"
]

#: What MEHO does with a token of the approval client that lacks the level.
SignInLevelInForce = Literal["off", "observe", "required"]

#: The client attribute that turns on the device grant.
_DEVICE_GRANT = "oauth2.device.authorization.grant.enabled"

#: The whole check gives up after this many seconds, so a slow Keycloak never
#: holds up the health answer for long.
_READ_TIMEOUT_S = 5.0

#: A read answer is kept this many seconds (per admin URL and client).
_KEEP_S = 60.0


class ApprovalSignInStatus(BaseModel):
    """The state of the approval sign-in flow and its binding (#6280)."""

    model_config = ConfigDict(frozen=True)

    state: ApprovalSignInState
    flow: str = APPROVAL_FLOW_ALIAS
    client: str
    detail: str | None = None


#: The kept answers: (admin URL, client) -> (when read, in monotonic seconds; the answer).
_kept: dict[tuple[str, str], tuple[float, ApprovalSignInStatus]] = {}


class _NoRightError(Exception):
    """The admin client may not read what the check needs; the arg is the reason word."""


async def _read_json(kc: KeycloakAdminClient, path: str, want: type, **params: str) -> Any:
    status, body = await kc.read(path, params or None)
    if status == 403:
        if path.startswith("/authentication/"):
            what = "login_flows"
        elif path.startswith("/clients"):
            what = "clients"
        else:
            what = "realm"
        raise _NoRightError(f"no_right_to_read_{what}")
    if status != 200 or not isinstance(body, want):
        raise KeycloakUnexpectedResponseError(f"GET {path} answered HTTP {status}")
    return body


def _object(value: Any) -> dict[str, Any]:
    """*value* when it is a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


async def _read_list(kc: KeycloakAdminClient, path: str, **params: str) -> list[Any]:
    body: list[Any] = await _read_json(kc, path, list, **params)
    return body


async def _level_step_difference(kc: KeycloakAdminClient, steps: list[Any]) -> str | None:
    """Whether the level step has the settings of the realm file (level 2, maximum age 0)."""
    step = next(
        s for s in steps if isinstance(s, dict) and s.get("providerId") == APPROVAL_LEVEL_STEP
    )
    config_id = step.get("authenticationConfig")
    if not config_id:
        return "the level step has no settings"
    have = await _read_json(kc, f"/authentication/config/{quote(str(config_id), safe='')}", dict)
    config = _object(have.get("config"))
    if any(config.get(key) != value for key, value in APPROVAL_LEVEL_CONFIG.items()):
        return f"the level step has the settings {config!r}; wanted {dict(APPROVAL_LEVEL_CONFIG)!r}"
    return None


async def _level_map_difference(kc: KeycloakAdminClient) -> str | None:
    """Whether the realm's ``acr.loa.map`` names level 2 ``meho-approve``, and only it."""
    realm = await _read_json(kc, "", dict)
    attributes = _object(realm.get("attributes"))
    levels = parse_level_map(attributes.get(LEVEL_MAP))
    if levels is None:
        return f"the realm attribute {LEVEL_MAP} is not a JSON object"
    if str(levels.get(APPROVAL_LEVEL_NAME)) != str(APPROVAL_LEVEL):
        return (
            f"the realm attribute {LEVEL_MAP} does not name level {APPROVAL_LEVEL} "
            f"{APPROVAL_LEVEL_NAME}"
        )
    return level_conflict(levels, {APPROVAL_LEVEL_NAME: APPROVAL_LEVEL})


def _client_state(
    client: dict[str, Any], client_id: str, flow_id: str
) -> tuple[ApprovalSignInState, str | None]:
    """The state of the client: its own level map, its grants, its binding."""
    own_map = client_level_map(client)
    if own_map is not None:
        return "flow_differs", own_map
    attributes = _object(client.get("attributes"))
    if client.get("directAccessGrantsEnabled") is True:
        return "other_grant_on", f"the client {client_id} allows direct grant"
    if str(attributes.get(_DEVICE_GRANT)).lower() == "true":
        return "other_grant_on", f"the client {client_id} allows device grant"
    bound = (client.get("authenticationFlowBindingOverrides") or {}).get("browser")
    if not bound:
        return "not_bound", f"the client {client_id} has no own browser flow"
    if bound != flow_id:
        return "not_bound", f"the client {client_id} is bound to another flow"
    return "ok", None


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
    if difference is None:
        difference = await _level_step_difference(kc, steps)
    if difference is None:
        difference = await _level_map_difference(kc)
    if difference is not None:
        return "flow_differs", difference
    clients = await _read_list(kc, "/clients", clientId=client_id)
    client = next(
        (c for c in clients if isinstance(c, dict) and c.get("clientId") == client_id), None
    )
    if client is None:
        return "not_bound", f"the client {client_id} does not exist"
    return _client_state(client, client_id, flow_id)


async def read_approval_sign_in(settings: Settings | None = None) -> ApprovalSignInStatus:
    """The approval sign-in flow and its binding, read at most every :data:`_KEEP_S` s."""
    settings = settings or get_settings()
    key = (settings.keycloak_admin_url, settings.keycloak_cli_approve_client_id)
    kept = _kept.get(key)
    if kept is not None and time.monotonic() - kept[0] < _KEEP_S:
        return kept[1]
    status = await _read(settings)
    _kept[key] = (time.monotonic(), status)
    return status


async def _read(settings: Settings) -> ApprovalSignInStatus:
    """Read the approval sign-in flow and its binding; never raises."""
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


def sign_in_level_for_state(mode: str, state: ApprovalSignInState | None) -> SignInLevelInForce:
    """What MEHO does for *mode* (the setting) when the approval sign-in is in *state*."""
    if mode == "off":
        return "off"
    if mode == "observe" or (mode == "auto" and state != "ok"):
        return "observe"
    return "required"


async def sign_in_level_in_force(settings: Settings) -> SignInLevelInForce:
    """What MEHO does now with a token of the approval client that lacks the level (#6281).

    ``off``, ``observe`` and ``required`` are the setting itself. ``auto``
    reads the approval sign-in (:func:`read_approval_sign_in`, kept for a
    short time): ``required`` when its state is ``ok``, else ``observe``.
    """
    mode = settings.approval_sign_in_level
    if mode != "auto":
        return sign_in_level_for_state(mode, None)
    return sign_in_level_for_state(mode, (await read_approval_sign_in(settings)).state)


def sign_in_level_warning(mode: str, state: ApprovalSignInState | None) -> str | None:
    """The health warning when MEHO does not require the approval level; else ``None``."""
    if sign_in_level_for_state(mode, state) == "required":
        return None
    if mode == "auto":
        return (
            "MEHO does not require the approval level: APPROVAL_SIGN_IN_LEVEL is auto and "
            f"approvals.sign_in is {state}, not ok. Fix the approval sign-in in Keycloak."
        )
    return (
        f"MEHO does not require the approval level: APPROVAL_SIGN_IN_LEVEL is {mode}. "
        "Set it to auto or required."
    )

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Test one Keycloak login: ``keycloak.user.check_login``.

After a password change an operator must prove two things: the new password
works, and the old one does not. This op tries one login for one user and
answers only ``accepted``, ``refused`` or ``error``.

How it works
============

1. The password is read from Vault (``password_secret_ref``, with the same
   ``password_secret_mount`` / ``password_secret_key`` params as
   ``keycloak.user.reset_password``). It is never an op param.
2. One ``POST /realms/{realm}/protocol/openid-connect/token`` with the
   ``password`` grant. No retry: every failed try counts toward Keycloak's
   lockout for too many failed logins.
3. On success, the new session is ended at once with
   ``POST /realms/{realm}/protocol/openid-connect/logout`` and the refresh
   token, so no session is left behind.
4. The answer holds ``result``, ``http_status`` and Keycloak's ``error`` /
   ``error_description``. Never the password, never a token.

Keycloak answers a wrong password with 401 ``invalid_grant`` and a disabled
user with 400 ``invalid_grant``; both are ``refused``. Any other answer (for
example 400 ``unauthorized_client`` when the client has no direct access
grants, or a 5xx) is ``error``.

Why ``caution`` with approval
=============================

The op reads nothing secret back, but it writes a login event in Keycloak,
and a refused try raises the user's failed-login count. So it is not
``safe``, and every op that is not ``safe`` needs approval.

The op id is pinned in
:data:`~meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS`: the token
request body carries the password and the token answer carries tokens, so
the flight recorder never stores either body, and the broadcast view and a
waiting approval keep only an aggregate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import structlog

from meho_backplane.connectors.keycloak._paths import _LOGOUT_PATH, _TOKEN_PATH, fill_path
from meho_backplane.connectors.keycloak.ops_read import KeycloakOp
from meho_backplane.connectors.keycloak.ops_write import _read_password_from_vault
from meho_backplane.connectors.keycloak.ops_write_preview import _managed_realm, _opt_str
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "CHECK_LOGIN_OP_ID",
    "LOGIN_CHECK_OPS",
    "WHEN_TO_USE_LOGIN_CHECK_BY_GROUP",
    "keycloak_user_check_login",
]

_log = structlog.get_logger(__name__)

CHECK_LOGIN_OP_ID = "keycloak.user.check_login"

#: ``admin-cli`` is public and has direct access grants on by default.
_DEFAULT_CLIENT_ID = "admin-cli"

#: Keycloak's descriptions are short; the cap guards against a very long body.
_MAX_ERROR_DESCRIPTION_CHARS = 200

_FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
}

_WHEN_TO_USE_LOGIN_CHECK = (
    "Use to test whether one user can log in with a password stored in Vault "
    "(``keycloak.user.check_login``), for example after "
    "``keycloak.user.reset_password``: the new password must be accepted and "
    "the old one refused. It makes one login try, ends the new session at "
    "once, and answers only accepted / refused / error. It never returns the "
    "password or a token. Each try writes a login event, and a refused try "
    "counts toward the user's lockout, so a second person must approve. The "
    "client must allow direct access grants (``admin-cli`` does by default)."
)

#: Group text for the op's group, merged into the connector's map.
WHEN_TO_USE_LOGIN_CHECK_BY_GROUP: dict[str, str] = {"user_login_check": _WHEN_TO_USE_LOGIN_CHECK}

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": {
            "type": "string",
            "minLength": 1,
            "description": "The username to log in as.",
        },
        "realm": {
            "type": "string",
            "minLength": 1,
            "description": "Realm to log in to. Default: the target's managed realm.",
        },
        "client_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Public client used for the login; it must allow direct access "
                "grants. Default 'admin-cli'."
            ),
        },
        "password_secret_ref": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Vault KV-v2 path the password is read from under your identity. "
                "The password is never passed inline."
            ),
        },
        "password_secret_mount": {
            "type": "string",
            "description": "Vault KV-v2 mount point (default 'secret').",
        },
        "password_secret_key": {
            "type": "string",
            "description": "Field within the Vault secret payload (default 'password').",
        },
    },
    "required": ["username", "password_secret_ref"],
    "additionalProperties": False,
    "examples": [
        {
            "realm": "master",
            "username": "admin",
            "password_secret_ref": "keycloak/master/admin-old",
        }
    ],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "result": {
            "type": "string",
            "enum": ["accepted", "refused", "error"],
            "description": "accepted: login worked. refused: Keycloak said no. error: other.",
        },
        "http_status": {"type": "integer", "description": "HTTP status of the token request."},
        "error": {
            "type": ["string", "null"],
            "description": "Keycloak's error code, for example 'invalid_grant'.",
        },
        "error_description": {
            "type": ["string", "null"],
            "description": "Keycloak's error text, for example 'Invalid user credentials'.",
        },
        "session_ended": {
            "type": ["boolean", "null"],
            "description": "After an accepted login: was the new session ended. Else null.",
        },
        "realm": {"type": "string", "description": "Realm the login was tried in."},
        "username": {"type": "string", "description": "Username the login was tried for."},
        "client_id": {"type": "string", "description": "Client used for the login."},
    },
    "required": ["result", "http_status"],
}

LOGIN_CHECK_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id=CHECK_LOGIN_OP_ID,
        handler_attr="user_check_login",
        summary="Test one login with a password from Vault; answer only accepted or refused.",
        description=(
            "One password-grant POST to the realm's token endpoint, no retry. "
            "An accepted login's session is ended at once. Returns result, "
            "HTTP status and Keycloak's error code. Never the password or a "
            "token. A refused try counts toward lockout."
        ),
        parameter_schema=_PARAMETER_SCHEMA,
        response_schema=_RESPONSE_SCHEMA,
        group_key="user_login_check",
        tags=("user", "login", "keycloak"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "After a password change, to prove the new password is accepted "
                "and the old one is refused. Or to check that a user can log in."
            ),
            "preconditions": (
                "Store the password to test in Vault first and pass its path in "
                "password_secret_ref; never pass a password inline. The client "
                "(default admin-cli) must allow direct access grants. Each try "
                "writes a login event, and a refused try raises the user's "
                "failed-login count, so do not repeat it in a loop."
            ),
            "parameter_hints": {
                "username": "The user to test, for example 'admin'.",
                "realm": "Realm of the user, for example 'master'. Default: managed realm.",
                "password_secret_ref": "Vault KV-v2 path to the password. Never inline.",
            },
            "output_shape": (
                "``{result, http_status, error, error_description, session_ended, "
                "realm, username, client_id}``. No password, no token."
            ),
            "next_step": (
                "refused with invalid_grant and 'Invalid user credentials' means a "
                "wrong password; 'Account disabled' means the user is disabled. "
                "error with unauthorized_client means the client has no direct "
                "access grants: pick another client_id."
            ),
        },
    ),
)


def _error_fields(response: httpx.Response, password: str) -> tuple[str | None, str | None]:
    """Return Keycloak's ``(error, error_description)`` from an OAuth2 error body.

    Only the two string fields are read. The description is cut to a short
    length, and the password is masked should a proxy ever echo it.
    """
    try:
        body = response.json()
    except ValueError:
        return None, None
    if not isinstance(body, dict):
        return None, None
    error = body.get("error")
    description = body.get("error_description")
    error_text = error if isinstance(error, str) and error else None
    description_text = None
    if isinstance(description, str) and description:
        description_text = description.replace(password, "***")[:_MAX_ERROR_DESCRIPTION_CHARS]
    return error_text, description_text


async def _end_session(
    self: KeycloakConnector,
    target: KeycloakTargetLike,
    *,
    logout_path: str,
    client_id: str,
    payload: Any,
) -> bool:
    """End the session an accepted login opened. ``True`` when Keycloak confirms."""
    refresh_token = payload.get("refresh_token") if isinstance(payload, dict) else None
    if not isinstance(refresh_token, str) or not refresh_token:
        return False
    client = await self._http_client(target)
    try:
        resp = await client.post(
            logout_path,
            data={"client_id": client_id, "refresh_token": refresh_token},
            headers=_FORM_HEADERS,
            extensions=self._request_extensions(target),
        )
    except httpx.HTTPError as exc:
        _log.warning(
            "keycloak_check_login_logout_failed", target=target.name, error=type(exc).__name__
        )
        return False
    return resp.is_success


async def keycloak_user_check_login(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Try one login and answer ``accepted`` / ``refused`` / ``error``.

    Op-id: ``keycloak.user.check_login``. The password comes from Vault and
    goes only into the token request body. The answer never carries the
    password or a token.
    """
    realm = _opt_str(params.get("realm")) or resolve_realm_config(target).managed_realm
    username = str(params["username"]).strip()
    client_id = _opt_str(params.get("client_id")) or _DEFAULT_CLIENT_ID
    password = await _read_password_from_vault(operator, target, params)

    realm_segment = quote_segment(realm)
    client = await self._http_client(target)
    resp = await client.post(
        fill_path(_TOKEN_PATH, {"realm": realm_segment}),
        data={
            "grant_type": "password",
            "client_id": client_id,
            "username": username,
            "password": password,
        },
        headers=_FORM_HEADERS,
        extensions=self._request_extensions(target),
    )
    answer: dict[str, Any] = {
        "result": "error",
        "http_status": resp.status_code,
        "error": None,
        "error_description": None,
        "session_ended": None,
        "realm": realm,
        "username": username,
        "client_id": client_id,
    }
    if resp.status_code == 200:
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and payload.get("access_token"):
            answer["result"] = "accepted"
            answer["session_ended"] = await _end_session(
                self,
                target,
                logout_path=fill_path(_LOGOUT_PATH, {"realm": realm_segment}),
                client_id=client_id,
                payload=payload,
            )
        return answer
    error, description = _error_fields(resp, password)
    answer["error"] = error
    answer["error_description"] = description
    if error == "invalid_grant":
        answer["result"] = "refused"
    return answer


async def _user_check_login_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Card for ``keycloak.user.check_login``: which user and where the password comes from."""
    preview: dict[str, Any] = {
        "resource": "user_login_check",
        "username": _opt_str(ctx.params.get("username")),
        "realm": _managed_realm(ctx),
        "client_id": _opt_str(ctx.params.get("client_id")) or _DEFAULT_CLIENT_ID,
        "password_source": "vault",
        "password_secret_ref": _opt_str(ctx.params.get("password_secret_ref")),
    }
    for key in ("password_secret_mount", "password_secret_key"):
        value = _opt_str(ctx.params.get(key))
        if value is not None:
            preview[key] = value
    return preview


register_preview_builder(CHECK_LOGIN_OP_ID, _user_check_login_preview)

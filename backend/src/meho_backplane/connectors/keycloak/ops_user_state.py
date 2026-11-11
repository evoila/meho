# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read one user's state: ``keycloak.user.get`` and ``keycloak.user.sessions``.

After ``keycloak.user.logout`` or ``keycloak.user.reset_password`` an operator
wants to check that it worked. These two safe reads show the user's state
afterwards:

* ``keycloak.user.get``: is the account on, its required actions, its "not
  before" time, its creation time, and the type and date of each stored
  credential (for example when the password was last set).
* ``keycloak.user.sessions``: how many live sessions the user has, and when
  each one started and was last used.

Both take ``id`` (the user's UUID) or ``username`` (an exact match, not the
"contains" search of ``keycloak.user.list``).

No secret and no session id: only the named fields are copied out of what
Keycloak answers (a list of allowed fields, not a list of blocked ones). A
credential is returned as ``type`` and ``createdDate`` only: never
``secretData``, ``credentialData`` or ``value``. A session is returned as
start time, last access time, IP address and client names: never a session id
or a token. A read that fails, or a body of the wrong shape, is an error; it
is never read as "no sessions" or "no credentials".

Keycloak gives ``notBefore`` in seconds and the other times in milliseconds.
All times are returned as ISO 8601 in UTC.

The credential list is named ``credentialDates``: MEHO hides every answer field
named ``credentials``, so that name would never reach the caller.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors.keycloak._paths import (
    _USER_CREDENTIALS_PATH,
    _USER_PATH,
    _USER_SESSIONS_PATH,
    _USERS_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_authflow import get_admin_list_strict
from meho_backplane.connectors.keycloak.ops_read import _UUID_PATTERN, KeycloakOp
from meho_backplane.connectors.keycloak.ops_write import KeycloakUserNotFoundError, _opt_str
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    KeycloakUnexpectedResponseError,
    get_or_none,
)
from meho_backplane.connectors.keycloak.redaction import redact_secret_fields
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "USER_STATE_OPS",
    "keycloak_user_get",
    "keycloak_user_sessions",
]

_GET_OP = "keycloak.user.get"
_SESSIONS_OP = "keycloak.user.sessions"


def _iso(value: Any, *, seconds: bool = False) -> str | None:
    """A Keycloak time as ISO 8601 in UTC; ``None`` when absent, zero or not a number.

    Keycloak gives ``notBefore`` in seconds and every other time in
    milliseconds. A ``notBefore`` of 0 means "never set".
    """
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        moment = datetime.fromtimestamp(value if seconds else value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.isoformat().replace("+00:00", "Z")


async def _user_id(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    params: dict[str, Any],
    op_id: str,
) -> str:
    """The user's UUID from ``id``, or from an exact ``username`` match."""
    user_id = _opt_str(params.get("id"))
    if user_id is not None:
        return user_id
    username = _opt_str(params.get("username"))
    if username is None:
        raise ValueError(f"{op_id} requires either 'id' (UUID) or 'username'")
    rows = await get_admin_list_strict(
        connector,
        target,
        fill_path(_USERS_PATH, {"realm": quote_segment(realm)}),
        operator=operator,
        params={"username": username, "exact": "true"},
    )
    wanted = username.casefold()
    for row in rows:
        if isinstance(row, dict) and str(row.get("username", "")).casefold() == wanted:
            found = row.get("id")
            if isinstance(found, str) and found:
                return found
    raise KeycloakUserNotFoundError(f"{op_id}: user not found: username={username!r}")


def _not_found(op_id: str, user_id: str) -> KeycloakUserNotFoundError:
    return KeycloakUserNotFoundError(f"{op_id}: user not found: id={user_id!r}")


def _credential_row(row: Any) -> dict[str, Any]:
    """Only ``type`` and ``createdDate`` of one stored credential."""
    if not isinstance(row, dict):
        raise KeycloakUnexpectedResponseError("a stored credential is not a JSON object")
    return {"type": row.get("type"), "createdDate": _iso(row.get("createdDate"))}


async def keycloak_user_get(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return one user's state and the type and date of each credential."""
    realm = resolve_realm_config(target).managed_realm
    realm_segment = quote_segment(realm)
    user_id = await _user_id(self, operator, target, realm, params, _GET_OP)
    segments = {"realm": realm_segment, "user-id": quote_segment(user_id)}
    rep = await get_or_none(self, target, fill_path(_USER_PATH, segments), operator)
    if rep is None:
        raise _not_found(_GET_OP, user_id)
    rep = redact_secret_fields(rep)
    try:
        credentials = await get_admin_list_strict(
            self, target, fill_path(_USER_CREDENTIALS_PATH, segments), operator=operator
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise _not_found(_GET_OP, user_id) from exc
        raise
    actions = rep.get("requiredActions")
    return {
        "id": rep.get("id"),
        "username": rep.get("username"),
        "enabled": rep.get("enabled"),
        "emailVerified": rep.get("emailVerified"),
        "requiredActions": [a for a in actions if isinstance(a, str)]
        if isinstance(actions, list)
        else [],
        "notBefore": _iso(rep.get("notBefore"), seconds=True),
        "createdTimestamp": _iso(rep.get("createdTimestamp")),
        "credentialDates": [_credential_row(row) for row in credentials],
    }


async def keycloak_user_sessions(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return how many live sessions one user has, with no id and no token."""
    realm = resolve_realm_config(target).managed_realm
    user_id = await _user_id(self, operator, target, realm, params, _SESSIONS_OP)
    path = fill_path(
        _USER_SESSIONS_PATH, {"realm": quote_segment(realm), "user-id": quote_segment(user_id)}
    )
    try:
        rows = await get_admin_list_strict(self, target, path, operator=operator)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise _not_found(_SESSIONS_OP, user_id) from exc
        raise
    sessions = []
    for row in rows:
        if not isinstance(row, dict):
            raise KeycloakUnexpectedResponseError("a user session is not a JSON object")
        clients = row.get("clients")
        names = (
            sorted(c for c in clients.values() if isinstance(c, str))
            if isinstance(clients, dict)
            else []
        )
        sessions.append(
            {
                "started": _iso(row.get("start")),
                "lastAccess": _iso(row.get("lastAccess")),
                "ipAddress": row.get("ipAddress"),
                "clients": names,
            }
        )
    return {"userId": user_id, "count": len(sessions), "sessions": sessions}


_USER_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "pattern": _UUID_PATTERN,
            "description": "The user's internal UUID. Pass this or username.",
        },
        "username": {
            "type": "string",
            "description": "The user's exact username (not a 'contains' search). Pass this or id.",
        },
    },
    "additionalProperties": False,
    "examples": [{"username": "example-user"}, {"id": "00000000-0000-0000-0000-000000000000"}],
}

_USER_HINTS = {
    "id": "The user's UUID, from keycloak.user.list. Pass id or username.",
    "username": "The exact username. Pass id or username.",
}

_GET_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "username": {"type": ["string", "null"]},
        "enabled": {"type": ["boolean", "null"]},
        "emailVerified": {"type": ["boolean", "null"]},
        "requiredActions": {"type": "array", "items": {"type": "string"}},
        "notBefore": {
            "type": ["string", "null"],
            "description": "ISO 8601 UTC. null: not set. Tokens issued before it are refused.",
        },
        "createdTimestamp": {"type": ["string", "null"], "description": "ISO 8601 UTC."},
        "credentialDates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": ["string", "null"], "description": "For example 'password'."},
                    "createdDate": {
                        "type": ["string", "null"],
                        "description": "ISO 8601 UTC. For a password: when it was last set.",
                    },
                },
            },
        },
    },
    "required": ["id", "requiredActions", "credentialDates"],
}

_SESSIONS_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "userId": {"type": "string"},
        "count": {"type": "integer", "description": "0 when the user has no live session."},
        "sessions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "started": {"type": ["string", "null"], "description": "ISO 8601 UTC."},
                    "lastAccess": {"type": ["string", "null"], "description": "ISO 8601 UTC."},
                    "ipAddress": {"type": ["string", "null"]},
                    "clients": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
    "required": ["userId", "count", "sessions"],
}

_WHEN_TO_USE_GET = (
    "Use to read one user's state (``keycloak.user.get``): is the account on, "
    "its required actions, its not-before time, when it was created, and the "
    "type and date of each stored credential, for example when the password "
    "was last set. Never returns credential data."
)
_WHEN_TO_USE_SESSIONS = (
    "Use to read one user's live sessions (``keycloak.user.sessions``): how "
    "many, and when each started and was last used, with IP address and "
    "client names. Never returns a session id or a token."
)

USER_STATE_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id=_GET_OP,
        handler_attr="user_get",
        summary="Read one user's state: enabled, required actions, credential dates.",
        description=(
            "Reads one user by id or exact username. Returns enabled, required "
            "actions, not-before and creation time, and the type and date of "
            "each stored credential. Never credential data."
        ),
        parameter_schema=_USER_PARAMS,
        response_schema=_GET_RESPONSE,
        group_key="user",
        tags=("read-only", "user", "keycloak"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_GET,
            "when_to_call": (
                "To check one user after keycloak.user.reset_password (the "
                "password date is new) or to see the account state. keycloak.user.list "
                "matches part of a name and returns many users."
            ),
            "parameter_hints": _USER_HINTS,
            "output_shape": (
                "``{id, username, enabled, emailVerified, requiredActions, notBefore, "
                "createdTimestamp, credentialDates: [{type, createdDate}]}``. "
                "Times are ISO 8601 UTC."
            ),
            "next_step": (
                "Call keycloak.user.sessions to see the live sessions, or "
                "keycloak.role_mapping.get for the roles."
            ),
        },
    ),
    KeycloakOp(
        op_id=_SESSIONS_OP,
        handler_attr="user_sessions",
        summary="Read one user's live sessions: count, start, last use, IP, clients.",
        description=(
            "Reads the live sessions of one user by id or exact username. "
            "Returns the count and, per session, start time, last access time, "
            "IP address and client names. Never a session id or a token."
        ),
        parameter_schema=_USER_PARAMS,
        response_schema=_SESSIONS_RESPONSE,
        group_key="user",
        tags=("read-only", "user", "keycloak"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_SESSIONS,
            "when_to_call": (
                "To check that keycloak.user.logout worked (count is 0), or to see "
                "where and when a user is signed in."
            ),
            "parameter_hints": _USER_HINTS,
            "output_shape": (
                "``{userId, count, sessions: [{started, lastAccess, ipAddress, "
                "clients}]}``. Times are ISO 8601 UTC. A user with no session "
                "gives count 0."
            ),
            "next_step": (
                "If sessions remain that should not, run keycloak.user.logout "
                "(it waits for a second person to approve)."
            ),
        },
    ),
)

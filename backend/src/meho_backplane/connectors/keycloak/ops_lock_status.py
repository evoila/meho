# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Lock status reads: ``keycloak.user.lock_status``, ``keycloak.realm.lock_settings``.

Keycloak shows the same text at the login page for a wrong password and for
an account that is locked after too many failed sign-ins. These two safe
reads let an operator tell the two apart in MEHO:

* ``keycloak.user.lock_status``: for one user, is the account locked now, how
  many sign-ins failed, how many temporary locks happened, and when the last
  failure was.
* ``keycloak.realm.lock_settings``: the realm's lock rules (is the lock on,
  after how many failures, how long it waits, whether the lock is permanent).

``lock_status`` takes ``id`` (the user's UUID) or ``username`` (an exact match).

Only the named fields are copied out. The IP address of the last failed
sign-in (``lastIPFailure``) is personal data, so it is never returned. A read
that fails, or a body of the wrong shape, is an error: it is never read as
"not locked". Keycloak gives times in milliseconds; they are returned as
ISO 8601 in UTC. A last failure of 0 means "no failure yet" and is ``null``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.keycloak._paths import (
    _ADMIN_REALM_PATH,
    _USER_BRUTE_FORCE_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_read import _UUID_PATTERN, KeycloakOp
from meho_backplane.connectors.keycloak.ops_user_state import _iso, _not_found, _user_id
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    KeycloakUnexpectedResponseError,
    get_or_none,
)
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "LOCK_STATUS_OPS",
    "keycloak_realm_lock_settings",
    "keycloak_user_lock_status",
]

_STATUS_OP = "keycloak.user.lock_status"
_SETTINGS_OP = "keycloak.realm.lock_settings"

_SETTING_FIELDS = (
    "bruteForceProtected",
    "permanentLockout",
    "failureFactor",
    "waitIncrementSeconds",
    "maxFailureWaitSeconds",
    "maxDeltaTimeSeconds",
    "minimumQuickLoginWaitSeconds",
    "quickLoginCheckMilliSeconds",
    "maxTemporaryLockouts",
)


def _opt_int(value: Any) -> int | None:
    """An integer count; ``None`` when absent or not a number."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _opt_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


async def keycloak_user_lock_status(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return one user's lock status after failed sign-ins, with no IP address."""
    realm = resolve_realm_config(target).managed_realm
    realm_segment = quote_segment(realm)
    user_id = await _user_id(self, operator, target, realm, params, _STATUS_OP)
    path = fill_path(
        _USER_BRUTE_FORCE_PATH, {"realm": realm_segment, "user-id": quote_segment(user_id)}
    )
    rep = await get_or_none(self, target, path, operator)
    if rep is None:
        raise _not_found(_STATUS_OP, user_id)
    disabled = rep.get("disabled")
    if not isinstance(disabled, bool):
        raise KeycloakUnexpectedResponseError("the lock status has no 'disabled' flag")
    return {
        "userId": user_id,
        "locked": disabled,
        "numFailures": _opt_int(rep.get("numFailures")),
        "numTemporaryLockouts": _opt_int(rep.get("numTemporaryLockouts")),
        "lastFailure": _iso(rep.get("lastFailure")),
    }


async def keycloak_realm_lock_settings(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return the realm's lock rules for failed sign-ins."""
    realm = resolve_realm_config(target).managed_realm
    row = await self._get_admin_json(
        target, fill_path(_ADMIN_REALM_PATH, {"realm": quote_segment(realm)}), operator=operator
    )
    if "bruteForceProtected" not in row:
        raise KeycloakUnexpectedResponseError("the realm answer has no 'bruteForceProtected'")
    return {"realm": realm, **{field: row.get(field) for field in _SETTING_FIELDS}}


_STATUS_PARAMS: dict[str, Any] = {
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

_SETTINGS_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

_STATUS_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "userId": {"type": "string"},
        "locked": {
            "type": "boolean",
            "description": "True when the account is locked now (temporary or permanent).",
        },
        "numFailures": {
            "type": ["integer", "null"],
            "description": "Failed sign-ins counted now. null: Keycloak did not say.",
        },
        "numTemporaryLockouts": {
            "type": ["integer", "null"],
            "description": "How many temporary locks happened. null: Keycloak did not say.",
        },
        "lastFailure": {
            "type": ["string", "null"],
            "description": "ISO 8601 UTC. null: no failed sign-in yet.",
        },
    },
    "required": ["userId", "locked", "numFailures", "numTemporaryLockouts", "lastFailure"],
}

_SETTINGS_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "realm": {"type": "string"},
        "bruteForceProtected": {"type": ["boolean", "null"], "description": "Is the lock on."},
        "permanentLockout": {
            "type": ["boolean", "null"],
            "description": "True: a locked account stays locked until an admin unlocks it.",
        },
        "failureFactor": {
            "type": ["integer", "null"],
            "description": "Failed sign-ins that cause a lock.",
        },
        "waitIncrementSeconds": {
            "type": ["integer", "null"],
            "description": "How long the first lock lasts. It grows with each lock.",
        },
        "maxFailureWaitSeconds": {
            "type": ["integer", "null"],
            "description": "The longest a temporary lock lasts.",
        },
        "maxDeltaTimeSeconds": {
            "type": ["integer", "null"],
            "description": "Failures older than this are forgotten.",
        },
        "minimumQuickLoginWaitSeconds": {"type": ["integer", "null"]},
        "quickLoginCheckMilliSeconds": {"type": ["integer", "null"]},
        "maxTemporaryLockouts": {"type": ["integer", "null"]},
    },
    "required": ["realm", "bruteForceProtected"],
}

LOCK_STATUS_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id=_STATUS_OP,
        handler_attr="user_lock_status",
        summary="Read whether one user is locked after failed sign-ins.",
        description=(
            "Reads the lock status of one user by id or exact username. Returns "
            "whether the account is locked now, the number of failed sign-ins, "
            "the number of temporary locks and the time of the last failure. "
            "Never the IP address of the failure."
        ),
        parameter_schema=_STATUS_PARAMS,
        response_schema=_STATUS_RESPONSE,
        group_key="user",
        tags=("read-only", "user", "keycloak"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Use to find out if a user is locked after failed sign-ins "
                "(``keycloak.user.lock_status``). Keycloak shows 'Invalid username "
                "or password' for a wrong password and for a locked account, so "
                "the login page cannot tell them apart."
            ),
            "when_to_call": (
                "When a person says they cannot sign in and the login page says "
                "'Invalid username or password'. Call keycloak.user.get too to "
                "see if the account is switched off."
            ),
            "parameter_hints": {
                "id": "The user's UUID, from keycloak.user.list. Pass id or username.",
                "username": "The exact username. Pass id or username.",
            },
            "output_shape": (
                "``{userId, locked, numFailures, numTemporaryLockouts, lastFailure}``. "
                "lastFailure is ISO 8601 UTC, or null when there was no failure."
            ),
            "next_step": (
                "Call keycloak.realm.lock_settings to see how many failures cause "
                "a lock and how long a lock lasts."
            ),
        },
    ),
    KeycloakOp(
        op_id=_SETTINGS_OP,
        handler_attr="realm_lock_settings",
        summary="Read the realm's lock rules for failed sign-ins.",
        description=(
            "Reads the realm's lock rules: whether the lock is on, how many "
            "failed sign-ins cause it, how long it waits, and whether it is "
            "permanent."
        ),
        parameter_schema=_SETTINGS_PARAMS,
        response_schema=_SETTINGS_RESPONSE,
        group_key="realm",
        tags=("read-only", "realm", "keycloak"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Use to read the realm's lock rules for failed sign-ins "
                "(``keycloak.realm.lock_settings``)."
            ),
            "when_to_call": (
                "After keycloak.user.lock_status shows a locked user, to explain "
                "why and for how long. Also to check if the lock is switched on."
            ),
            "parameter_hints": {},
            "output_shape": (
                "``{realm, bruteForceProtected, permanentLockout, failureFactor, "
                "waitIncrementSeconds, maxFailureWaitSeconds, maxDeltaTimeSeconds, "
                "...}``. Times are in seconds, except quickLoginCheckMilliSeconds."
            ),
            "next_step": "Call keycloak.user.lock_status for one user.",
        },
    ),
)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keep user writes from locking MEHO out of a Keycloak.

A Keycloak target signs in with a user name and a password, or with a
client secret (then the user is the client's service-account user). Some
writes on that user make Keycloak refuse MEHO's next sign-in, or take its
admin rights away. MEHO then cannot reach that Keycloak until a person
fixes it by hand.

``keycloak.user.delete`` refuses that user
(:func:`~meho_backplane.connectors.keycloak.ops_write_delete_lookup.check_user_guards`).
These writes use the same check (:data:`LOGIN_USER_WRITES`):

* ``keycloak.user.disable``;
* ``keycloak.user.required_actions.update``;
* ``keycloak.user.reset_password``;
* ``keycloak.user.credential.delete``;
* ``keycloak.user.update`` when ``changes`` has ``username`` or ``email``;
* ``keycloak.group.member.remove`` (the admin rights can come from a group);
* ``keycloak.role_mapping.client.remove``.

Each check runs before the request waits for approval and again when the
approved request runs. Nothing is written. Both use the user name that
Keycloak stores for the user, not the name in the request. The check only
matters when the realm of the write is the realm the target signs in to,
so it reads no admin credential otherwise.

The shared user lookup calls this module
(:mod:`~meho_backplane.connectors.keycloak.ops_write_user_identity`): the
check before approval runs in ``refuse_other_user_check``, the run-time
check in ``read_user_for_write``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.keycloak._paths import _USER_PATH, fill_path
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    FoundObject,
    KeycloakDeleteRefusedError,
    KeycloakUnexpectedResponseError,
    check_user_guards,
    get_or_none,
)
from meho_backplane.connectors.keycloak.session import RealmConfig, resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "LOGIN_USER_WRITES",
    "login_user_action",
    "login_user_park_reason",
    "park_refusal_for_login_user",
    "refuse_login_user",
    "refuse_login_user_of_write",
    "refuse_login_user_rep",
]

#: The user writes that can lock the target out, and the words that finish
#: the sentence "<words> breaks the target".
LOGIN_USER_WRITES: Final[dict[str, str]] = {
    "keycloak.user.disable": "disabling it",
    "keycloak.user.required_actions.update": "changing its required actions",
    "keycloak.user.reset_password": "resetting its password",
    "keycloak.user.credential.delete": "deleting one of its credentials",
    "keycloak.user.update": "changing its user name or email",
    "keycloak.group.member.remove": "removing it from a group",
    "keycloak.role_mapping.client.remove": "removing one of its roles",
}

#: ``keycloak.user.update`` fields the target can sign in with.
_SIGN_IN_FIELDS: Final = ("username", "email")
_USER_UPDATE_OP: Final = "keycloak.user.update"


def _opt_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    return None


def login_user_action(op_id: str, params: dict[str, Any]) -> str | None:
    """The words for *op_id* when it must not touch the sign-in user; else ``None``.

    ``keycloak.user.update`` counts only when it changes the user name or
    the email: a new first or last name cannot lock the target out.
    """
    action = LOGIN_USER_WRITES.get(op_id)
    if action is None or op_id != _USER_UPDATE_OP:
        return action
    changes = params.get("changes")
    if isinstance(changes, dict) and any(field in changes for field in _SIGN_IN_FIELDS):
        return action
    return None


async def refuse_login_user(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    *,
    op_id: str,
    action: str,
    username: str,
    realms: RealmConfig | None = None,
) -> None:
    """Raise :exc:`KeycloakDeleteRefusedError` when *username* is the target's sign-in user.

    *username* is the one Keycloak stores for the user. *realms* defaults to
    the target's realms; its ``managed_realm`` is the realm of the write.
    """
    await check_user_guards(
        connector,
        operator,
        target,
        FoundObject(
            realms=realms or resolve_realm_config(target), object_id=None, name=username, rep=None
        ),
        op_id=op_id,
        action=action,
    )


async def refuse_login_user_rep(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    user: dict[str, Any],
    *,
    op_id: str,
    action: str,
    realms: RealmConfig | None = None,
) -> None:
    """Like :func:`refuse_login_user`, for a user the caller has already read.

    A user without a stored user name is refused too: MEHO cannot tell
    whether it is the sign-in user.
    """
    realms = realms or resolve_realm_config(target)
    if realms.managed_realm != realms.admin_realm:
        return
    stored = _opt_str(user.get("username"))
    if stored is None:
        raise KeycloakUnexpectedResponseError(
            f"{op_id}: Keycloak returned the user without a username, so MEHO cannot "
            "tell whether it is the user this target logs in with. Nothing was changed."
        )
    await refuse_login_user(
        connector,
        operator,
        target,
        op_id=op_id,
        action=action,
        username=stored,
        realms=realms,
    )


async def refuse_login_user_of_write(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
    user: dict[str, Any],
    *,
    op_id: str,
) -> None:
    """The run-time check: refuse *user* when *op_id* must not touch the sign-in user.

    *user* is the user the write then acts on, as Keycloak stores it.
    """
    action = login_user_action(op_id, params)
    if action is not None:
        await refuse_login_user_rep(connector, operator, target, user, op_id=op_id, action=action)


def park_refusal_for_login_user(op_id: str, action: str) -> str:
    """The reason shown before approval. It names no username."""
    return (
        f"{op_id}: this user is the user this MEHO target logs in with; {action} "
        "breaks the target. Nothing was changed."
    )


async def login_user_park_reason(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
    *,
    op_id: str,
    id_key: str = "id",
) -> str | None:
    """The check before approval: the refusal reason for the sign-in user, or ``None``.

    Reads the user named by *id_key* or ``username``. A user that does not
    exist is let through: the run then fails with "not found".
    """
    action = login_user_action(op_id, params)
    if action is None:
        return None
    realms = resolve_realm_config(target)
    if realms.managed_realm != realms.admin_realm:
        return None
    uuid = _opt_str(params.get(id_key))
    if uuid is None:
        username = _opt_str(params.get("username"))
        if username is None:
            return None
        uuid = await connector._find_user_uuid(
            target, realms.managed_realm, username, operator=operator
        )
        if uuid is None:
            return None
    path = fill_path(_USER_PATH, {"realm": realms.managed_realm, "user-id": uuid})
    user = await get_or_none(connector, target, path, operator)
    if user is None:
        return None
    try:
        await refuse_login_user_rep(connector, operator, target, user, op_id=op_id, action=action)
    except KeycloakDeleteRefusedError:
        return park_refusal_for_login_user(op_id, action)
    return None

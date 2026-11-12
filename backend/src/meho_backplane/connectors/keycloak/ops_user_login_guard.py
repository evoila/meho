# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keep user writes from locking MEHO out of a Keycloak.

A Keycloak target can sign in with a user name and a password. A required
action such as ``UPDATE_PASSWORD`` on that user, or turning it off, makes
Keycloak refuse its next password sign-in. MEHO then cannot reach that
Keycloak until a person fixes it by hand.

``keycloak.user.delete`` already refuses the user the target signs in with
(:func:`~meho_backplane.connectors.keycloak.ops_write_delete_lookup.check_user_guards`).
This module uses the same check for the two other writes that can lock the
target out:

* ``keycloak.user.required_actions.update`` (the handler calls
  :func:`refuse_login_user` with the user it has already read);
* ``keycloak.user.disable`` (the handler and a check before the wait for
  approval both call :func:`refuse_login_user_of_request`).

The refusal comes before approval and again when the approved request runs.
Nothing is written. The check only matters when the managed realm is the
realm the target signs in to, so it reads no credential name otherwise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.keycloak._paths import _USER_PATH, fill_path
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    FoundObject,
    KeycloakDeleteRefusedError,
    KeycloakUnexpectedResponseError,
    check_user_guards,
    get_or_none,
)
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config
from meho_backplane.operations._park_check import register_park_check

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "USER_DISABLE_OP_ID",
    "park_refusal_for_login_user",
    "refuse_login_user",
    "refuse_login_user_of_request",
]

USER_DISABLE_OP_ID = "keycloak.user.disable"


def _opt_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    return None


async def refuse_login_user(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    *,
    op_id: str,
    action: str,
    username: str,
) -> None:
    """Raise :exc:`KeycloakDeleteRefusedError` when *username* is the target's sign-in user.

    *username* is the one Keycloak stores for the user. *action* finishes
    the sentence "<action> breaks the target", for example ``"disabling it"``.
    """
    await check_user_guards(
        connector,
        operator,
        target,
        FoundObject(realms=resolve_realm_config(target), object_id=None, name=username, rep=None),
        op_id=op_id,
        action=action,
    )


async def refuse_login_user_of_request(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
    *,
    op_id: str,
    action: str,
) -> None:
    """Like :func:`refuse_login_user`, for a request with ``id`` and/or ``username``.

    Reads the user to get the username Keycloak stores for it. A user that
    does not exist is let through: the write fails with "not found".
    """
    realms = resolve_realm_config(target)
    if realms.managed_realm != realms.admin_realm:
        return
    uuid = _opt_str(params.get("id"))
    if uuid is None:
        username = _opt_str(params.get("username"))
        if username is None:
            return
        uuid = await connector._find_user_uuid(
            target, realms.managed_realm, username, operator=operator
        )
        if uuid is None:
            return
    path = fill_path(_USER_PATH, {"realm": realms.managed_realm, "user-id": quote_segment(uuid)})
    user = await get_or_none(connector, target, path, operator)
    if user is None:
        return
    stored = _opt_str(user.get("username"))
    if stored is None:
        raise KeycloakUnexpectedResponseError(
            f"{op_id}: Keycloak returned the user without a username, so MEHO cannot "
            "tell whether it is the user this target logs in with. Nothing was changed."
        )
    await refuse_login_user(
        connector, operator, target, op_id=op_id, action=action, username=stored
    )


def park_refusal_for_login_user(op_id: str, action: str) -> str:
    """The reason shown before approval. It names no username."""
    return (
        f"{op_id}: this user is the user this MEHO target logs in with; {action} "
        "breaks the target. Nothing was changed."
    )


async def _refuse_disable_of_login_user(ctx: PreviewContext) -> str | None:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    connector = ctx.connector_instance
    if not isinstance(connector, KeycloakConnector) or ctx.target is None:
        # No live connector to ask. The run checks again.
        return None
    try:
        await refuse_login_user_of_request(
            connector,
            ctx.operator,
            ctx.target,
            ctx.params,
            op_id=USER_DISABLE_OP_ID,
            action="disabling it",
        )
    except KeycloakDeleteRefusedError:
        return park_refusal_for_login_user(USER_DISABLE_OP_ID, "disabling it")
    return None


register_park_check(USER_DISABLE_OP_ID, _refuse_disable_of_login_user)

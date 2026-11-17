# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared helpers for the Keycloak user account writes.

Used by the client role ops, ``keycloak.user.update`` and
``keycloak.user.credential.delete``. The user is found and checked by the
shared user check of every Keycloak user write
(:func:`~meho_backplane.connectors.keycloak.ops_write_user_identity.read_user_for_write`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors.keycloak.ops_write import KeycloakUserNotFoundError
from meho_backplane.connectors.keycloak.ops_write_user_identity import read_user_for_write
from meho_backplane.connectors.keycloak.session import resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "REALM_MANAGEMENT_CLIENT",
    "is_realm_management",
    "opt_str",
    "raise_user_not_found_on_404",
    "resolve_user",
]

#: The client whose roles decide who may manage a realm.
REALM_MANAGEMENT_CLIENT = "realm-management"


def opt_str(value: Any) -> str | None:
    """Return a trimmed non-empty string, or ``None`` for absent or blank input."""
    if isinstance(value, str):
        return value.strip() or None
    return None


def is_realm_management(params: dict[str, Any]) -> bool:
    """True when the request names the ``realm-management`` client, in any case."""
    client_id = opt_str(params.get("client_id"))
    return client_id is not None and client_id.casefold() == REALM_MANAGEMENT_CLIENT


async def resolve_user(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
    *,
    op_id: str,
) -> tuple[str, str | None, str]:
    """Return ``(user_uuid, stored_username, managed_realm)``.

    Uses :func:`read_user_for_write`: in the approved run the user id comes
    from the approval card, and a ``username`` that is not the stored one is
    refused. Raises :exc:`ValueError` when neither ``id`` nor ``username`` is
    given or they name different users, and :exc:`KeycloakUserNotFoundError`
    when the user does not exist.
    """
    user = await read_user_for_write(connector, operator, target, params, op_id=op_id)
    return user.uuid, user.username, resolve_realm_config(target).managed_realm


def raise_user_not_found_on_404(
    exc: httpx.HTTPStatusError, *, op_id: str, user_id: str, realm: str
) -> None:
    """Raise :exc:`KeycloakUserNotFoundError` when *exc* is a 404. Else return."""
    if exc.response.status_code == 404:
        raise KeycloakUserNotFoundError(
            f"{op_id}: no user with id={user_id!r} in realm {realm!r}"
        ) from exc

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Turn a Keycloak user off and on: ``keycloak.user.disable`` / ``keycloak.user.enable``.

Both ops set ``enabled`` on the user and keep every other field. They
read the user, put ``enabled`` on top and write the whole user back
(:func:`~meho_backplane.connectors.keycloak.ops_write_merge.read_merge_write`),
so a ``PUT`` never resets a field the caller did not ask for. They key on
the user's internal UUID (``id``) or on the exact ``username``.

Safe to run twice: setting ``enabled`` to the value it already has is a
success. A user that does not exist raises
:exc:`~meho_backplane.connectors.keycloak.ops_write.KeycloakUserNotFoundError`.

Disabling a user does **not** end sessions that are already open. To cut
access at once, run ``keycloak.user.disable``, then ``keycloak.user.logout``.

The op-metadata rows are in
:mod:`~meho_backplane.connectors.keycloak.ops_write_user_enabled_schemas`.
The ops read and return no credential.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors.keycloak._paths import _USER_PATH, fill_path
from meho_backplane.connectors.keycloak.ops_write import KeycloakUserNotFoundError
from meho_backplane.connectors.keycloak.ops_write_merge import read_merge_write
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = ["keycloak_user_disable", "keycloak_user_enable"]


def _opt_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    return None


async def _set_enabled(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
    *,
    op_id: str,
    enabled: bool,
) -> dict[str, Any]:
    realms = resolve_realm_config(target)
    uuid = _opt_str(params.get("id"))
    username = _opt_str(params.get("username"))
    if uuid is None:
        if username is None:
            raise ValueError(f"{op_id} requires either 'id' (UUID) or 'username'")
        uuid = await connector._find_user_uuid(
            target, realms.managed_realm, username, operator=operator
        )
        if uuid is None:
            raise KeycloakUserNotFoundError(
                f"{op_id}: no user with username={username!r} in realm {realms.managed_realm!r}"
            )
    try:
        check = await read_merge_write(
            connector,
            operator,
            target,
            path=fill_path(
                _USER_PATH, {"realm": realms.managed_realm, "user-id": quote_segment(uuid)}
            ),
            requested={"enabled": enabled},
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise KeycloakUserNotFoundError(
                f"{op_id}: no user with id={uuid!r} in realm {realms.managed_realm!r}"
            ) from exc
        raise
    return {
        "id": uuid,
        "username": username,
        "enabled": enabled,
        "unrequested_changes": check["unrequested_changes"],
        "read_back_checked": check["read_back_checked"],
    }


async def keycloak_user_disable(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Turn a user off (``PUT .../users/{id}`` with ``enabled: false``).

    Op-id: ``keycloak.user.disable``. Open sessions stay alive: run
    ``keycloak.user.logout`` after it to cut access at once.
    """
    return await _set_enabled(
        self, operator, target, params, op_id="keycloak.user.disable", enabled=False
    )


async def keycloak_user_enable(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Turn a user on again (``PUT .../users/{id}`` with ``enabled: true``).

    Op-id: ``keycloak.user.enable``.
    """
    return await _set_enabled(
        self, operator, target, params, op_id="keycloak.user.enable", enabled=True
    )

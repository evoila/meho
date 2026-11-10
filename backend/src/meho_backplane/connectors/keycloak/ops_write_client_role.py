# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Give a user a client role, or take it away.

``keycloak.role_mapping.client.assign`` and
``keycloak.role_mapping.client.remove`` change the roles of one client that
a user holds. Example: give ``realm-management`` / ``realm-admin``, so a
person can use the admin console of one realm.

Both are dangerous. ``assign`` sends ``POST``, ``remove`` sends ``DELETE``, to
``.../users/{id}/role-mappings/clients/{client}``.

How a call runs:

1. Find the user (``id`` or exact ``username``) and the client
   (``client_id``, the human name, resolved to the client's UUID).
2. Read each named role on that client. A role that does not exist is an
   error, and nothing is sent. So a typo never gives or removes part of the
   roles.
3. Send the role representations to Keycloak.
4. Read the user's roles on the client again and say which named roles do
   not match the request (``not_applied``). When the read fails, the write
   has already run, so the op still succeeds and says
   ``read_back_checked: false``.

Keycloak treats a second assign, or a remove of a role the user does not
hold, as a success, so both ops are safe to run twice.
The ops read and return no credential.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from meho_backplane.connectors._shared.vault_creds import CredentialsReadError
from meho_backplane.connectors.adapters.http import ResponseTooLargeError
from meho_backplane.connectors.keycloak._paths import (
    _CLIENT_ROLE_PATH,
    _USER_ROLE_MAPPINGS_CLIENT_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.connector import KeycloakAdminTokenError
from meho_backplane.connectors.keycloak.ops_write import KeycloakRoleNotFoundError
from meho_backplane.connectors.keycloak.ops_write_account_common import (
    opt_str,
    raise_user_not_found_on_404,
    resolve_user,
)
from meho_backplane.connectors.keycloak.secret_endpoint import KeycloakClientNotFoundError
from meho_backplane.connectors.keycloak.session import quote_segment

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = ["keycloak_role_mapping_client_assign", "keycloak_role_mapping_client_remove"]


def role_names(params: dict[str, Any]) -> list[str]:
    """The role names of the request: trimmed, without blanks and repeats, in order."""
    raw = params.get("roles")
    names: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        name = str(item).strip()
        if name and name not in names:
            names.append(name)
    return names


async def _resolve_roles(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    *,
    op_id: str,
    realm: str,
    client_id: str,
    client_uuid: str,
    names: list[str],
) -> list[dict[str, Any]]:
    """Read each named role on the client. An unknown role raises before any write."""
    reprs: list[dict[str, Any]] = []
    for name in names:
        try:
            role = await connector._get_admin_json(
                target,
                fill_path(
                    _CLIENT_ROLE_PATH,
                    {
                        "realm": realm,
                        "client-uuid": quote_segment(client_uuid),
                        "role-name": quote_segment(name),
                    },
                ),
                operator=operator,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            role = {}
        if role.get("name") != name or not isinstance(role.get("id"), str):
            raise KeycloakRoleNotFoundError(
                f"{op_id}: client {client_id!r} has no role {name!r} in realm {realm!r}"
            )
        reprs.append(role)
    return reprs


async def _change_client_roles(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
    *,
    op_id: str,
    method: str,
) -> dict[str, Any]:
    uuid, username, realm = await resolve_user(connector, operator, target, params, op_id=op_id)
    client_id = opt_str(params.get("client_id"))
    if client_id is None:
        raise ValueError(f"{op_id} requires 'client_id' (the client's name, not its UUID)")
    names = role_names(params)
    if not names:
        raise ValueError(f"{op_id} requires at least one name in 'roles'")
    client_uuid = await connector._find_client_uuid(target, realm, client_id, operator=operator)
    if client_uuid is None:
        raise KeycloakClientNotFoundError(f"{op_id}: no client {client_id!r} in realm {realm!r}")
    reprs = await _resolve_roles(
        connector,
        operator,
        target,
        op_id=op_id,
        realm=realm,
        client_id=client_id,
        client_uuid=client_uuid,
        names=names,
    )
    path = fill_path(
        _USER_ROLE_MAPPINGS_CLIENT_PATH,
        {"realm": realm, "user-id": quote_segment(uuid), "client-id": quote_segment(client_uuid)},
    )
    try:
        await connector._write_admin(
            target,
            method,
            path,
            operator=operator,
            json=reprs,  # type: ignore[arg-type]  # KC accepts a JSON array body here
            idempotent_conflict=False,
        )
    except httpx.HTTPStatusError as exc:
        raise_user_not_found_on_404(exc, op_id=op_id, user_id=uuid, realm=realm)
        raise
    return {
        "id": uuid,
        "username": username,
        "client_id": client_id,
        "roles": names,
        **await _read_back(connector, operator, target, path, names, want_held=method == "POST"),
    }


async def _read_back(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    path: str,
    names: list[str],
    *,
    want_held: bool,
) -> dict[str, Any]:
    """Compare the roles the user holds on the client now with the request."""
    try:
        rows = await connector._get_admin_list(target, path, operator=operator, strict=True)
    except (
        httpx.HTTPError,
        ResponseTooLargeError,
        OSError,
        ValueError,
        KeycloakAdminTokenError,
        CredentialsReadError,
    ):
        return {"read_back_checked": False, "not_applied": None}
    held = {row.get("name") for row in rows if isinstance(row, dict)}
    return {
        "read_back_checked": True,
        "not_applied": [name for name in names if (name in held) != want_held],
    }


async def keycloak_role_mapping_client_assign(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Give a user roles of one client (``POST .../role-mappings/clients/{client}``).

    Op-id: ``keycloak.role_mapping.client.assign``. A privilege grant.
    """
    return await _change_client_roles(
        self, operator, target, params, op_id="keycloak.role_mapping.client.assign", method="POST"
    )


async def keycloak_role_mapping_client_remove(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Take roles of one client from a user (``DELETE .../role-mappings/clients/{client}``).

    Op-id: ``keycloak.role_mapping.client.remove``.
    """
    return await _change_client_roles(
        self, operator, target, params, op_id="keycloak.role_mapping.client.remove", method="DELETE"
    )

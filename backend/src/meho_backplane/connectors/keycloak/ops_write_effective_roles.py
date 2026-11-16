# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What a role really gives: the parts of a role, on every level.

A realm role or a client role can be a composite: it is made of other
roles, and those can be composites too. Giving a user the outer role gives
the user every part. ``keycloak.role_mapping.assign`` and
``keycloak.role_mapping.client.assign`` use :func:`effective_roles` when they
wait for approval in a realm of MEHO's own login. The approval card lists the
result under ``effective_roles``. Each entry is::

    {"id", "name", "client_id", "realm_management", "login_role"}

* ``client_id`` is ``None`` for a realm role.
* ``realm_management`` marks a role of the ``realm-management`` client: it
  decides who may manage the realm.
* ``login_role`` marks a realm role that MEHO's login turns into platform
  admin rights (:func:`platform_role_names`).

The list is ``None`` when MEHO could not read it completely: a read failed,
a role is missing, there are more than :data:`ROLE_CAP` roles, or the
nesting is deeper than :data:`DEPTH_CAP`. The approval then counts the
change as the strict case. The same list is built again right before the
approved change runs. When the ids differ, the run is refused
(:data:`ROLE_PARTS_CHANGED`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.keycloak._paths import (
    _CLIENT_PATH,
    _CLIENT_ROLE_PATH,
    _ROLE_BY_ID_COMPOSITES_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_write_account_common import (
    REALM_MANAGEMENT_CLIENT,
    opt_str,
)
from meho_backplane.connectors.keycloak.session import quote_segment
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "DEPTH_CAP",
    "ROLE_CAP",
    "ROLE_PARTS_CHANGED",
    "effective_roles",
    "platform_role_names",
]

#: The most roles MEHO reads for one request. More counts as "unreadable".
ROLE_CAP: Final[int] = 500

#: The deepest nesting of composite roles MEHO follows.
DEPTH_CAP: Final[int] = 10

#: Why an approved role change did not run.
ROLE_PARTS_CHANGED: Final[str] = (
    "The parts of the role changed since the request; send a new request. "
    "The operation did not run."
)


def platform_role_names() -> set[str] | None:
    """The realm role names that MEHO's login turns into platform admin rights.

    The login gives these rights for one realm role only: the exact name in
    ``JWT_PLATFORM_ADMIN_ROLE_NAME``. Unset, no role gives them. ``None`` when
    the settings cannot be read.
    """
    try:
        configured = get_settings().jwt_platform_admin_role_name
    except Exception:
        return None
    return {configured} if configured else set()


def _sort_key(role: dict[str, Any]) -> tuple[str, str, str]:
    return (role["client_id"] or "", role["name"].casefold(), role["id"])


async def _client_name(
    connector: KeycloakConnector,
    ctx: PreviewContext,
    realm: str,
    client_uuid: str,
    cache: dict[str, str],
) -> str:
    if client_uuid not in cache:
        row = await connector._get_admin_json(
            ctx.target,
            fill_path(_CLIENT_PATH, {"realm": realm, "client-uuid": quote_segment(client_uuid)}),
            operator=ctx.operator,
        )
        name = opt_str(row.get("clientId"))
        if name is None:
            raise ValueError("client without a name")
        cache[client_uuid] = name
    return cache[client_uuid]


async def _start_roles(
    connector: KeycloakConnector, ctx: PreviewContext, realm: str, client_id: str | None
) -> list[dict[str, Any]]:
    """The roles the request names, read from Keycloak. A role that is missing raises."""
    from meho_backplane.connectors.keycloak.ops_write_client_role import role_names

    client_uuid: str | None = None
    if client_id is not None:
        client_uuid = await connector._find_client_uuid(
            ctx.target, realm, client_id, operator=ctx.operator
        )
        if client_uuid is None:
            raise ValueError("client not found")
    roles: list[dict[str, Any]] = []
    for name in role_names(ctx.params):
        if client_uuid is None:
            role = await connector._find_realm_role(ctx.target, realm, name, operator=ctx.operator)
        else:
            try:
                role = await connector._get_admin_json(
                    ctx.target,
                    fill_path(
                        _CLIENT_ROLE_PATH,
                        {
                            "realm": realm,
                            "client-uuid": quote_segment(client_uuid),
                            "role-name": quote_segment(name),
                        },
                    ),
                    operator=ctx.operator,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                role = None
        if role is None or role.get("name") != name:
            raise ValueError("role not found")
        roles.append(role)
    return roles


class _UnreadableError(Exception):
    """The roles cannot be read completely."""


async def _entry(
    connector: KeycloakConnector,
    ctx: PreviewContext,
    realm: str,
    role: dict[str, Any],
    platform: set[str],
    clients: dict[str, str],
) -> dict[str, Any]:
    role_id = opt_str(role.get("id"))
    name = opt_str(role.get("name"))
    if role_id is None or name is None:
        raise _UnreadableError
    owner: str | None = None
    if role.get("clientRole") is True:
        container = opt_str(role.get("containerId"))
        if container is None:
            raise _UnreadableError
        owner = await _client_name(connector, ctx, realm, container, clients)
    return {
        "id": role_id,
        "name": name,
        "client_id": owner,
        "realm_management": owner is not None and owner.casefold() == REALM_MANAGEMENT_CLIENT,
        "login_role": owner is None and name in platform,
    }


async def _parts(
    connector: KeycloakConnector, ctx: PreviewContext, realm: str, role_id: str
) -> list[dict[str, Any]]:
    """The direct parts of one composite role."""
    parts = await connector._get_admin_list(
        ctx.target,
        fill_path(_ROLE_BY_ID_COMPOSITES_PATH, {"realm": realm, "role-id": quote_segment(role_id)}),
        operator=ctx.operator,
        strict=True,
    )
    if not all(isinstance(part, dict) for part in parts):
        raise _UnreadableError
    return parts


async def effective_roles(
    connector: KeycloakConnector, ctx: PreviewContext, realm: str, *, client_id: str | None
) -> list[dict[str, Any]] | None:
    """The roles the request gives, with the parts of every composite; ``None`` when unreadable.

    *client_id* is the client the named roles belong to, or ``None`` for realm roles.
    """
    platform = platform_role_names()
    if platform is None:
        return None
    clients: dict[str, str] = {}
    found: dict[str, dict[str, Any]] = {}
    try:
        level = await _start_roles(connector, ctx, realm, client_id)
        for _depth in range(DEPTH_CAP + 1):
            following: list[dict[str, Any]] = []
            for role in level:
                entry = await _entry(connector, ctx, realm, role, platform, clients)
                if entry["id"] in found:
                    continue
                found[entry["id"]] = entry
                if len(found) > ROLE_CAP:
                    raise _UnreadableError
                if role.get("composite") is True:
                    following.extend(await _parts(connector, ctx, realm, entry["id"]))
            level = following
            if not level:
                return sorted(found.values(), key=_sort_key)
    except Exception:
        return None
    return None
